"""Tests for the pair desk: store, workspace/git automation, checklist, TSV, session discovery, OSS signing."""
from __future__ import annotations

import csv
import io
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent_trace_kit import checklist as cl
from agent_trace_kit import export_tsv
from agent_trace_kit import ghutil
from agent_trace_kit import oss as oss_mod
from agent_trace_kit import workspace as ws
from agent_trace_kit.desk import DeskServer
from agent_trace_kit.desk_store import DeskStore
from agent_trace_kit.runner import (
    PairRunner,
    side_wall_seconds,
    stall_seconds_from_settings,
    watchdog_idle_seconds,
)


def test_ensure_workers_scales_up_without_restart(tmp_path):
    """Saving a higher parallel-pair cap must spawn extra workers immediately."""
    store = DeskStore(tmp_path / "desk")
    store.save_settings({"max_parallel_pairs": 2})
    runner = PairRunner(store)
    runner.start()
    assert len(runner._workers) == 2
    store.save_settings({"max_parallel_pairs": 8})
    assert runner.ensure_workers() == 8
    assert len(runner._workers) == 8
    runner.stop()


# ---------- git fixtures ----------

def _git(args, cwd):
    import subprocess
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def baseline_repo(tmp_path):
    import subprocess
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = tmp_path / "base"
    base.mkdir()
    _git(["init", "-b", "main"], base)
    _git(["config", "user.email", "t@example.com"], base)
    _git(["config", "user.name", "T"], base)
    (base / ".gitignore").write_text(".env\n", encoding="utf-8")
    (base / "README.md").write_text("# baseline\n", encoding="utf-8")
    _git(["add", "-A"], base)
    _git(["commit", "-m", "baseline"], base)
    _git(["remote", "add", "origin", str(bare)], base)
    _git(["push", "-u", "origin", "main"], base)
    return base


def test_watchdog_idle_resets_only_when_transcript_grows():
    idle, last = watchdog_idle_seconds(
        30, cpu_io_busy=False, prev_transcript_mtime=0, cur_transcript_mtime=100, poll_every=15)
    assert idle == 0.0 and last == 100
    idle, last = watchdog_idle_seconds(
        0, cpu_io_busy=False, prev_transcript_mtime=100, cur_transcript_mtime=100, poll_every=15)
    assert idle == 15 and last == 100
    idle, last = watchdog_idle_seconds(
        15, cpu_io_busy=True, prev_transcript_mtime=100, cur_transcript_mtime=100, poll_every=15)
    assert idle == 0.0


def test_side_wall_seconds_clamps():
    assert side_wall_seconds({}) == 4 * 3600
    assert side_wall_seconds({"side_wall_budget_seconds": 99 * 3600}) == 8 * 3600
    assert side_wall_seconds({"side_wall_budget_seconds": 10}) == 60
    assert side_wall_seconds({"side_wall_budget_seconds": "nope"}) == 4 * 3600
    assert stall_seconds_from_settings({}) == 0
    assert stall_seconds_from_settings({"stall_seconds": 90}) == 90
    assert stall_seconds_from_settings({"stall_seconds": "x"}) == 0


def test_store_create_update_and_crash_recovery(tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "do X", "baseline_repo": str(tmp_path)})
    assert job["status"] == "draft"
    assert job["sides"]["A"]["branch"].endswith("-a")
    store.update_side(job["id"], "A", {"status": "running"})
    # simulate a fresh process after crash
    PairRunner(store).recover()
    recovered = store.get_job(job["id"])
    assert recovered["sides"]["A"]["status"] == "failed"
    assert "重跑" in recovered["sides"]["A"]["error"]
    assert len(store.list_jobs()) == 1


def test_retry_blocked_when_side_inflight_even_if_json_says_pending(tmp_path):
    """UI 待运行 but the side thread is still in run_side / recopy / backoff."""
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "baseline_repo": str(tmp_path)})
    store.update_job(job["id"], {"baseline_sha": "a" * 40, "status": "running"})
    store.update_side(job["id"], "B", {"status": "pending"})
    runner = PairRunner(store)
    runner._inflight.add(f"{job['id']}/B")
    assert runner.is_side_running(job["id"], "B")
    with pytest.raises(RuntimeError, match="正在运行"):
        runner.retry_side(job["id"], "B")


def test_sync_job_status_keeps_running_when_sibling_failed(tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "baseline_repo": str(tmp_path)})
    store.update_job(job["id"], {"baseline_sha": "a" * 40, "status": "running"})
    store.update_side(job["id"], "A", {"status": "running"})
    store.update_side(job["id"], "B", {"status": "failed"})
    PairRunner(store)._sync_job_status(job["id"])
    assert store.get_job(job["id"])["status"] == "running"


def test_fresh_workspace_during_run_marks_preparing_not_pending(baseline_repo, tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "baseline_repo": str(baseline_repo)})
    PairRunner(store).prepare(job["id"])
    runner = PairRunner(store)
    runner._fresh_workspace(job["id"], "B", during_run=True)
    assert store.get_job(job["id"])["sides"]["B"]["status"] == "preparing"
    runner._fresh_workspace(job["id"], "A", during_run=False)
    assert store.get_job(job["id"])["sides"]["A"]["status"] == "pending"


def test_run_job_skips_failed_side_that_still_needs_recopy(tmp_path):
    """Clicking ② must not launch B on a dirty tree while A is being recopied."""
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "baseline_repo": str(tmp_path)})
    store.update_job(job["id"], {"baseline_sha": "a" * 40, "status": "ready"})
    store.update_side(job["id"], "A", {"status": "preparing"})
    store.update_side(job["id"], "B", {"status": "failed"})
    runner = PairRunner(store)
    launched: list[str] = []
    runner.run_side = lambda jid, side: launched.append(side)
    runner._run_job(job["id"])
    assert launched == ["A"]


def test_enqueue_recopies_all_failed_sides_before_queueing(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "baseline_repo": str(tmp_path)})
    store.update_job(job["id"], {"baseline_sha": "a" * 40, "status": "failed"})
    store.update_side(job["id"], "A", {"status": "failed"})
    store.update_side(job["id"], "B", {"status": "failed"})
    desk = DeskServer(store)
    order: list[tuple] = []
    monkeypatch.setattr(desk.runner, "retry_side",
                        lambda jid, side, enqueue=True: order.append(("retry", side, enqueue)))
    monkeypatch.setattr(desk.runner, "enqueue",
                        lambda jid: order.append(("enqueue", jid)))
    monkeypatch.setattr(desk.runner, "_sync_job_status", lambda jid: None)
    desk.job_action({"job": job["id"], "action": "enqueue"})
    assert order[:2] == [("retry", "A", False), ("retry", "B", False)]
    assert order[-1] == ("enqueue", job["id"])


def test_public_job_live_is_cli_not_preparing_status(tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "baseline_repo": str(tmp_path)})
    store.update_side(job["id"], "B", {"status": "preparing"})
    desk = DeskServer(store)
    pub = desk.public_job(store.get_job(job["id"]))
    assert pub["sides"]["B"]["live"] is False
    desk.runner._inflight.add(f"{job['id']}/B")
    pub = desk.public_job(store.get_job(job["id"]))
    assert pub["sides"]["B"]["live"] is True


def _new_desk_job(tmp_path, **job_patch):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "baseline_repo": str(tmp_path)})
    if job_patch:
        store.update_job(job["id"], job_patch)
    return store, job["id"]


def test_archive_then_restore_roundtrip(tmp_path):
    store, jid = _new_desk_job(tmp_path)
    desk = DeskServer(store)
    out = desk.job_action({"job": jid, "action": "archive"})
    assert out["archived"] is True
    assert store.get_job(jid)["archived"] is True
    assert store.get_job(jid)["archived_at"]
    out = desk.job_action({"job": jid, "action": "unarchive"})
    assert out["archived"] is False
    restored = store.get_job(jid)
    assert restored["archived"] is False and restored["archived_at"] == ""


def test_archive_refused_while_side_running(tmp_path):
    store, jid = _new_desk_job(tmp_path, status="running")
    store.update_side(jid, "A", {"status": "running"})
    desk = DeskServer(store)
    with pytest.raises(RuntimeError):
        desk.job_action({"job": jid, "action": "archive"})
    assert store.get_job(jid)["archived"] is False


def test_archived_job_rejects_run_and_collect_actions(tmp_path):
    store, jid = _new_desk_job(tmp_path, archived=True)
    desk = DeskServer(store)
    for action in ("prepare", "enqueue", "collect"):
        with pytest.raises(RuntimeError):
            desk.job_action({"job": jid, "action": action})


def test_archived_job_enqueue_is_ignored(tmp_path):
    store, jid = _new_desk_job(tmp_path, archived=True, baseline_sha="a" * 40)
    desk = DeskServer(store)
    desk.runner.enqueue(jid)
    assert jid not in desk.runner._queued
    assert desk.runner._queue.empty()


def test_archived_flag_survives_in_public_job_and_list(tmp_path):
    store, jid = _new_desk_job(tmp_path, archived=True)
    desk = DeskServer(store)
    # list_jobs still returns archived jobs; filtering is a UI concern.
    assert jid in [j["id"] for j in store.list_jobs()]
    assert desk.public_job(store.get_job(jid))["archived"] is True

def test_prepare_workspaces_ancestry_and_push(baseline_repo, tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "baseline_repo": str(baseline_repo)})
    snap = ws.snapshot_baseline(baseline_repo)
    assert ws.is_sha40(snap["sha"])

    pair_dir = tmp_path / "ws"
    info_a = ws.prepare_side_workspace(baseline_repo, pair_dir / "a", "pair-x-a")
    info_b = ws.prepare_side_workspace(baseline_repo, pair_dir / "b", "pair-x-b")
    assert info_a["head"] == snap["sha"] == info_b["head"]  # same start point

    # simulate the model producing changes on side A
    (Path(info_a["workspace"]) / "engine.py").write_text("print('A')\n", encoding="utf-8")
    fin = ws.finalize_side(info_a["workspace"], "pair-x-a", "A product")
    assert ws.is_sha40(fin["sha"])
    assert ws.is_ancestor(info_a["workspace"], snap["sha"], fin["sha"])
    assert ws.sha_pushed(info_a["workspace"], fin["sha"], "pair-x-a")

    # permalink shape for a real https remote
    _git(["config", "remote.origin.url", "https://github.com/org/repo.git"], info_a["workspace"])
    url = ws.commit_permalink(info_a["workspace"], fin["sha"])
    assert url == f"https://github.com/org/repo/commit/{fin['sha']}"


