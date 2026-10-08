"""Posthoc collection never changes a failed result or overwrites another writer."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import threading

import pytest

from agent_trace_kit import desk as desk_mod
from agent_trace_kit import failed_capture as capture_mod
from agent_trace_kit.desk_store import DeskStore

REAL_CAPTURE = capture_mod.capture_failed_side


@pytest.fixture
def failed_pair(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "实现准确采样。", "agent": "codex", "cli_model": "auto_model/urm",
                            "github_owner": "example", "github_repo": "sampler"})
    original = tmp_path / "original"
    original.mkdir()
    (original / "source.txt").write_text("原始失败产物\n", encoding="utf-8")
    store.update_side(job["id"], "A", {"status": "failed", "workspace": str(original), "exit_code": 124,
                                       "error": "预算终止；原始错误保留", "finished_at": "2026-10-01T00:46:08Z",
                                       "attempts": [{"attempt": 4, "status": "cut", "session_id": "cut-session"}]})
    store.update_side(job["id"], "B", {"status": "done", "exit_code": 0, "head_sha": "b" * 40,
                                       "session_id": "complete-session"})
    desk = desk_mod.DeskServer(store)
    calls = []

    def prepare(job, side, evidence, *, expected_job):
        assert job == expected_job
        calls.append((job["id"], side))
        snapshot = evidence / "failed-capture" / "snapshot"
        snapshot.mkdir(parents=True, exist_ok=True)
        trace = evidence / "cut.jsonl"
        trace.write_text('{"cut":true}\n', encoding="utf-8")
        receipt_path = snapshot.parent / "posthoc-failed-capture.json"
        receipt = {"snapshot_branch": f"codex/failed-capture/{job['id']}-a-123456789abc"}
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        return {"patch": {"workspace": str(snapshot), "head_sha": "a" * 40, "head_url": "", "pushed": False,
                          "session_id": "cut-session", "jsonl_local": str(trace), "failure_capture": {
                              "source_workspace": job["sides"][side]["workspace"], "source_head_sha": "c" * 40,
                              "origin": "posthoc-failed-worktree", "completed": False}},
                "receipt": receipt, "receipt_path": receipt_path}

    monkeypatch.setattr(capture_mod, "capture_failed_side", prepare)
    git_calls = []
    monkeypatch.setattr(desk_mod.ws, "git", lambda args, workspace, **kw: git_calls.append((args, str(workspace), kw)))
    push_calls = []
    monkeypatch.setattr(desk_mod.ws, "ensure_remote_contains",
                        lambda workspace, sha, branch, **kw: push_calls.append((str(workspace), sha, branch, kw)))
    monkeypatch.setattr(desk_mod.ws, "commit_permalink", lambda workspace, sha: f"https://github.com/example/sampler/commit/{sha}")
    return store, desk, original, calls, git_calls, push_calls


def request(store, job_id):
    side = store.get_job(job_id)["sides"]["A"]
    return {"job": job_id, "side": "A", "origin": "ai-authorized-failure-capture",
            "expected_side_sha256": capture_mod.side_sha256({**side, "live": False})}


def test_capture_binds_actual_snapshot_and_preserves_failed_generation(failed_pair):
    store, desk, original, calls, git_calls, push_calls = failed_pair
    job_id = store.list_jobs()[0]["id"]
    before = store.get_job(job_id)
    response = desk.failed_capture(request(store, job_id))
    after = store.get_job(job_id)
    assert response == {"captured": True, "job": job_id, "side": "A",
                        "receipt_sha256": after["sides"]["A"]["failure_capture"]["receipt_sha256"]}
    side = after["sides"]["A"]
    for key in ("status", "exit_code", "error", "finished_at", "attempts", "branch", "initial_sha"):
        assert side[key] == before["sides"]["A"][key]
    assert after["sides"]["B"] == before["sides"]["B"]
    assert after["review"] == before["review"]
    assert side["head_url"] == "https://github.com/example/sampler/commit/" + "a" * 40
    assert side["pushed"] is True and side["failure_capture"]["completed"] is False
    assert (original / "source.txt").read_text(encoding="utf-8") == "原始失败产物\n"
    assert calls == [(job_id, "A")]
    assert all(path != str(original) for _, path, _ in git_calls)
    assert git_calls[-1][0] == ["remote", "add", "origin", "https://github.com/example/sampler.git"]
    assert push_calls[0][2].startswith(f"codex/failed-capture/{job_id}-a-")
    assert push_calls[0][3] == {"force": False}
    receipt_path = side["failure_capture"]["receipt_path"]
    assert hashlib.sha256(open(receipt_path, "rb").read()).hexdigest() == response["receipt_sha256"]


@pytest.mark.parametrize("kind", ["sibling_running", "live", "active", "queued", "review", "archive", "model", "stale"])
def test_capture_rejects_busy_changed_or_reviewed_targets_before_copy(failed_pair, monkeypatch, kind):
    store, desk, _, calls, _, _ = failed_pair
    job_id = store.list_jobs()[0]["id"]
    body = request(store, job_id)
    if kind == "sibling_running":
        store.update_side(job_id, "B", {"status": "running"})
    elif kind == "live":
        monkeypatch.setattr(desk.runner, "is_side_live", lambda *_: True)
    elif kind == "active":
        desk.runner._active[job_id] = True
    elif kind == "queued":
        desk.runner._queued.add(job_id)
    elif kind == "review":
        store.update_review(job_id, {"validity": "作废-工程故障"})
    elif kind == "archive":
        store.set_archived(job_id, True)
    elif kind == "model":
        store.update_job(job_id, {"codex_model": "another-model"})
    else:
        store.update_side(job_id, "A", {"error": "new error"})
    with pytest.raises(RuntimeError):
        desk.failed_capture(body)
    assert calls == []
    assert job_id not in desk._failed_capture_jobs


@pytest.mark.parametrize("field,value", [("side", "../A"), ("job", "../pair-123456789a"),
                                         ("origin", "manual"), ("expected_side_sha256", "bad"),
                                         ("validity", "有效"), ("command", "anything")])
def test_capture_has_no_generic_update_or_execution_fields(failed_pair, field, value):
    store, desk, _, calls, _, _ = failed_pair
    body = request(store, store.list_jobs()[0]["id"])
    body[field] = value
    with pytest.raises(RuntimeError):
        desk.failed_capture(body)
    assert calls == []


@pytest.mark.parametrize("change", ["side", "sibling", "review"])
def test_capture_compare_and_swap_preserves_concurrent_updates(failed_pair, monkeypatch, change):
    store, desk, _, _, _, _ = failed_pair
    job_id = store.list_jobs()[0]["id"]
    body = request(store, job_id)
    original_prepare = capture_mod.capture_failed_side

    def changed(*args, **kwargs):
        prepared = original_prepare(*args, **kwargs)
        if change == "side":
            store.update_side(job_id, "A", {"error": "independent update"})
        elif change == "sibling":
            store.update_side(job_id, "B", {"finished_at": "new finish"})
        else:
            store.update_review(job_id, {"validity": "作废-工程故障"})
        return prepared

    monkeypatch.setattr(capture_mod, "capture_failed_side", changed)
    with pytest.raises(RuntimeError, match="已变化"):
        desk.failed_capture(body)
    saved = store.get_job(job_id)
    assert saved["sides"]["A"]["head_sha"] == ""
    assert saved["sides"]["A"]["status"] == "failed"
    assert not saved["sides"]["A"].get("failure_capture")
    if change == "side":
        assert saved["sides"]["A"]["error"] == "independent update"
    elif change == "sibling":
        assert saved["sides"]["B"]["finished_at"] == "new finish"
    else:
        assert saved["review"]["validity"] == "作废-工程故障"


def test_capture_does_not_hold_store_lock_and_blocks_target_mutations(failed_pair, monkeypatch):
    store, desk, _, _, _, _ = failed_pair
    job_id = store.list_jobs()[0]["id"]
    other = store.create_job({"prompt": "other", "agent": "codex", "cli_model": "auto_model/urm"})
    original_prepare = capture_mod.capture_failed_side

    def prepare(*args, **kwargs):
        for action in (lambda: desk.job_action({"job": job_id, "action": "enqueue"}),
                       lambda: desk.side_action({"job": job_id, "side": "A", "action": "retry"}),
                       lambda: desk.review_save({"job": job_id, "validity": "有效"})):
            with pytest.raises(RuntimeError, match="冻结失败产物"):
                action()
        done = threading.Event()
        writer = threading.Thread(target=lambda: (store.update_job(other["id"], {"name": "progress"}), done.set()))
        writer.start()
        assert done.wait(2), "snapshot preparation held the shared store lock"
        writer.join()
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(capture_mod, "capture_failed_side", prepare)
    assert desk.failed_capture(request(store, job_id))["captured"]
    assert store.get_job(other["id"])["name"] == "progress"


def test_store_capture_cannot_change_failure_status_or_human_fields(failed_pair):
    store, _, _, _, _, _ = failed_pair
    job = store.list_jobs()[0]
    with pytest.raises(RuntimeError, match="指定的证据字段"):
        store.update_failed_capture(job["id"], "A", {"status": "done"}, copy.deepcopy(job["sides"]), job["review"])
    assert store.get_job(job["id"])["sides"]["A"]["status"] == "failed"


def test_status_only_reports_runtime_readiness_without_mutation(failed_pair):
    store, desk, _, _, _, _ = failed_pair
    before = store.list_jobs()
    media = {"recording_jobs": [], "recording_keys": [], "preview_keys": [], "preview_pending_keys": []}
    assert desk.failed_capture_status({}) == {"enabled": True, "busy_jobs": [], "active_jobs": [], **media}
    job_id = before[0]["id"]
    desk._failed_capture_jobs.add(job_id)
    desk.runner._active["another-job"] = True
    assert desk.failed_capture_status({}) == {"enabled": True, "busy_jobs": [job_id], "active_jobs": ["another-job"], **media}
    with pytest.raises(RuntimeError):
        desk.failed_capture_status({"command": "anything"})
    assert store.list_jobs() == before


def test_real_capture_module_integrates_with_desk_and_frozen_job_contract(failed_pair, monkeypatch):
    store, desk, source, _, _, push_calls = failed_pair
    job = store.list_jobs()[0]

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), "-c", "user.name=Test",
                                        "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false", *args],
                                       encoding="utf-8").strip()

    git("init", "-q")
    git("add", "source.txt")
    git("commit", "-q", "-m", "initial")
    initial = git("rev-parse", "HEAD")
    (source / "actual.py").write_text("raise RuntimeError('真实失败')\n", encoding="utf-8")
    attempts = store.evidence_dir(job["id"]) / "attempts"
    attempts.mkdir()
    trace = attempts / "a-04-cut-session.jsonl"
    events = [{"type": "session_meta", "payload": {"id": "cut-session", "cwd": str(source)}},
              {"type": "turn_context", "payload": {"model": "auto_model/urm"}},
              {"type": "event_msg", "payload": {"type": "user_message", "message": job["prompt"]}},
              {"type": "event_msg", "payload": {"type": "turn_failed", "error": "timeout"}}]
    trace.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
    stream = attempts / "a-04-stream.jsonl"
    stream.write_text('{"type":"thread.started","thread_id":"cut-session"}\n', encoding="utf-8")
    store.update_side(job["id"], "A", {"initial_sha": initial, "attempts": [{
        "attempt": 4, "status": "cut", "session_id": "cut-session", "exit_code": 124,
        "transcript_path": str(trace), "stream_path": str(stream)}]})
    before = store.get_job(job["id"])
    source_bytes = (source / "actual.py").read_bytes()
    trace_bytes = trace.read_bytes()
    monkeypatch.setattr(capture_mod, "capture_failed_side", REAL_CAPTURE)
    response = desk.failed_capture(request(store, job["id"]))
    saved = store.get_job(job["id"])["sides"]["A"]
    snapshot = Path(saved["workspace"])
    assert snapshot != source and (snapshot / "actual.py").read_bytes() == source_bytes
    assert (source / "actual.py").read_bytes() == source_bytes and trace.read_bytes() == trace_bytes
    assert git("rev-parse", "HEAD") == initial
    for key in ("status", "error", "exit_code", "finished_at", "attempts", "branch"):
        assert saved[key] == before["sides"]["A"][key]
    assert saved["failure_capture"]["completed"] is False
    receipt = json.loads(Path(saved["failure_capture"]["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["pushed"] is False  # immutable core receipt records preparation
    assert receipt["last_attempt"]["interruption_reason"] == "turn_failed"
    assert len(receipt["evidence_files"]) == 2 and response["captured"]
    assert len(push_calls) == 1


def test_capture_status_uses_actual_recording_and_preview_liveness(failed_pair):
    store, desk, _, calls, _, _ = failed_pair
    job_id = store.list_jobs()[0]["id"]
    key = job_id + "/A"
    desk.recorder._recs[key] = {"proc": None}  # startup reservation is already busy
    assert desk.failed_capture_status({})["recording_jobs"] == [job_id]
    with pytest.raises(RuntimeError):
        desk.failed_capture(request(store, job_id))
    desk.recorder._recs.clear()
    release = threading.Event()
    alive = threading.Thread(target=lambda: release.wait(5))
    alive.start()
    try:
        desk.previews._items[key] = {"thread": alive}
        assert desk.failed_capture_status({})["preview_keys"] == [key]
        with pytest.raises(RuntimeError):
            desk.failed_capture(request(store, job_id))
    finally:
        release.set()
        alive.join()
    assert desk.failed_capture_status({})["preview_keys"] == []
    desk.previews._items[key] = {}
    assert desk.failed_capture_status({})["preview_pending_keys"] == [key]
    with pytest.raises(RuntimeError):
        desk.failed_capture(request(store, job_id))
    assert calls == []


def test_status_sees_preview_start_before_registry_entry_is_created(failed_pair, monkeypatch):
    store, desk, source, calls, _, _ = failed_pair
    job_id = store.list_jobs()[0]["id"]
    started, release = threading.Event(), threading.Event()
    errors = []

    def blocked_start(*args, **kwargs):
        started.set()
        assert release.wait(5)
        raise RuntimeError("stop test preview")

    monkeypatch.setattr(desk_mod.ports_mod, "start_static_preview", blocked_start)

    def start():
        try:
            desk.previews.start(job_id, "A", str(source))
        except RuntimeError as error:
            errors.append(str(error))

    worker = threading.Thread(target=start)
    worker.start()
    try:
        assert started.wait(2)
        assert desk.previews._items == {}
        assert desk.failed_capture_status({})["preview_pending_keys"] == [job_id + "/A"]
        with pytest.raises(RuntimeError):
            desk.failed_capture(request(store, job_id))
        assert calls == []
    finally:
        release.set()
        worker.join()
    assert errors == ["stop test preview"]
    assert desk.failed_capture_status({})["preview_pending_keys"] == []