def _make_session_file(projects_dir: Path, encoded_dir: str, session_id: str, cwd: str, prompt: str):
    d = projects_dir / encoded_dir
    d.mkdir(parents=True, exist_ok=True)
    recs = [
        {"type": "mode", "sessionId": session_id, "cwd": None},
        {"type": "user", "sessionId": session_id, "cwd": cwd,
         "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}},
        {"type": "assistant", "sessionId": session_id, "cwd": cwd, "message": {"content": "done"}},
    ]
    p = d / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")
    return p


def test_find_session_jsonl_by_cwd(tmp_path, monkeypatch):
    home = tmp_path / "claudehome"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    wdir = tmp_path / "ws" / "a"
    wdir.mkdir(parents=True)
    p = _make_session_file(home / "projects", ws._encode_cwd(wdir),
                           "sid-aaa", str(wdir), "build an engine")
    found = ws.find_session_jsonl(wdir)
    assert len(found) == 1 and found[0]["session_id"] == "sid-aaa"
    # a session from another directory must not match
    other = tmp_path / "ws" / "b"
    other.mkdir()
    assert ws.find_session_jsonl(other) == []
    assert p.exists()


def test_clean_path_strips_wrapping_quotes():
    from agent_trace_kit.desk_store import clean_path
    assert clean_path(r'"D:\myprojects\repo"') == r"D:\myprojects\repo"
    assert clean_path(r"'D:\myprojects\repo'") == r"D:\myprojects\repo"
    assert clean_path("  D:\\x  ") == "D:\\x"
    assert clean_path("") == ""


def _write_transcript(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
        encoding="utf-8",
    )
    return path


def _tool_tail_records(prompt: str, final_text: str | None, *, final_is_api_error: bool = False):
    """user prompt -> assistant tool_use -> tool_result -> optional final assistant."""
    recs = [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": prompt}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
    ]
    if final_text is not None:
        if final_is_api_error:
            recs.append({"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 504,
                         "message": {"role": "assistant", "model": "<synthetic>",
                                     "content": [{"type": "text", "text": final_text}]}})
        else:
            recs.append({"type": "assistant", "message": {"role": "assistant",
                                                           "content": [{"type": "text", "text": final_text}]}})
    return recs


def _api_error_record(text: str = "API Error: 504 Gateway Time-out") -> dict:
    return {"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 504,
            "message": {"role": "assistant", "model": "<synthetic>",
                        "content": [{"type": "text", "text": text}]}}


def test_transcript_interruption_classification(tmp_path):
    p = tmp_path / "cut-504.jsonl"
    _write_transcript(p, _tool_tail_records(
        "build it", "API Error: 504 Gateway Time-out", final_is_api_error=True))
    assert ws.transcript_interruption_reason(p) == "api_error"
    assert not ws.session_turn_complete(p)
    assert ws.session_has_assistant(p)

    p2 = tmp_path / "cut-stall.jsonl"
    _write_transcript(p2, _tool_tail_records("build it", None))
    assert ws.transcript_interruption_reason(p2) == "dangling_tool_result"
    assert not ws.session_turn_complete(p2)

    p3 = tmp_path / "complete.jsonl"
    _write_transcript(p3, _tool_tail_records("build it", "全部完成，测试已通过"))
    assert ws.transcript_interruption_reason(p3) is None
    assert ws.session_turn_complete(p3) and ws.session_has_assistant(p3)

    p4 = tmp_path / "empty.jsonl"
    _write_transcript(p4, [{"type": "ai-title", "aiTitle": "x"}])
    assert ws.transcript_interruption_reason(p4) == "no_assistant"
    assert not ws.session_has_assistant(p4)

    # A real model answer that merely quotes the words "API Error" is NOT a cut.
    p5 = tmp_path / "quoted-error.jsonl"
    _write_transcript(p5, _tool_tail_records("document 504s", "API Error 处理说明：收到 504 时应重试。"))
    assert ws.transcript_interruption_reason(p5) is None

    # Trailing synthetic 504 AFTER a normal closing answer = complete turn
    # (CLI exits non-zero on a post-turn auxiliary call, but the product is done).
    recs = _tool_tail_records("build it", "全部完成，测试已通过")
    recs.append(_api_error_record())
    p6 = tmp_path / "trailing-504.jsonl"
    _write_transcript(p6, recs)
    assert ws.transcript_interruption_reason(p6) is None


def _screenshot_companion_records(prompt: str) -> list[dict]:
    """One SDK prompt + tool_result image + Claude Code's isMeta [Image:] companion."""
    return [
        {"type": "user", "promptSource": "sdk", "isSidechain": False,
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t-read", "name": "Read",
             "input": {"file_path": "gui_screenshot.png"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t-read",
             "content": [{"type": "image", "source": {"type": "base64"}}]}]}},
        {"type": "user", "isMeta": True, "turnCompanion": True, "isSidechain": False,
         "message": {"role": "user", "content": (
             "[Image: original 2560x1440, displayed at 2000x1125. "
             "Multiply coordinates by 1.28 to map to original image.]")}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "截图已看过，继续改代码。"}]}},
    ]


def test_jsonl_user_texts_skips_screenshot_meta_companion(tmp_path):
    """Read(screenshot) injects a user [Image:] caption; that is not a second turn."""
    prompt = "我需要一个本地桌面工具，调试设备字节流。"
    p = _write_transcript(tmp_path / "a.jsonl", _screenshot_companion_records(prompt))
    texts = cl._jsonl_user_texts(p)
    assert texts == [prompt]


def test_jsonl_user_texts_still_counts_a_real_second_prompt(tmp_path):
    prompt = "实现一个模块"
    recs = _screenshot_companion_records(prompt)
    recs.append({"type": "user", "isSidechain": False,
                 "message": {"role": "user", "content": "请再加一个导出按钮"}})
    p = _write_transcript(tmp_path / "two-turns.jsonl", recs)
    texts = cl._jsonl_user_texts(p)
    assert texts == [prompt, "请再加一个导出按钮"]


def test_checklist_single_turn_ignores_image_companion(tmp_path):
    store = DeskStore(tmp_path / "desk")
    prompt = "我需要一个本地桌面工具，调试设备字节流。"
    job = store.create_job({"prompt": prompt, "stack": "Python"})
    jsonl = _write_transcript(
        tmp_path / "a.jsonl", _screenshot_companion_records(prompt))
    store.update_side(job["id"], "A", {
        "jsonl_local": str(jsonl), "session_id": "sid-a", "status": "done",
    })
    job = store.get_job(job["id"])
    item = next(x for x in cl.run_checklist(job)["items"] if x["id"] == "A_single_turn")
    assert item["ok"], item["detail"]
    match = next(x for x in cl.run_checklist(job)["items"] if x["id"] == "A_prompt_match")
    assert match["ok"]


def test_jsonl_user_texts_skips_image_caption_without_meta_flags(tmp_path):
    prompt = "实现一个模块"
    recs = [
        {"type": "user", "promptSource": "sdk",
         "message": {"role": "user", "content": prompt}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "[Image: original 800x600, displayed at 800x600.]"}]}},
    ]
    p = _write_transcript(tmp_path / "img-list.jsonl", recs)
    assert cl._jsonl_user_texts(p) == [prompt]


def test_jsonl_user_texts_keeps_sdk_prompt_even_if_meta_flagged(tmp_path):
    prompt = "实现一个模块"
    recs = [{"type": "user", "promptSource": "sdk", "isMeta": True,
             "message": {"role": "user", "content": prompt}}]
    p = _write_transcript(tmp_path / "sdk-meta.jsonl", recs)
    assert cl._jsonl_user_texts(p) == [prompt]


def test_job_list_keeps_cached_checklist_after_job_view(tmp_path):
    """/api/jobs must not drop a just-computed checklist; polling used to wipe it."""
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "stack": "Python"})
    desk = DeskServer(store)
    listed = desk.public_job(store.get_job(job["id"]))
    assert not listed.get("check")
    viewed = desk.job_view(job["id"])
    assert viewed["check"]["items"]
    listed_again = desk.public_job(store.get_job(job["id"]))
    assert listed_again.get("check") == viewed["check"]


def test_job_list_drops_checklist_when_job_updates(tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "stack": "Python"})
    desk = DeskServer(store)
    desk.job_view(job["id"])
    assert desk.public_job(store.get_job(job["id"])).get("check")
    store.update_side(job["id"], "A", {"status": "done"})
    assert not desk.public_job(store.get_job(job["id"])).get("check")


class _FakeJob:
    """No-op stand-in for jobobj.KillJob; records reaping for assertions."""

    def __init__(self, assign_ok: bool = True):
        self.alive = True
        self.reason = ""
        self.assign_ok = assign_ok
        self.added: list[int] = []
        self.terminated = 0
        self.closed = 0

    def add_pid(self, pid: int) -> bool:
        self.added.append(pid)
        return self.assign_ok

    def terminate(self, exit_code: int = 1) -> None:
        self.terminated += 1

    def close(self) -> None:
        self.closed += 1


@pytest.fixture(autouse=True)
def _fake_kill_job(monkeypatch, tmp_path):
    """Never create real Windows Job Objects in the unit suite."""
    from agent_trace_kit import runner as rn
    monkeypatch.setattr(rn.PairRunner, "_new_kill_job", staticmethod(_FakeJob))
    from agent_trace_kit import run_policy
    monkeypatch.setattr(run_policy, "preflight", lambda *a: {"capability_check": "fixture"})
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user-home"))


class _FakeProc:
    """Minimal subprocess.Popen double: fixed stdout lines, already exited."""

    def __init__(self, lines: list[str], returncode: int):
        self.stdout = [json.dumps({"type": "system", "subtype": "init", "tools": ["Bash", "Read", "Write"]}) + "\n", *lines]
        self.pid = 424242
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def _run_one_attempt(store, tmp_path, transcript_path: Path,
                     stream_lines: list[str], returncode: int, monkeypatch):
    from agent_trace_kit import runner as rn
    if not store.list_jobs():
        store.create_job({"prompt": "实现一个模块"})
    job_id = store.list_jobs()[0]["id"]
    wdir = tmp_path / "w"
    wdir.mkdir(exist_ok=True)
    fake_proc = _FakeProc(stream_lines, returncode)
    monkeypatch.setattr(rn.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(ws, "find_session_jsonl",
                        lambda w, **kwargs: [{"path": str(transcript_path), "session_id": "sid-x",
                                    "mtime": "9999999999"}])
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(lambda w, **kwargs: 0.0))
    evidence = store.evidence_dir(job_id)
    return rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        evidence / "attempts" / "a-01-stream.jsonl",
        timeout=60, stall_after=30, poll_every=5, attempt=1,
    )


def test_attempt_reaps_job_on_clean_finish(tmp_path, monkeypatch):
    """Even when claude finishes normally, any background server it spawned via
    `&` must be reaped: the kill-on-close job is terminated AND closed once."""
    from agent_trace_kit import runner as rn
    store = DeskStore(tmp_path / "desk")
    store.create_job({"prompt": "实现一个模块"})
    job_id = store.list_jobs()[0]["id"]
    wdir = tmp_path / "w"
    wdir.mkdir(exist_ok=True)
    complete = _write_transcript(
        tmp_path / "complete.jsonl", _tool_tail_records("实现一个模块", "完成"))
    fake_proc = _FakeProc([_result_line(False)], 0)
    shared_job = _FakeJob()
    monkeypatch.setattr(rn.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(rn.PairRunner, "_new_kill_job", staticmethod(lambda: shared_job))
    monkeypatch.setattr(ws, "find_session_jsonl",
                        lambda w, **kwargs: [{"path": str(complete), "session_id": "sid-x", "mtime": "9"}])
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(lambda w, **kwargs: 0.0))
    evidence = store.evidence_dir(job_id)
    rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        evidence / "attempts" / "a-01-stream.jsonl",
        timeout=60, stall_after=30, poll_every=5, attempt=1)
    assert shared_job.added == [fake_proc.pid]
    assert shared_job.terminated == 1
    assert shared_job.closed == 1


def test_attempt_falls_back_to_taskkill_when_job_unavailable(tmp_path, monkeypatch):
    """If the process cannot be assigned to a job, cleanup closes the disabled
    job. Windows avoids an exited PID; POSIX reaps the owned process group."""
    from agent_trace_kit import runner as rn
    from agent_trace_kit import procmon
    store = DeskStore(tmp_path / "desk")
    store.create_job({"prompt": "实现一个模块"})
    job_id = store.list_jobs()[0]["id"]
    wdir = tmp_path / "w"
    wdir.mkdir(exist_ok=True)
    complete = _write_transcript(
        tmp_path / "complete.jsonl", _tool_tail_records("实现一个模块", "完成"))
    fake_proc = _FakeProc([_result_line(False)], 0)  # already exited
    disabled_job = _FakeJob(assign_ok=False)
    killed = []
    monkeypatch.setattr(rn.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(rn.PairRunner, "_new_kill_job", staticmethod(lambda: disabled_job))
    monkeypatch.setattr(procmon, "kill_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(ws, "find_session_jsonl",
                        lambda w, **kwargs: [{"path": str(complete), "session_id": "sid-x", "mtime": "9"}])
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(lambda w, **kwargs: 0.0))
    evidence = store.evidence_dir(job_id)
    rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        evidence / "attempts" / "a-01-stream.jsonl",
        timeout=60, stall_after=30, poll_every=5, attempt=1)
    assert disabled_job.terminated == 0
    assert disabled_job.closed == 1
    assert killed == ([] if rn.os.name == "nt" else [fake_proc.pid])


def _result_line(is_error: bool) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": is_error,
                       "num_turns": 3, "total_cost_usd": 0.0,
                       "result": "API Error: 429 rate limited" if is_error else "完成"}) + "\n"


class _RecordingStdin:
    def __init__(self):
        self.writes: list[str] = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


def test_long_prompt_is_written_to_stdin_and_closed(tmp_path, monkeypatch):
    """Prompts >= 1500 chars go via stdin=PIPE; forgetting to write/close it
    leaves claude blocked on read() until the stall watchdog (looks hung)."""
    from agent_trace_kit import runner as rn
    prompt = "implement the module. " + ("detailed requirement. " * 80)
    assert len(prompt) >= 1500
    store = DeskStore(tmp_path / "desk")
    store.create_job({"prompt": prompt})
    job_id = store.list_jobs()[0]["id"]
    wdir = tmp_path / "w"
    wdir.mkdir(exist_ok=True)
    captured: dict = {}

    class CapturingPopen(_FakeProc):
        def __init__(self, args, **kw):
            captured["args"] = args
            captured["stdin_kw"] = kw.get("stdin")
            super().__init__([_result_line(False)], 0)
            self.stdin = _RecordingStdin()
            captured["pipe"] = self.stdin

    complete = _write_transcript(
        tmp_path / "complete.jsonl", _tool_tail_records(prompt, "完成"))
    monkeypatch.setattr(rn.subprocess, "Popen", lambda args, **kw: CapturingPopen(args, **kw))
    monkeypatch.setattr(ws, "find_session_jsonl",
                        lambda w, **kwargs: [{"path": str(complete), "session_id": "sid-x", "mtime": "9"}])
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(lambda w, **kwargs: 0.0))
    evidence = store.evidence_dir(job_id)
    rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        evidence / "attempts" / "a-01-stream.jsonl",
        timeout=60, stall_after=30, poll_every=5, attempt=1)
    assert captured["stdin_kw"] is rn.subprocess.PIPE
    assert prompt not in captured["args"]
    assert captured["pipe"].writes == [prompt]
    assert captured["pipe"].closed is True


def test_abort_survives_wait_timeout_after_reap(tmp_path, monkeypatch):
    """After 中止, proc.wait(timeout=30) must not raise TimeoutExpired out of
    _run_attempt — that used to fail the side with an engine exception and
    skip the rest of cleanup."""
    from agent_trace_kit import runner as rn
    from agent_trace_kit import procmon
    store = DeskStore(tmp_path / "desk")
    store.create_job({"prompt": "实现一个模块"})
    job_id = store.list_jobs()[0]["id"]
    wdir = tmp_path / "w"
    wdir.mkdir(exist_ok=True)

    class HungProc(_FakeProc):
        def __init__(self, lines, returncode):
            super().__init__(lines, returncode)
            self.returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)

    fake_proc = HungProc([], None)
    monkeypatch.setattr(rn.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(procmon, "kill_tree", lambda pid: None)
    monkeypatch.setattr(ws, "find_session_jsonl", lambda w, **kwargs: [])
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(lambda w, **kwargs: 0.0))
    evidence = store.evidence_dir(job_id)
    runner = rn.PairRunner(store)
    runner._abort.add(f"{job_id}/A")
    out = runner._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        evidence / "attempts" / "a-01-stream.jsonl",
        timeout=60, stall_after=30, poll_every=1, attempt=1)
    assert out["aborted"] is True
    assert out["completed"] is False


def test_stall_survives_wait_timeout_after_reap(tmp_path, monkeypatch):
    """Stall path must use _wait_proc too; TimeoutExpired used to skip retry."""
    from agent_trace_kit import runner as rn
    from agent_trace_kit import procmon
    store = DeskStore(tmp_path / "desk")
    store.save_settings({"stall_seconds": 1})
    store.create_job({"prompt": "实现一个模块"})
    job_id = store.list_jobs()[0]["id"]
    wdir = tmp_path / "w"
    wdir.mkdir(exist_ok=True)

    class HungProc(_FakeProc):
        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)

    fake_proc = HungProc([], None)
    monkeypatch.setattr(rn.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(procmon, "kill_tree", lambda pid: None)
    monkeypatch.setattr(procmon, "snapshot", lambda: {})
    monkeypatch.setattr(procmon, "descendants", lambda *a, **k: set())
    monkeypatch.setattr(procmon, "tree_busy", lambda *a, **k: False)
    monkeypatch.setattr(ws, "find_session_jsonl", lambda w, **kwargs: [])
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(lambda w, **kwargs: 0.0))
    evidence = store.evidence_dir(job_id)
    out = rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        evidence / "attempts" / "a-01-stream.jsonl",
        timeout=60, stall_after=1, poll_every=1, attempt=1)
    assert out["aborted"] is False
    assert out["completed"] is False
    assert "静默断流" in (out.get("failure") or "")


def test_stall_after_transcript_stops_updating(tmp_path, monkeypatch):
    """A single early transcript write must not disable the stall watchdog."""
    from agent_trace_kit import runner as rn
    from agent_trace_kit import procmon
    store = DeskStore(tmp_path / "desk")
    store.save_settings({"stall_seconds": 1})
    store.create_job({"prompt": "实现一个模块"})
    job_id = store.list_jobs()[0]["id"]
    wdir = tmp_path / "w"
    wdir.mkdir(exist_ok=True)
    calls = {"n": 0}

    class HungProc(_FakeProc):
        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)

    def fake_mtime(_w, **kwargs):
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 100.0

    monkeypatch.setattr(rn.subprocess, "Popen", lambda *a, **k: HungProc([], None))
    monkeypatch.setattr(rn.time, "sleep", lambda *_: None)
    monkeypatch.setattr(procmon, "kill_tree", lambda pid: None)
    monkeypatch.setattr(procmon, "snapshot", lambda: {})
    monkeypatch.setattr(procmon, "descendants", lambda *a, **k: set())
    monkeypatch.setattr(procmon, "tree_busy", lambda *a, **k: False)
    monkeypatch.setattr(ws, "find_session_jsonl", lambda w, **kwargs: [])
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(fake_mtime))
    evidence = store.evidence_dir(job_id)
    out = rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        evidence / "attempts" / "a-01-stream.jsonl",
        timeout=60, stall_after=1, poll_every=1, attempt=1)
    assert "静默断流" in (out.get("failure") or "")
    assert calls["n"] >= 3


def test_zero_stall_waits_for_process_timeout(tmp_path, monkeypatch):
    """stall_seconds=0 must not kill a live Claude; only the side timeout may."""
    from agent_trace_kit import runner as rn
    from agent_trace_kit import procmon
    store = DeskStore(tmp_path / "desk")
    store.save_settings({"stall_seconds": 0})
    store.create_job({"prompt": "实现一个模块"})
    job_id = store.list_jobs()[0]["id"]
    wdir = tmp_path / "w"
    wdir.mkdir(exist_ok=True)
    clock = {"t": 1000.0}

    class HungProc(_FakeProc):
        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)

    def fake_time():
        clock["t"] += 1
        return clock["t"]

    monkeypatch.setattr(rn.time, "time", fake_time)
    monkeypatch.setattr(rn.time, "sleep", lambda *_: None)
    monkeypatch.setattr(rn.subprocess, "Popen", lambda *a, **k: HungProc([], None))
    monkeypatch.setattr(procmon, "kill_tree", lambda pid: None)
    monkeypatch.setattr(procmon, "snapshot", lambda: {})
    monkeypatch.setattr(procmon, "descendants", lambda *a, **k: set())
    monkeypatch.setattr(procmon, "tree_busy", lambda *a, **k: False)
    monkeypatch.setattr(ws, "find_session_jsonl", lambda w, **kwargs: [])
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(lambda w, **kwargs: 0.0))
    evidence = store.evidence_dir(job_id)
    out = rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        evidence / "attempts" / "a-01-stream.jsonl",
        timeout=3, stall_after=1, poll_every=1, attempt=1)
    assert "静默断流" not in (out.get("failure") or "")
    assert "总超时" in (out.get("failure") or "")


def test_recover_auto_retries_when_baseline_exists(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "baseline_repo": str(tmp_path)})
    store.update_job(job["id"], {"baseline_sha": "a" * 40, "status": "running"})
    store.update_side(job["id"], "A", {"status": "running"})
    recopied = []
    monkeypatch.setattr(PairRunner, "_fresh_workspace",
                        lambda self, j, s, **k: recopied.append(s) or {})
    runner = PairRunner(store)
    out = runner.recover()
    assert f"{job['id']}/A" in out["recovered"]
    assert recopied == ["A"]
    assert store.get_job(job["id"])["sides"]["A"]["status"] == "preparing"
    runner._resume_incomplete_jobs()
    assert job["id"] in runner._queued


def test_recover_skips_review_locked_jobs(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "baseline_repo": str(tmp_path)})
    store.update_job(job["id"], {
        "baseline_sha": "a" * 40, "status": "running",
        "review": {"locked_at": "2026-01-01T00:00:00+00:00"},
    })
    store.update_side(job["id"], "A", {"status": "running"})
    recopied = []
    monkeypatch.setattr(PairRunner, "_fresh_workspace",
                        lambda self, j, s, **k: recopied.append(s) or {})
    PairRunner(store).recover()
    assert recopied == []
    assert store.get_job(job["id"])["sides"]["A"]["status"] == "failed"


def test_run_side_stops_when_wall_exhausted(tmp_path, monkeypatch):
    from agent_trace_kit import runner as rn
    store = DeskStore(tmp_path / "desk")
    store.save_settings({"side_wall_budget_seconds": 60, "side_max_attempts": 12})
    job = store.create_job({"prompt": "实现一个模块"})
    wdir = tmp_path / "w"
    wdir.mkdir()
    store.update_side(job["id"], "A", {"workspace": str(wdir)})
    ticks = {"n": 0}

    def fake_time():
        ticks["n"] += 1
        return 1_000_000.0 if ticks["n"] == 1 else 1_000_000.0 + 120

    monkeypatch.setattr(rn.time, "time", fake_time)
    monkeypatch.setattr(rn.time, "sleep", lambda *_: None)

    def boom(*_a, **_k):
        raise AssertionError("must not launch after the wall budget")

    monkeypatch.setattr(rn.PairRunner, "_run_attempt", boom)
    rn.PairRunner(store).run_side(job["id"], "A")
    side = store.get_job(job["id"])["sides"]["A"]
    assert side["status"] == "failed"
    assert "预算" in (side.get("error") or "")


def test_abort_during_checks_marks_side_failed(tmp_path, monkeypatch):
    """A completed attempt + 中止 during check_commands must not stay 完成."""
    from agent_trace_kit import runner as rn
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "实现一个模块", "check_commands": "pytest -q"})
    wdir = tmp_path / "ws"
    wdir.mkdir()
    store.update_side(job["id"], "A", {"workspace": str(wdir)})
    good = _write_transcript(tmp_path / "good.jsonl",
                             _tool_tail_records("实现一个模块", "完成"))
    runner = rn.PairRunner(store)

    def fake_attempt(*_a, **_k):
        return {
            "code": 0, "new_session": {"path": str(good), "session_id": "sid-ok"},
            "completed": True, "aborted": False, "summary": "#1 完成",
            "failure": "", "stalls": 0, "session_path": str(good), "session_id": "sid-ok",
        }

    def fake_checks(*_a, **_k):
        runner.abort_side(job["id"], "A")
        return [{"command": "pytest -q", "exit_code": None, "aborted": True}]

    monkeypatch.setattr(rn.PairRunner, "_run_attempt", fake_attempt)
    monkeypatch.setattr(rn, "run_check_commands", fake_checks)
    monkeypatch.setattr(ws, "finalize_side",
                        lambda *_a, **_k: {"sha": "a" * 40, "url": "u", "branch": "b"})
    runner.run_side(job["id"], "A")
    side = store.get_job(job["id"])["sides"]["A"]
    assert side["status"] == "failed"
    assert "中止" in side["error"]


def test_runner_passes_permission_mode(tmp_path, monkeypatch):
    """Default is bypassPermissions (acceptEdits auto-denies all Bash under -p);
    a settings override flows through to the CLI argv."""
    from agent_trace_kit import runner as rn
    store = DeskStore(tmp_path / "desk")
    store.create_job({"prompt": "实现一个模块"})
    job_id = store.list_jobs()[0]["id"]
    wdir = tmp_path / "w"
    wdir.mkdir(exist_ok=True)
    captured = {}

    class CapturingPopen(_FakeProc):
        def __init__(self, args, **kw):
            captured["args"] = args
            super().__init__([_result_line(False)], 0)

    def fake_popen(args, **kw):
        return CapturingPopen(args, **kw)

    complete = _write_transcript(
        tmp_path / "complete.jsonl", _tool_tail_records("实现一个模块", "完成"))
    monkeypatch.setattr(rn.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ws, "find_session_jsonl",
                        lambda w, **kwargs: [{"path": str(complete), "session_id": "sid-x", "mtime": "9"}])
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(lambda w, **kwargs: 0.0))
    evidence = store.evidence_dir(job_id)
    rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        evidence / "attempts" / "a-01-stream.jsonl",
        timeout=60, stall_after=30, poll_every=5, attempt=1)
    assert captured["args"][captured["args"].index("--permission-mode") + 1] == "bypassPermissions"

    store.save_settings({"permission_mode": "plan"})
    rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        evidence / "attempts" / "a-01-stream.jsonl",
        timeout=60, stall_after=30, poll_every=5, attempt=1)
    assert captured["args"][captured["args"].index("--permission-mode") + 1] == "plan"


def test_claude_popen_hides_console(tmp_path, monkeypatch):
    """Claude gets one hidden console so each Bash powershell does not pop a window.

    CREATE_NO_WINDOW hides claude itself and then Windows gives every console
    grandchild its own visible window. A console created already hidden is
    inherited instead.
    """
    from agent_trace_kit import procmon
    from agent_trace_kit import runner as rn
    store = DeskStore(tmp_path / "desk")
    store.create_job({"prompt": "实现一个模块"})
    job_id = store.list_jobs()[0]["id"]
    wdir = tmp_path / "w"
    wdir.mkdir(exist_ok=True)
    captured = {}

    class CapturingPopen(_FakeProc):
        def __init__(self, args, **kw):
            captured.update(kw)
            super().__init__([_result_line(False)], 0)

    complete = _write_transcript(
        tmp_path / "complete.jsonl", _tool_tail_records("实现一个模块", "完成"))
    monkeypatch.setattr(rn.subprocess, "Popen", lambda args, **kw: CapturingPopen(args, **kw))
    monkeypatch.setattr(ws, "find_session_jsonl",
                        lambda w, **kwargs: [{"path": str(complete), "session_id": "sid-x", "mtime": "9"}])
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(lambda w, **kwargs: 0.0))
    evidence = store.evidence_dir(job_id)
    rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        evidence / "attempts" / "a-01-stream.jsonl",
        timeout=60, stall_after=30, poll_every=5, attempt=1)
    expected = procmon.hidden_console_kwargs()
    assert captured.get("creationflags", 0) == expected.get("creationflags", 0)
    if "startupinfo" in expected:
        si = captured["startupinfo"]
        assert si.dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert si.wShowWindow == subprocess.SW_HIDE
        assert captured["creationflags"] & subprocess.CREATE_NEW_CONSOLE
        assert not (captured["creationflags"] & subprocess.CREATE_NO_WINDOW)


def test_runner_accepts_exit1_trailing504_after_clean_turn(tmp_path, monkeypatch):
    """exit 1 + 504 stream line + clean result + complete transcript = success."""
    store = DeskStore(tmp_path / "desk")
    complete = _write_transcript(
        tmp_path / "complete.jsonl", _tool_tail_records("实现一个模块", "完成，测试通过"))
    out = _run_one_attempt(store, tmp_path, complete,
                           ["API Error: 504 trailing auxiliary failure\n", _result_line(False)], 1,
                           monkeypatch)
    assert out["completed"] is True and out["code"] == 1


def test_runner_rejects_result_is_error(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    complete = _write_transcript(
        tmp_path / "complete.jsonl", _tool_tail_records("实现一个模块", "完成"))
    out = _run_one_attempt(store, tmp_path, complete, [_result_line(True)], 1, monkeypatch)
    assert out["completed"] is False
    assert "result 错误" in out["failure"]


def test_runner_rejects_midturn_cut_even_if_stream_says_success(tmp_path, monkeypatch):
    """f02ec37c: stream says success but transcript tail is a terminal 504."""
    store = DeskStore(tmp_path / "desk")
    cut = _write_transcript(
        tmp_path / "cut.jsonl",
        _tool_tail_records("实现一个模块", "API Error: 504 Gateway Time-out",
                           final_is_api_error=True))
    out = _run_one_attempt(store, tmp_path, cut, [_result_line(False)], 0, monkeypatch)
    assert out["completed"] is False
    assert "截断" in out["failure"]


def test_retry_backoff_grows_and_caps():
    import random as _random
    from agent_trace_kit.runner import retry_backoff_seconds
    rng = _random.Random(0)
    waits = [retry_backoff_seconds(a, rng) for a in range(2, 8)]
    assert waits[0] > 0
    assert waits[0] < waits[1] < waits[2]
    assert all(w <= 120 for w in waits)
    assert waits[-1] <= 120


def test_checklist_blocks_cut_transcript(tmp_path, baseline_repo, monkeypatch):
    """A done side whose transcript tail is a gateway 504 must block export."""
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "实现一个模块", "stack": "Python",
                            "baseline_repo": str(baseline_repo)})
    ws.snapshot_baseline(baseline_repo)
    info = ws.prepare_side_workspace(baseline_repo, tmp_path / "ws" / "a", "pair-x-a")
    fin = ws.finalize_side(info["workspace"], "pair-x-a", "A product")
    cut_jsonl = _write_transcript(
        tmp_path / "ev" / "a-cut.jsonl",
        _tool_tail_records(job["prompt"], "API Error: 504 Gateway Time-out",
                           final_is_api_error=True))
    store.update_side(job["id"], "A", {
        "workspace": info["workspace"], "session_id": "sid-cut",
        "jsonl_local": str(cut_jsonl), "head_sha": fin["sha"],
        "head_url": f"https://github.com/org/repo/commit/{fin['sha']}",
        "pushed": True, "status": "done",
        "trace_url": "https://oss.example.com/sid-cut.jsonl",
    })
    job = store.get_job(job["id"])
    blocked = {x["id"]: x for x in cl.run_checklist(job)["items"]
               if x["blocking"] and not x["ok"]}
    assert "A_trace_complete" in blocked
    # declaring the pair an engineering fault downgrades it to non-blocking audit
    store.update_review(job["id"], {"validity": "作废-工程故障"})
    job = store.get_job(job["id"])
    a_item = next(x for x in cl.run_checklist(job)["items"] if x["id"] == "A_trace_complete")
    assert not a_item["ok"] and not a_item["blocking"]



def test_checklist_blocks_then_passes(tmp_path, baseline_repo, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({
        "prompt": "实现一个流处理引擎",
        "stack": "Go",
        "baseline_repo": str(baseline_repo),
    })
    report = cl.run_checklist(job)
    assert not report["ready"]
    assert report["blocking_count"] > 0

    # fill the job the way the runner would after two successful sides
    snap = ws.snapshot_baseline(baseline_repo)
    pair_dir = tmp_path / "ws"
    home = tmp_path / "ch"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    prompt = job["prompt"]
    sides = {}
    for name in ("A", "B"):
        info = ws.prepare_side_workspace(baseline_repo, pair_dir / name.lower(), f"pair-x-{name.lower()}")
        (Path(info["workspace"]) / f"{name.lower()}.py").write_text("print(1)\n", encoding="utf-8")
        fin = ws.finalize_side(info["workspace"], info["branch"], f"{name} product")
        sid = f"sid-{name.lower()}-111"
        jsonl = _make_session_file(home / "projects", ws._encode_cwd(info["workspace"]),
                                   sid, info["workspace"], prompt + " 请严格测试")
        # prompt is contained in the user message (allowance: checklist uses substring)
        sides[name] = {**info, "fin": fin, "jsonl": jsonl, "sid": sid}

    # rewrite the user message to equal the exact prompt for the strict match
    for name in ("A", "B"):
        recs = [json.loads(l) for l in sides[name]["jsonl"].read_text(encoding="utf-8").splitlines()]
        recs[1]["message"]["content"][0]["text"] = prompt
        recs[2]["message"]["model"] = ws.PINNED_MODEL
        sides[name]["jsonl"].write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")

    for name in ("A", "B"):
        store.update_side(job["id"], name, {
            "workspace": sides[name]["workspace"], "session_id": sides[name]["sid"],
            "initial_sha": snap["sha"],
            "jsonl_local": str(sides[name]["jsonl"]),
            "head_sha": sides[name]["fin"]["sha"], "head_url":
            f"https://github.com/org/repo/commit/{sides[name]['fin']['sha']}",
            "pushed": True, "status": "done",
            "trace_url": f"https://oss.example.com/{sides[name]['sid']}.jsonl",
            "video_local": str(tmp_path / f"{name}.mp4"),
        })
        (tmp_path / f"{name}.mp4").write_bytes(b"FAKEMP4")
    job = store.update_job(job["id"], {
        "baseline_sha": snap["sha"],
        "baseline_url": f"https://github.com/org/repo/commit/{snap['sha']}",
        "baseline_pushed": True, "harness_version": "2.1.260",
    })
    job = store.update_review(job["id"], {"validity": "有效", "conclusion": "A 更好", "reason": "A 恢复路径无死锁，" * 5, "reviewer": "Vincent",
                                          "a_delivery_score": "5", "a_delivery_description": "实际产物完成主要流程，异常恢复没有发现缺陷。",
                                          "b_delivery_score": "3", "b_delivery_description": "边界输入恢复仍有缺陷，影响实际使用。"})
    report = cl.run_checklist(job)
    blockers = [x["id"] for x in report["items"] if x["blocking"] and not x["ok"]]
    assert blockers == [], blockers
    assert report["ready"]


def test_export_tsv_headers_and_strict_gate(tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p"})
    with pytest.raises(ValueError):
        export_tsv.export_tsv(job, tmp_path / "x.tsv", strict=True)
    # Feishu paste row: 26 columns, no 有效性 (that field is desk-internal).
    assert len(export_tsv.HEADERS) == 26
    assert export_tsv.HEADERS[0] == "User Prompt"
    assert export_tsv.HEADERS[1] == "提交人"
    assert export_tsv.HEADERS[13:15] == ["A - 交付完整性（1-5）", "A - 交付完整性描述"]
    assert export_tsv.HEADERS[19:21] == ["B - 交付完整性（1-5）", "B - 交付完整性描述"]
    assert export_tsv.HEADERS[21] == "GSB 结论"
    assert "有效性" not in export_tsv.HEADERS
    assert export_tsv.HEADERS[23:25] == ["内部质检", "质检反馈"]
    assert export_tsv.HEADERS[-1] == "备注"


def test_export_tsv_paste_row_aligns_with_feishu(tmp_path):
    """Copy-paste payload is one data row: B-录屏 afterwards is GSB 结论, not 有效."""
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({
        "prompt": "我需要一个本地桌面工具，调试设备字节流。",
        "task_type": "0-1代码生成",
        "harness": "Claude Code",
        "difficulty": "困难",
        "stack": "Python, Tkinter",
        "os_name": "Windows",
        "repro_level": "无外部依赖",
    })
    store.update_job(job["id"], {
        "baseline_url": "https://github.com/StatXzy7/binary-frame-studio/commit/" + "a" * 40,
    })
    store.update_side(job["id"], "A", {
        "session_id": "78533254-ab77-463e-9f63-d6935fdba919",
        "trace_url": "https://example.com/a.jsonl",
        "head_url": "https://github.com/StatXzy7/binary-frame-studio/commit/" + "b" * 40,
        "video_url": "https://example.com/a.mp4",
    })
    store.update_side(job["id"], "B", {
        "session_id": "191e76f6-a158-4062-be6e-5a322dea34d9",
        "trace_url": "https://example.com/b.jsonl",
        "head_url": "https://github.com/StatXzy7/binary-frame-studio/commit/" + "c" * 40,
        "video_url": "https://example.com/b.mp4",
    })
    reason = "B recovered both frames after a bad checksum while A swallowed the embedded frame."
    job = store.update_review(job["id"], {
        "validity": "有效",
        "conclusion": "B 更好",
        "reason": reason,
        "reviewer": "徐子扬",
        "a_delivery_score": "5", "a_delivery_description": "A 的产物能处理异常校验并输出正确字节。",
        "b_delivery_score": "4", "b_delivery_description": "B 的产物基本可用，但边界恢复有缺陷。",
    })
    result = export_tsv.export_tsv(job, tmp_path / "x.tsv", strict=False)
    rows = list(csv.reader(io.StringIO(result["tsv"]), dialect="excel-tab"))
    assert len(rows) == 1, "header row must not be copied into Feishu"
    row = rows[0]
    assert len(row) == len(export_tsv.HEADERS)
    assert row[0] == "我需要一个本地桌面工具，调试设备字节流。"
    assert row[1] == "徐子扬"
    assert row[2] == "0-1代码生成"
    expected_os = "MacOS/Linux" if job["os_name"] in {"Linux", "Darwin"} else job["os_name"]
    assert row[6] == expected_os
    assert row[9] == "78533254-ab77-463e-9f63-d6935fdba919"
    assert row[12] == "https://example.com/a.mp4"
    assert row[13:15] == ["5", "A 的产物能处理异常校验并输出正确字节。"]
    assert row[15] == "191e76f6-a158-4062-be6e-5a322dea34d9"
    assert row[18] == "https://example.com/b.mp4"
    assert row[19:21] == ["4", "B 的产物基本可用，但边界恢复有缺陷。"]
    assert row[21] == "B 更好"
    assert row[22] == reason
    assert row[23:] == ["", "", ""]
    assert result["tsv"].count("\t") == len(export_tsv.HEADERS) - 1


def test_delivery_quality_new_jobs_required_old_jobs_remain_blank(tmp_path):
    from agent_trace_kit import desk as desk_mod
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p"})
    desk = desk_mod.DeskServer(store)
    for name in ("A", "B"):
        trace = _write_transcript(tmp_path / f"{name}.jsonl", _tool_tail_records("p", "完成"))
        store.update_side(job["id"], name, {"jsonl_local": str(trace)})
    review = {"job": job["id"], "validity": "有效", "conclusion": "Same",
              "reason": "两侧产物都能完成主要任务，实际运行中的边界处理也基本一致。" * 3,
              "ai_confirmed": True, "lock": True}
    with pytest.raises(RuntimeError, match="A 交付完整性"):
        desk.review_save(review)
    review.update({"a_delivery_score": "6", "a_delivery_description": "有具体缺陷",
                   "b_delivery_score": "3", "b_delivery_description": "有具体缺陷"})
    with pytest.raises(RuntimeError, match="只能填写 1-5"):
        desk.review_save(review)
    review["a_delivery_score"] = "4"
    desk.review_save(review)
    saved = store.get_job(job["id"])
    assert saved["review"]["a_delivery_score"] == "4"
    assert saved["review"]["b_delivery_description"] == "有具体缺陷"

    legacy = store.create_job({"prompt": "old"})
    for name in ("A", "B"):
        trace = _write_transcript(tmp_path / f"old-{name}.jsonl", _tool_tail_records("old", "完成"))
        legacy["sides"][name]["jsonl_local"] = str(trace)
    legacy.pop("delivery_quality_required")
    legacy["review"].pop("a_delivery_score")
    legacy["review"].pop("a_delivery_description")
    legacy["review"].pop("b_delivery_score")
    legacy["review"].pop("b_delivery_description")
    store._atomic_write_json(store.job_path(legacy["id"]), legacy)
    desk.review_save({**review, "job": legacy["id"],
                      "a_delivery_score": "", "a_delivery_description": "",
                      "b_delivery_score": "", "b_delivery_description": ""})
    old_job = store.get_job(legacy["id"])
    row = export_tsv.job_row(old_job)
    assert row[13:15] == ["", ""]
    assert row[19:21] == ["", ""]


def test_checklist_voided_pair_skips_gsb_and_run_blockers(tmp_path):
    """作废-工程故障: a failed side without a GSB judgement still exports (audit record)."""
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "stack": "Go"})
    store.update_side(job["id"], "A", {"status": "failed", "error": "claude crashed 中断"})
    store.update_side(job["id"], "B", {"status": "failed", "error": "claude crashed 中断"})
    job = store.update_review(job["id"], {
        "validity": "作废-工程故障", "reviewer": "徐子扬",
    })
    report = cl.run_checklist(job)
    blocking_ids = {x["id"] for x in report["items"] if x["blocking"] and not x["ok"]}
    assert "conclusion" not in blocking_ids
    assert "reason" not in blocking_ids
    assert "A_run" not in blocking_ids and "B_run" not in blocking_ids
    # validity itself must be selected, and an unselected one still blocks
    assert "validity" not in blocking_ids
    job2 = store.create_job({"prompt": "p2", "stack": "Go"})
    assert any(x["id"] == "validity" and x["blocking"] and not x["ok"]
               for x in cl.run_checklist(job2)["items"])


def test_oss_sigv4_request_and_public_url(tmp_path):
    received = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_PUT(self):
            length = int(self.headers.get("Content-Length", "0"))
            received["body"] = self.rfile.read(length)
            received["auth"] = self.headers.get("Authorization", "")
            received["amz_date"] = self.headers.get("X-Amz-Date", "")
            self.send_response(200); self.send_header("ETag", '"abc"'); self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), H)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
    try:
        cfg = oss_mod.OssConfig(
            endpoint=f"http://127.0.0.1:{port}", bucket="bk",
            access_key_id="JDC_TEST", secret_access_key="secret", region="cn-north-1",
        )
        f = tmp_path / "oss.bin"
        f.write_bytes(b"hello-oss")
        try:
            info = oss_mod.upload_file(cfg, f, "pairwise/sid.jsonl")
        finally:
            f.unlink(missing_ok=True)
        assert received["body"] == b"hello-oss"
        assert received["auth"].startswith("AWS4-HMAC-SHA256 Credential=JDC_TEST/")
        assert "/cn-north-1/s3/aws4_request, " in received["auth"]
        assert info["url"].endswith("/bk/pairwise/sid.jsonl")
        assert info["size"] == 9
    finally:
        server.shutdown()


def test_oss_config_from_secrets_file(tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text(
        "# comment\nOSS_ACCESS_KEY_ID=\"JDC_X\"\nOSS_SECRET_ACCESS_KEY=sec\n", encoding="utf-8")
    mapping = {"oss_endpoint": "https://s3.cn-north-1.jdcloud-oss.com", "oss_bucket": "b1"}
    cfg = oss_mod.config_from_mapping(mapping, secrets)
    assert cfg is not None and cfg.access_key_id == "JDC_X" and cfg.bucket == "b1"
    assert oss_mod.config_from_mapping({"oss_bucket": ""}, secrets) is None


# ---------- one-click baseline provisioning (ghutil + workspace + runner) ----------

def test_repo_name_validation():
    from agent_trace_kit import ghutil
    assert ghutil.validate_repo_name("rate-limiter_lib") == ""
    assert ghutil.validate_repo_name("a") == ""
    assert ghutil.validate_repo_name("") != ""
    assert ghutil.validate_repo_name("bad name") != ""       # space
    assert ghutil.validate_repo_name("bad/name") != ""
    assert ghutil.validate_repo_name("con") != ""            # Windows reserved
    assert ghutil.validate_repo_name("con.txt") != ""        # reserved base + extension
    assert ghutil.validate_repo_name("nul.md") != ""
    assert ghutil.validate_repo_name("-leaddash") != ""      # option-injection guard
    assert ghutil.validate_repo_name("a..b") != ""
    assert ghutil.validate_repo_name("trailing.") != ""
    assert ghutil.validate_repo_name("x" * 101) != ""


def test_render_readme_default_blurb_contains_title():
    from agent_trace_kit import ghutil
    out = ghutil.render_readme("my-task", "")
    assert out.startswith("# my-task\n") and "基线" in out
    assert "一句话说明" in ghutil.render_readme("r", "一句话说明")


class _FakeGh:
    """Scripted gh stand-in: records create calls, returns None or a repo on view."""

    def __init__(self, existing: "ghutil.RemoteRepo | None" = None, login: str = "StatXzy7",
                 url: str = ""):
        self._existing = existing
        self._login = login
        self._url = url
        self.created: list[tuple[str, str, bool]] = []

    def viewer_login(self) -> str:
        return self._login

    def repo_view(self, owner: str, name: str):
        return self._existing

    def repo_create(self, owner: str, name: str, description: str, private: bool):
        self.created.append((name, description, private))
        repo = ghutil.RemoteRepo(
            name=name, owner=owner,
            url=self._url or f"https://github.com/{owner}/{name}",
            default_branch="main", visibility="PRIVATE" if private else "PUBLIC",
        )
        self._existing = repo
        return repo


def test_ensure_remote_repo_creates_when_missing_and_reuses_when_present():
    from agent_trace_kit import ghutil
    fake = _FakeGh(existing=None)
    repo, created = ghutil.ensure_remote_repo("new-task", "blurb", private=False, gh=fake)
    assert created is True and repo.full_name == "StatXzy7/new-task"
    assert repo.visibility == "PUBLIC" and repo.default_branch == "main"
    assert fake.created == [("new-task", "blurb", False)]
    # second call finds the repo and never recreates it
    repo2, created2 = ghutil.ensure_remote_repo("new-task", "blurb", private=True, gh=fake)
    assert created2 is False and repo2.url == repo.url and len(fake.created) == 1


def test_provision_baseline_folder_seeds_empty_remote_and_is_idempotent(tmp_path):
    import subprocess
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parent = tmp_path / "github-base"

    info = ws.provision_baseline_folder(parent, "task-x", bare.as_uri(), "main", "题目说明")
    target = Path(info["path"])
    assert ws.is_sha40(info["sha"]) and info["branch"] == "main" and info["seeded"] is True
    assert (target / "README.md").read_text(encoding="utf-8").startswith("# task-x\n\n题目说明")
    assert (target / ".gitignore").exists()
    # the initial commit is reachable on the bare remote's main
    assert ws._remote_branch_exists(target, "main")

    # re-running is a no-op: no new seed commit, same sha
    again = ws.provision_baseline_folder(parent, "task-x", bare.as_uri(), "main", "题目说明")
    assert again["seeded"] is False and again["sha"] == info["sha"]


def test_provision_baseline_folder_clones_existing_remote_without_injecting(tmp_path):
    """A remote that already has history is cloned as-is (README blurb not injected)."""
    import subprocess
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-b", "main"], seed)
    _git(["config", "user.email", "t@example.com"], seed)
    _git(["config", "user.name", "T"], seed)
    (seed / "problem.md").write_text("题目本体\n", encoding="utf-8")
    _git(["add", "-A"], seed)
    _git(["commit", "-m", "题目初始代码"], seed)
    _git(["remote", "add", "origin", str(bare)], seed)
    _git(["push", "-u", "origin", "main"], seed)
    original_sha = ws.head_sha(seed)

    parent = tmp_path / "github-base"
    info = ws.provision_baseline_folder(parent, "task-y", bare.as_uri(), "main", "忽略此说明")
    assert info["seeded"] is False and info["sha"] == original_sha
    target = Path(info["path"])
    assert (target / "problem.md").exists()
    assert not (target / "README.md").exists()  # existing history is never mutated


def test_provision_rejects_unrelated_non_git_folder(tmp_path):
    import subprocess
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parent = tmp_path / "github-base"
    occupied = parent / "task-z"
    occupied.mkdir(parents=True)
    (occupied / "notes.txt").write_text("user file", encoding="utf-8")
    with pytest.raises(ws.GitError):
        ws.provision_baseline_folder(parent, "task-z", bare.as_uri(), "main", "")


def test_job_carries_provisioning_fields_from_settings(tmp_path):
    store = DeskStore(tmp_path / "desk")
    store.save_settings({"baseline_parent_dir": str(tmp_path / "base"),
                         "github_owner": "", "github_private": True})
    job = store.create_job({"prompt": "p", "github_repo": "new-repo", "github_readme": "说明"})
    assert job["github_repo"] == "new-repo"
    assert job["github_readme"] == "说明"
    assert job["github_private"] is True
    assert job["baseline_parent_dir"] == str(tmp_path / "base")
    assert job["baseline_repo"] == ""


def test_path_vs_repo_name_heuristic():
    f = DeskServer._looks_like_local_path
    assert f(r"D:\myprojects\GoletaLab数据标注\github-base\x")
    assert f("/home/u/repos/x")
    assert not f("rate-limiter-lib")
    assert not f("my_repo.name-1")
    assert f("C:/work/x")
    assert not f("")


def test_prepare_end_to_end_provisions_github_repo(tmp_path, monkeypatch):
    """prepare() on a name-only job: fake gh + local bare remote -> seeded baseline + A/B."""
    from agent_trace_kit import runner as rn
    import subprocess

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parent = tmp_path / "github-base"
    store = DeskStore(tmp_path / "desk")
    store.save_settings({"baseline_parent_dir": str(parent)})
    job = store.create_job({"prompt": "实现一个模块", "github_repo": "e2e-task",
                            "github_readme": "端到端题目"})

    fake = _FakeGh(existing=None, url=bare.as_uri())
    _orig_ensure = ghutil.ensure_remote_repo

    def _fake_ensure(name, desc, *, private, owner="", gh=None):
        return _orig_ensure(name, desc, private=private, owner=owner, gh=fake)

    monkeypatch.setattr(rn.ghutil, "ensure_remote_repo", _fake_ensure)
    report = rn.PairRunner(store).prepare(job["id"])
    assert report["provisioned"] is not None
    assert report["provisioned"]["created"] is True
    assert report["provisioned"]["seeded"] is True
    assert fake.created[0][0] == "e2e-task"

    done = store.get_job(job["id"])
    assert done["baseline_repo"] == str(parent / "e2e-task")
    assert ws.is_sha40(done["baseline_sha"]) and done["status"] == "ready"
    # both isolated side workspaces were copied from the seeded baseline
    for side_name in ("A", "B"):
        wdir = done["sides"][side_name]["workspace"]
        assert Path(wdir).is_dir() and (Path(wdir) / "README.md").exists()
    # a second prepare reuses the local repo (no new remote create, same baseline sha)
    rn.PairRunner(store).prepare(job["id"])
    assert len(fake.created) == 1


def test_provision_refuses_zero_commit_folder_with_user_files(tmp_path):
    """HIGH: a git-init'd folder holding untracked files must never have them
    swept into a (public by default) initial commit/push."""
    import subprocess
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parent = tmp_path / "github-base"
    target = parent / "task-w"
    target.mkdir(parents=True)
    _git(["init", "-b", "main"], target)
    (target / "secret.txt").write_text("TOP SECRET\n", encoding="utf-8")  # untracked
    with pytest.raises(ws.GitError, match="拒绝自动提交"):
        ws.provision_baseline_folder(parent, "task-w", bare.as_uri(), "main", "")
    # nothing was pushed to the bare remote
    out, _, _ = subprocess_run_capture(["git", "--git-dir", str(bare), "ls-remote", "--heads"])
    assert out.strip() == ""


def subprocess_run_capture(args):
    import subprocess
    p = subprocess.run(args, text=True, capture_output=True)
    return p.stdout, p.stderr, p.returncode


def test_canonical_remote_matches_https_and_ssh_forms():
    f = ws.canonical_remote
    assert f("https://github.com/StatXzy7/task-x.git") == "github.com/statxzy7/task-x"
    assert f("git@github.com:StatXzy7/task-x.git") == "github.com/statxzy7/task-x"
    assert f("https://github.com/StatXzy7/task-y") != f("https://github.com/StatXzy7/task-x")
    assert f("not-a-remote") == ""


def test_ensure_remote_repo_rejects_owner_other_than_login():
    gh = _FakeGh(existing=None)
    with pytest.raises(ghutil.GitHubError, match="不一致"):
        ghutil.ensure_remote_repo("x", "d", private=False, gh=gh, owner="someone-else")
    assert gh.created == []  # nothing created


def test_is_client_gone_treats_winerror_10053_as_browser_abort():
    from agent_trace_kit.desk import _is_client_gone
    assert _is_client_gone(ConnectionAbortedError(10053, "aborted"))
    aborted = OSError(22, "aborted")
    aborted.winerror = 10053
    assert _is_client_gone(aborted)
    assert not _is_client_gone(ValueError("nope"))


def test_desk_csrf_and_host_defenses(tmp_path):
    """C1: only same-loopback application/json POSTs reach the action routes."""
    import http.client
    import threading
    from agent_trace_kit.desk import _ExclusiveServer, DeskServer

    desk = DeskServer(DeskStore(tmp_path / "desk"))
    httpd = _ExclusiveServer(("127.0.0.1", 0), desk.make_handler())
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        def post(headers, body=b"{}"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/api/jobs", body=body, headers=headers)
            r = conn.getresponse()
            r.read()
            return r.status

        # same-origin JSON request is accepted and routed
        assert post({"Content-Type": "application/json"}) == 200
        # cross-site "simple" request content type is rejected (forces preflight)
        assert post({"Content-Type": "text/plain"}) == 415
        # explicit cross-origin browser request blocked
        assert post({"Content-Type": "application/json", "Origin": "http://evil.example"}) == 403
        # DNS-rebinding style Host header blocked
        assert post({"Content-Type": "application/json", "Host": "evil.example"}) == 403
        # loopback origin but a different port blocked
        assert post({"Content-Type": "application/json", "Origin": "http://127.0.0.1:9999"}) == 403
        # SSH forwarding preserves the browser's Host and Origin, both different
        # from the worker's actual listening port.
        assert post({"Content-Type": "application/json", "Host": "127.0.0.1:18765",
                     "Origin": "http://127.0.0.1:18765"}) == 200
        assert post({"Content-Type": "application/json", "Host": "127.0.0.1:18765",
                     "Origin": "http://127.0.0.1:9999"}) == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------- P1-3: session id taken from the stream-json init event ----------

def test_runner_prefers_init_session_id_over_newest_mtime(tmp_path, monkeypatch):
    """The CLI's init event names the real session; an older mtime must not win."""
    from agent_trace_kit import runner as rn
    store = DeskStore(tmp_path / "desk")
    store.create_job({"prompt": "实现一个模块"})
    job_id = store.list_jobs()[0]["id"]
    wdir = tmp_path / "w"
    wdir.mkdir(exist_ok=True)

    init_line = json.dumps({"type": "system", "subtype": "init",
                            "sessionId": "sid-init", "cwd": str(wdir)}) + "\n"
    complete = _write_transcript(
        tmp_path / "complete.jsonl", _tool_tail_records("实现一个模块", "完成"))
    other = _write_transcript(
        tmp_path / "other.jsonl", _tool_tail_records("实现一个模块", "也完成"))

    fake_proc = _FakeProc([init_line, _result_line(False)], 0)
    monkeypatch.setattr(rn.subprocess, "Popen", lambda *a, **k: fake_proc)
    # Newest-by-mtime candidate is a DIFFERENT session; the init session is older.
    monkeypatch.setattr(ws, "find_session_jsonl", lambda w, **kwargs: [
        {"path": str(other), "session_id": "sid-newer-noise", "mtime": "999"},
        {"path": str(complete), "session_id": "sid-init", "mtime": "1"},
    ])
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(lambda w, **kwargs: 0.0))
    evidence = store.evidence_dir(job_id)
    out = rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        evidence / "attempts" / "a-01-stream.jsonl",
        timeout=60, stall_after=30, poll_every=5, attempt=1)
    assert out["completed"] is True
    assert out["new_session"]["session_id"] == "sid-init"
    assert out["init_session_id"] == "sid-init"


# ---------- P0-1: per-attempt evidence archiving + structured ledger ----------

def test_archive_attempt_keeps_cut_transcript_and_stream(tmp_path):
    from agent_trace_kit import runner as rn
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p"})
    runner = rn.PairRunner(store)
    evidence = store.evidence_dir(job["id"])
    cut = _write_transcript(
        tmp_path / "cut.jsonl",
        _tool_tail_records("p", "API Error: 504 Gateway Time-out", final_is_api_error=True))
    stream = evidence / "attempts" / "a-01-stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"result"}\n', encoding="utf-8")

    ledger: list = []
    runner._archive_attempt(job["id"], "A", evidence, {
        "attempt": 1, "started_at": "t0", "finished_at": "t1", "duration_seconds": 12,
        "completed": False, "aborted": False, "code": 1, "stalls": 0,
        "failure": "网关 504/API Error，轮次中途截断",
        "session_path": str(cut), "session_id": "sid-cut",
    }, ledger)

    assert len(ledger) == 1
    row = ledger[0]
    assert row["status"] == "cut" and row["session_id"] == "sid-cut"
    assert row["failure"].startswith("网关 504")
    # the discarded turn is preserved inside evidence, not only in the run log
    assert Path(row["transcript_path"]).is_file()
    assert "sid-cut" in row["transcript_path"]
    assert Path(row["stream_path"]).is_file()


def test_archive_attempt_marks_complete(tmp_path):
    from agent_trace_kit import runner as rn
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p"})
    evidence = store.evidence_dir(job["id"])
    done = _write_transcript(tmp_path / "ok.jsonl", _tool_tail_records("p", "完成"))
    ledger: list = []
    rn.PairRunner(store)._archive_attempt(job["id"], "B", evidence, {
        "attempt": 2, "started_at": "a", "finished_at": "b", "duration_seconds": 30,
        "completed": True, "aborted": False, "code": 0, "stalls": 1, "failure": "",
        "session_path": str(done), "session_id": "sid-ok",
    }, ledger)
    assert ledger[0]["status"] == "complete" and ledger[0]["attempt"] == 2
    assert Path(ledger[0]["transcript_path"]).is_file()


def test_run_side_writes_structured_attempts_ledger(tmp_path, monkeypatch):
    """End-to-end run_side: a cut attempt then a complete one leaves a 2-row ledger."""
    from agent_trace_kit import runner as rn
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "实现一个模块", "baseline_repo": str(tmp_path / "base")})
    base = tmp_path / "base"
    base.mkdir()
    wdir = tmp_path / "ws" / job["id"] / "a"
    wdir.mkdir(parents=True)
    store.update_side(job["id"], "A", {"workspace": str(wdir)})

    cut = _write_transcript(tmp_path / "cut.jsonl",
                            _tool_tail_records("实现一个模块",
                                               "API Error: 504", final_is_api_error=True))
    good = _write_transcript(tmp_path / "good.jsonl",
                             _tool_tail_records("实现一个模块", "完成，测试通过"))
    calls = {"n": 0}

    class FakeProc:
        def __init__(self, *a, **k):
            self.pid = 999
            self.stdout = iter([json.dumps({"type": "system", "subtype": "init", "tools": ["Bash", "Read"]}) + "\n", _result_line(False)])
            self.returncode = 0
            self.stdin = type("S", (), {
                "write": lambda *x: None, "flush": lambda *x: None, "close": lambda *x: None,
            })()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

    def fake_popen(*a, **k):
        calls["n"] += 1
        return FakeProc()

    # attempt 1 -> cut transcript; attempt 2 -> complete
    def fake_find(w, **kwargs):
        return [{"path": str(good if calls["n"] >= 2 else cut),
                 "session_id": "sid-good" if calls["n"] >= 2 else "sid-cut",
                 "mtime": "9"}]

    monkeypatch.setattr(rn.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ws, "find_session_jsonl", fake_find)
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(lambda w, **kwargs: 0.0))
    monkeypatch.setattr(rn, "retry_backoff_seconds", lambda *a, **k: 0)
    monkeypatch.setattr(rn.PairRunner, "_fresh_workspace", lambda self, j, s, **k: None)
    monkeypatch.setattr(ws, "finalize_side",
                        lambda wdir, branch, msg, force=False: {"sha": "a" * 40, "url": "u", "branch": branch})

    rn.PairRunner(store).run_side(job["id"], "A")
    side = store.get_job(job["id"])["sides"]["A"]
    assert side["status"] == "done"
    assert isinstance(side["attempts"], list) and len(side["attempts"]) == 2
    assert side["attempts"][0]["status"] == "cut"
    assert side["attempts"][1]["status"] == "complete"
    # both attempts' raw streams were archived separately
    ev = store.evidence_dir(job["id"]) / "attempts"
    assert (ev / "a-01-stream.jsonl").is_file() and (ev / "a-02-stream.jsonl").is_file()


# ---------- P1-4: human-only review lock gate ----------

def test_review_lock_blocks_run_actions_server_side(tmp_path):
    from agent_trace_kit import desk as desk_mod
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p"})
    desk = desk_mod.DeskServer(store)
    for name in ("A", "B"):
        trace = _write_transcript(tmp_path / f"{name}.jsonl", _tool_tail_records("p", "完成"))
        store.update_side(job["id"], name, {"jsonl_local": str(trace)})

    def expect_blocked(action_route, body):
        with pytest.raises(RuntimeError, match="锁定"):
            action_route(body)

    # lock with attestation
    desk.review_save({"job": job["id"], "validity": "有效", "conclusion": "A 更好",
                      "reason": "A 的错误处理路径完整且有测试，B 在边界输入崩溃。",
                      "a_delivery_score": "5", "a_delivery_description": "A 的错误处理路径完整且有测试。",
                      "b_delivery_score": "2", "b_delivery_description": "B 在边界输入崩溃，产物不可稳定使用。",
                      "ai_confirmed": True, "lock": True})
    assert DeskStore.review_locked(store.get_job(job["id"]))

    expect_blocked(desk.side_action, {"job": job["id"], "side": "A", "action": "retry"})
    expect_blocked(desk.job_action, {"job": job["id"], "action": "prepare"})
    expect_blocked(desk.job_action, {"job": job["id"], "action": "collect"})
    expect_blocked(desk.job_action, {"job": job["id"], "action": "enqueue"})
    # abort stays available even when locked (operator safety)
    assert desk.side_action({"job": job["id"], "side": "A", "action": "abort"}) == {"aborted": True}

    # lock requires the no-AI attestation
    j2 = store.create_job({"prompt": "p2"})
    with pytest.raises(RuntimeError, match="未使用任何 AI"):
        desk.review_save({"job": j2["id"], "reason": "x" * 40, "ai_confirmed": False, "lock": True})

    # explicit unlock re-enables runs
    desk.review_save({"job": job["id"], "unlock": True})
    assert not DeskStore.review_locked(store.get_job(job["id"]))


# ---------- delete job: remote cleanup options ----------

class _RecordingGh:
    """CliGh stand-in for delete tests: records calls, scripts failures."""

    def __init__(self, login: str = "StatXzy7", fail_branches: tuple = (), repo_delete_error: str = ""):
        self._login = login
        self._fail_branches = fail_branches
        self._repo_delete_error = repo_delete_error
        self.deleted_branches: list[str] = []
        self.deleted_repos: list[str] = []

    def viewer_login(self) -> str:
        return self._login

    def repo_delete(self, full_name: str) -> None:
        if self._repo_delete_error:
            raise ghutil.GitHubError(self._repo_delete_error)
        self.deleted_repos.append(full_name)

    def delete_remote_branches(self, full_name: str, branches: list[str]) -> list[str]:
        errors = []
        for b in branches:
            if b in self._fail_branches:
                errors.append(f"{b}: 422 boom")
            else:
                self.deleted_branches.append(f"{full_name}@{b}")
        return errors


def _make_desk_with_gh(tmp_path, monkeypatch, gh):
    store = DeskStore(tmp_path / "desk")
    desk = DeskServer(store)
    monkeypatch.setattr(desk, "_gh_login", lambda **k: gh.viewer_login())
    monkeypatch.setattr(ghutil, "CliGh", lambda *a, **k: gh)
    return store, desk


def _job_with_remote(store, tmp_path, *, created=True, pushed=True):
    job = store.create_job({"prompt": "p"})
    baseline = tmp_path / "base" / job["id"]
    baseline.mkdir(parents=True)
    (baseline / "README.md").write_text("x", encoding="utf-8")
    store.update_job(job["id"], {
        "github_repo": "StatXzy7/auto-repo", "github_created": created,
        "baseline_repo": str(baseline),
    })
    for s in ("A", "B"):
        store.update_side(job["id"], s, {
            "branch": f"pair-{job['id']}/{s.lower()}", "pushed": pushed,
        })
    return job, baseline


def test_job_delete_keep_remote_is_default(tmp_path, monkeypatch):
    gh = _RecordingGh()
    store, desk = _make_desk_with_gh(tmp_path, monkeypatch, gh)
    job, baseline = _job_with_remote(store, tmp_path)
    out = desk.job_action({"job": job["id"], "action": "delete"})
    assert out["deleted"] is True and "branches_deleted" not in out
    assert gh.deleted_branches == [] and gh.deleted_repos == []
    assert not store.job_path(job["id"]).exists()
    assert baseline.is_dir()  # local baseline untouched in keep mode


def test_job_delete_with_remote_branches(tmp_path, monkeypatch):
    gh = _RecordingGh()
    store, desk = _make_desk_with_gh(tmp_path, monkeypatch, gh)
    job, baseline = _job_with_remote(store, tmp_path)
    out = desk.job_action({"job": job["id"], "action": "delete", "remote": "branches"})
    assert out["deleted"] is True
    assert gh.deleted_branches == [
        f"StatXzy7/auto-repo@pair-{job['id']}/a",
        f"StatXzy7/auto-repo@pair-{job['id']}/b",
    ]
    assert gh.deleted_repos == [] and baseline.is_dir()


def test_job_delete_repo_mode_removes_repo_and_unused_baseline(tmp_path, monkeypatch):
    gh = _RecordingGh()
    store, desk = _make_desk_with_gh(tmp_path, monkeypatch, gh)
    job, baseline = _job_with_remote(store, tmp_path)
    out = desk.job_action({"job": job["id"], "action": "delete", "remote": "repo"})
    assert out["deleted"] is True and out["repo_deleted"] == "StatXzy7/auto-repo"
    assert gh.deleted_repos == ["StatXzy7/auto-repo"]
    assert not baseline.exists()  # auto-provisioned local baseline removed too


def test_job_delete_repo_mode_keeps_baseline_used_by_other_job(tmp_path, monkeypatch):
    gh = _RecordingGh()
    store, desk = _make_desk_with_gh(tmp_path, monkeypatch, gh)
    job, baseline = _job_with_remote(store, tmp_path)
    other = store.create_job({"prompt": "p2"})
    store.update_job(other["id"], {"baseline_repo": str(baseline)})
    desk.job_action({"job": job["id"], "action": "delete", "remote": "repo"})
    assert baseline.is_dir()  # another job still references it


def test_job_delete_repo_mode_rejects_non_auto_created_repo(tmp_path, monkeypatch):
    gh = _RecordingGh()
    store, desk = _make_desk_with_gh(tmp_path, monkeypatch, gh)
    job, _ = _job_with_remote(store, tmp_path, created=False)
    with pytest.raises(RuntimeError, match="不是交付台自动创建"):
        desk.job_action({"job": job["id"], "action": "delete", "remote": "repo"})
    assert gh.deleted_repos == []
    assert store.job_path(job["id"]).exists()  # local state kept on failure


def test_job_delete_remote_rejects_foreign_owner(tmp_path, monkeypatch):
    gh = _RecordingGh(login="StatXzy7")
    store, desk = _make_desk_with_gh(tmp_path, monkeypatch, gh)
    job, _ = _job_with_remote(store, tmp_path)
    store.update_job(job["id"], {"github_repo": "someone-else/auto-repo"})
    with pytest.raises(RuntimeError, match="不属于当前 gh 登录账号"):
        desk.job_action({"job": job["id"], "action": "delete", "remote": "branches"})
    assert gh.deleted_branches == [] and store.job_path(job["id"]).exists()


def test_job_delete_remote_failure_keeps_local_state(tmp_path, monkeypatch):
    gh = _RecordingGh(fail_branches=("pair-x/a",))
    store, desk = _make_desk_with_gh(tmp_path, monkeypatch, gh)
    job, _ = _job_with_remote(store, tmp_path)
    # make the A branch deletion fail
    gh._fail_branches = (f"pair-{job['id']}/a",)
    with pytest.raises(RuntimeError, match="删除远端分支失败"):
        desk.job_action({"job": job["id"], "action": "delete", "remote": "branches"})
    assert store.job_path(job["id"]).exists()  # retry or 仅删除本地 still possible


def test_job_delete_without_github_repo_has_no_remote_option(tmp_path, monkeypatch):
    gh = _RecordingGh()
    store, desk = _make_desk_with_gh(tmp_path, monkeypatch, gh)
    job = store.create_job({"prompt": "p"})
    with pytest.raises(RuntimeError, match="没有记录 GitHub 仓库"):
        desk.job_action({"job": job["id"], "action": "delete", "remote": "branches"})
    # keep mode still works fine for local-only jobs
    assert desk.job_action({"job": job["id"], "action": "delete"})["deleted"] is True


def test_cligh_delete_remote_branches_uses_refs_api(monkeypatch):
    calls = []

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(self, args):
        calls.append(args)
        return _P()

    monkeypatch.setattr(ghutil.CliGh, "_run", fake_run)
    errors = ghutil.CliGh().delete_remote_branches("o/r", ["pair-1/a", "pair-1/b"])
    assert errors == []
    assert calls[0] == ["api", "repos/o/r/git/refs/heads/pair-1/a", "-X", "DELETE"]
    assert calls[1][1].endswith("heads/pair-1/b")


def test_cligh_repo_delete_surfaces_scope_hint(monkeypatch):
    class _P:
        returncode = 1
        stdout = ""
        stderr = "must have the `delete_repo` scope"

    monkeypatch.setattr(ghutil.CliGh, "_run", lambda self, args: _P())
    with pytest.raises(ghutil.GitHubError, match="delete_repo"):
        ghutil.CliGh().repo_delete("o/r")


# ---------- recorder: auto-stop cap is passed to ffmpeg ----------

def test_recorder_start_passes_window_and_time_cap(tmp_path, monkeypatch):
    from agent_trace_kit import recorder as rec_mod
    monkeypatch.setattr(rec_mod.sys, "platform", "win32")
    store = DeskStore(tmp_path / "desk")
    captured = {}

    class FakeProc:
        pid = 7
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(rec_mod.shutil, "which", lambda name: r"C:\ffmpeg.exe")
    monkeypatch.setattr(rec_mod.subprocess, "Popen", fake_popen)
    rec = rec_mod.Recorder(store)
    out = rec.start("pair-x", "A", window="Chrome — 演示", max_seconds=90, fps=12)
    assert out["max_seconds"] == 89 and out["window"] == "Chrome — 演示"
    cmd = captured["cmd"]
    assert cmd[cmd.index("-i") + 1] == "title=Chrome — 演示"
    assert cmd[cmd.index("-t") + 1] == "89"
    # full-screen default uses the desktop grabber
    rec.start("pair-x", "B", max_seconds=0, fps=0)
    cmd_b = captured["cmd"]
    assert cmd_b[cmd_b.index("-i") + 1] == "desktop"
    assert cmd_b[cmd_b.index("-t") + 1] == str(rec_mod.DEFAULT_MAX_SECONDS)


def test_list_windows_does_not_spawn_powershell(monkeypatch):
    from agent_trace_kit import recorder as rec_mod

    def boom(*_a, **_k):
        raise AssertionError("list_windows must not spawn PowerShell")

    monkeypatch.setattr(rec_mod.subprocess, "run", boom)
    rows = rec_mod.Recorder.list_windows()
    assert isinstance(rows, list)
    for row in rows:
        assert row.get("title", "").strip()


def test_desk_ports_scan_rewrites_occupied_and_rejects_non_loopback_probe(tmp_path, monkeypatch):
    """GSB helper must not send the operator to a non-loopback URL, and must
    rewrite README's :8080 when that port is already taken by Steam CEF."""
    store = DeskStore(tmp_path / "desk")
    desk = DeskServer(store, port=8765)
    job = store.create_job({"prompt": "web lab"})
    wdir = tmp_path / "ws"
    wdir.mkdir()
    (wdir / "README.md").write_text("python -m http.server 8080\n", encoding="utf-8")
    (wdir / "index.html").write_text(
        "<html><head><title>Lab</title></head><body><script type=\"module\" src=\"a.js\"></script></body></html>",
        encoding="utf-8")
    store.update_side(job["id"], "A", {"workspace": str(wdir)})

    listeners = [{"addr": "127.0.0.1", "port": 8080, "pid": 1, "name": "steamwebhelper.exe"}]

    def fake_probe(url, timeout=1.2):
        return {"url": url, "ok": False, "status": 200, "kind": "cef_debugger",
                "title": "Inspectable WebContents", "snippet": "Steam", "error": ""}

    from agent_trace_kit import desk as desk_mod
    orig = desk_mod.ports_mod.gsb_scan

    def wrapped(**kw):
        kw.setdefault("listeners", listeners)
        kw.setdefault("probe_fn", fake_probe)
        return orig(**kw)

    monkeypatch.setattr(desk_mod.ports_mod, "gsb_scan", wrapped)
    report = desk.ports_scan({"job": job["id"]})
    assert any(w["kind"] == "foreign_cef" for w in report["warnings"])
    assert report["sides"]["A"]["free_port"] != 8080
    assert "8080" not in (report["sides"]["A"]["rewritten_commands"] or [""])[0]

    with pytest.raises(RuntimeError, match="loopback"):
        desk.ports_probe({"url": "http://example.com/"})
    with pytest.raises(RuntimeError, match="用户名"):
        desk.ports_probe({"url": "http://127.0.0.1:8080@example.com/"})
    cmd = (report["sides"]["A"]["rewritten_commands"] or [""])[0]
    assert str(report["sides"]["A"]["free_port"]) in cmd


def test_ports_overview_lists_previews_and_killable_leftovers(tmp_path, monkeypatch):
    from agent_trace_kit import desk as desk_mod
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "web lab", "name": "flow-lab"})
    desk = DeskServer(store, port=8765)
    desk.previews._items[f"{job['id']}/B"] = {
        "url": "http://127.0.0.1:18082/", "port": 18082,
        "workspace": r"C:\ws\b", "httpd": None, "thread": None,
        "probe": {"ok": True, "kind": "product", "status": 200, "title": "B"},
    }
    monkeypatch.setattr(desk_mod.ports_mod, "list_listeners", lambda: [
        {"addr": "127.0.0.1", "port": 8080, "pid": 99, "name": "node.exe"},
        {"addr": "127.0.0.1", "port": 8765, "pid": 1, "name": "python.exe"},
        {"addr": "127.0.0.1", "port": 5173, "pid": 2, "name": "steamwebhelper.exe"},
        {"addr": "127.0.0.1", "port": 18082, "pid": 1, "name": "python.exe"},
    ])
    report = desk.ports_overview({})
    assert report["previews"][0]["job_name"] == "flow-lab"
    assert report["previews"][0]["side"] == "B"
    ports = {x["port"]: x for x in report["leftovers"]}
    assert 8080 in ports and ports[8080]["killable"] is True
    assert 5173 in ports and ports[5173]["killable"] is False
    assert 8765 not in ports
    assert 18082 not in ports


def test_open_shell_launches_powershell_in_workspace_cwd(tmp_path, monkeypatch):
    """Operator test helper: visible PowerShell whose cwd is the side workspace."""
    from agent_trace_kit import desk as desk_mod

    monkeypatch.setattr(desk_mod.sys, "platform", "win32")
    store = DeskStore(tmp_path / "desk")
    desk = DeskServer(store)
    job = store.create_job({"prompt": "trace lab"})
    wdir = tmp_path / "ws'a"
    wdir.mkdir()
    store.update_side(job["id"], "A", {"workspace": str(wdir)})
    captured = {}

    class FakePopen:
        def __init__(self, args, **kw):
            captured["args"] = args
            captured["kw"] = kw

    monkeypatch.setattr(desk_mod.subprocess, "Popen", FakePopen)
    out = desk.open_shell({"job": job["id"], "side": "A", "path": str(tmp_path / "evil")})
    resolved = str(wdir.resolve())
    assert Path(out["opened"]) == wdir.resolve()
    assert captured["kw"]["cwd"] == resolved
    assert captured["kw"].get("creationflags") == getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    assert "powershell" in str(captured["args"][0]).lower()
    assert "-NoExit" in captured["args"]
    command = captured["args"][captured["args"].index("-Command") + 1]
    assert "Set-Location -LiteralPath" in command
    assert resolved.replace("'", "''") in command
    assert str(tmp_path / "evil") not in " ".join(str(x) for x in captured["args"])


def test_open_shell_rejects_missing_workspace_and_bad_side(tmp_path, monkeypatch):
    from agent_trace_kit import desk as desk_mod
    monkeypatch.setattr(desk_mod.sys, "platform", "win32")
    store = DeskStore(tmp_path / "desk")
    desk = DeskServer(store)
    job = store.create_job({"prompt": "trace lab"})
    with pytest.raises(RuntimeError, match="工作区不存在"):
        desk.open_shell({"job": job["id"], "side": "A"})
    with pytest.raises(RuntimeError, match="A 或 B"):
        desk.open_shell({"job": job["id"], "side": "C"})


def test_open_shell_linux_returns_quoted_command(tmp_path, monkeypatch):
    from agent_trace_kit import desk as desk_mod
    monkeypatch.setattr(desk_mod.sys, "platform", "linux")
    store = DeskStore(tmp_path / "desk")
    desk = DeskServer(store)
    job = store.create_job({"prompt": "trace lab"})
    workspace = tmp_path / "work space's"
    workspace.mkdir()
    store.update_side(job["id"], "A", {"workspace": str(workspace)})
    result = desk.open_shell({"job": job["id"], "side": "A"})
    import shlex
    assert shlex.split(result["command"]) == ["cd", "--", str(workspace.resolve())]
