from __future__ import annotations

import hashlib
import copy
from io import BytesIO
import json
import os
from pathlib import Path
from urllib.error import URLError

import pytest

from agent_trace_kit import batch_pipeline as bp


def batch(tmp_path, count=2):
    folder = tmp_path / "batch"
    folder.mkdir()
    cards = [{"github_repo": f"中文-repo-{i}", "prompt": "真实边界验收"} for i in range(count)]
    (folder / "create-requests.json").write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    scoring = tmp_path / "score.txt"
    scoring.write_text("根据真实执行证据评分，保留用户判断有效性。", encoding="utf-8")
    pipeline = bp.BatchPipeline(folder, "http://127.0.0.1:8765", scoring, desk_home=tmp_path / "desk")
    return pipeline, cards


def receipt(pipeline, cards, count=None):
    count = len(cards) if count is None else count
    # This is the exact expression used by the actual launch_pairs.py.
    digest = hashlib.sha256(json.dumps(cards, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    value = {"batch_sha256": digest, "entries": {
        card["github_repo"]: {"status": "queued", "job_id": f"pair-{index:010x}"}
        for index, card in enumerate(cards[:count])}}
    bp.atomic_json(pipeline.batch_dir / "submission-receipt.json", value)
    return value


def job(job_id, status="done"):
    return {"id": job_id, "agent": "codex", "codex_model": bp.MODEL, "review": {}, "check": {"items": []},
            "sides": {name: {"status": status, "live": False, "head_sha": "a" * 40,
                              "session_id": name + "-session", "finished_at": "now",
                              "jsonl_local": "raw-" + name + ".jsonl",
                              "trace_url": "https://evidence/trace-" + name, "video_url": "https://evidence/video-" + name}
                      for name in ("A", "B")}}


class Api:
    def __init__(self, jobs):
        self.jobs = {value["id"]: value for value in jobs}
        self.calls = []

    def call(self, path, body, **kwargs):
        self.calls.append((path, body, kwargs))
        if path == "/api/jobs":
            return {"jobs": list(self.jobs.values())}
        if path == "/api/job":
            return self.jobs[body["id"]]
        if path == "/api/settings_get":
            return {"codex_command": "codex"}
        if path == "/api/export":
            return {"path": "/evidence/submission.tsv"}
        raise AssertionError(f"Unexpected mutation: {path}")


def evaluation():
    return {"a_delivery_score": 4, "b_delivery_score": 3,
            "a_delivery_description": "实际数值路径正常，关键非法输入仍有验证不足。",
            "b_delivery_description": "边界输入已复现异常，存在主体缺口。",
            "conclusion": "A 更好", "reason": "实际分别运行两侧数值输入，A保存后能够还原，B在相同输入下出现了状态丢失。这影响主体要求，综合来看A更好。",
            "qc_status": "待判断有效性", "qc_feedback": "主体测试和录像已保存，用户需决定有效性。", "note": "AI自动评价，用户已授权。"}


def capture_receipt(pipeline, current, traces=None):
    root = pipeline.desk_home / "evidence" / current["id"]
    root.mkdir(parents=True, exist_ok=True)
    traces = traces or [root / "cut-transcript.jsonl"]
    for path in traces:
        if not path.exists():
            path.write_text("真实失败记录", encoding="utf-8")
    side = current["sides"]["A"]
    side["status"] = "failed"
    original = copy.deepcopy(side)
    side.update(workspace=str(root / "snapshot/workspace"), head_sha="b" * 40,
                session_id="actual-cut-session", jsonl_local=str(traces[-1]))
    value = {"job": current["id"], "side": "A", "model": current["codex_model"],
             "prompt_sha256": hashlib.sha256(str(current.get("prompt", "")).encode("utf-8")).hexdigest(),
             "complete": False, "source_pre_post_equal": True, "original_side": original,
             "original_side_sha256": bp.side_sha256(original),
             "snapshot_workspace": side["workspace"], "snapshot_head_sha": side["head_sha"],
             "last_attempt": {"complete": False, "session_id": side["session_id"], "transcript_path": side["jsonl_local"],
                              "sha256": bp.sha256(traces[-1])},
             "evidence_files": [{"path": str(path), "sha256": bp.sha256(path)} for path in traces]}
    path = root / "capture.json"
    bp.atomic_json(path, value)
    side["failure_capture"] = {"origin": "posthoc-failed-worktree", "completed": False,
                               "receipt_path": str(path), "receipt_sha256": bp.sha256(path)}
    return path


def test_receipt_hash_matches_real_launcher_canonical_expression(tmp_path):
    pipeline, cards = batch(tmp_path)
    value = receipt(pipeline, cards)
    assert pipeline.requests_digest == value["batch_sha256"]
    assert pipeline.requests_digest != hashlib.sha256(json.dumps(cards, ensure_ascii=False, sort_keys=True,
                                                               separators=(",", ":")).encode()).hexdigest()


def test_partial_submission_never_starts_postprocessing_or_reads_api(tmp_path):
    pipeline, cards = batch(tmp_path)
    receipt(pipeline, cards, count=1)
    pipeline.api = Api([])
    pipeline.tick()
    assert pipeline.state["status"] == "waiting_submission"
    assert pipeline.api.calls == []


def test_real_twenty_card_partial_receipt_with_queued_and_pending_waits(tmp_path):
    pipeline, cards = batch(tmp_path, count=20)
    value = receipt(pipeline, cards, count=3)
    value["entries"][cards[3]["github_repo"]] = {"status": "pending"}
    bp.atomic_json(pipeline.batch_dir / "submission-receipt.json", value)
    pipeline.api = Api([])
    pipeline.state["controller_error"] = "ValueError: The batch submission has an unresolved outcome"
    pipeline.tick()
    assert pipeline.state["status"] == "waiting_submission"
    assert "controller_error" not in pipeline.state
    assert not pipeline.api.calls


def test_thirteen_queued_and_seven_registered_wait_without_api_or_model(tmp_path):
    pipeline, cards = batch(tmp_path, count=20)
    value = receipt(pipeline, cards)
    for card in cards[13:]:
        value["entries"][card["github_repo"]]["status"] = "registered"
    bp.atomic_json(pipeline.batch_dir / "submission-receipt.json", value)
    pipeline.state["controller_error"] = "old registered status rejection"
    pipeline.api = Api([])
    pipeline.process_one = lambda *_: pytest.fail("Registered is not yet queued")
    pipeline.tick()
    assert pipeline.state["status"] == "waiting_submission"
    assert "controller_error" not in pipeline.state
    assert not pipeline.api.calls


@pytest.mark.parametrize("job_id", ["", "../../other", "pair-0000000000"])
def test_registered_invalid_or_duplicate_job_id_is_rejected(tmp_path, job_id):
    pipeline, cards = batch(tmp_path)
    value = receipt(pipeline, cards)
    value["entries"][cards[-1]["github_repo"]] = {"status": "registered", "job_id": job_id}
    bp.atomic_json(pipeline.batch_dir / "submission-receipt.json", value)
    pipeline.api = Api([])
    with pytest.raises(ValueError, match="job ID"):
        pipeline.tick()
    assert not pipeline.api.calls


def test_full_repository_set_with_inflight_pending_still_waits_without_api(tmp_path):
    pipeline, cards = batch(tmp_path)
    value = receipt(pipeline, cards)
    value["entries"][cards[-1]["github_repo"]] = {"status": "pending"}
    bp.atomic_json(pipeline.batch_dir / "submission-receipt.json", value)
    pipeline.api = Api([])
    pipeline.tick()
    assert pipeline.state["status"] == "waiting_submission"
    assert not pipeline.api.calls


def test_pending_receipt_cannot_bypass_batch_hash_validation(tmp_path):
    pipeline, cards = batch(tmp_path)
    value = receipt(pipeline, cards, count=1)
    value["batch_sha256"] = "0" * 64
    value["entries"][cards[-1]["github_repo"]] = {"status": "pending"}
    bp.atomic_json(pipeline.batch_dir / "submission-receipt.json", value)
    pipeline.api = Api([])
    with pytest.raises(RuntimeError, match="exact batch"):
        pipeline.tick()
    assert not pipeline.api.calls


def test_known_prepare_failure_is_attention_even_during_partial_submission(tmp_path):
    pipeline, cards = batch(tmp_path)
    value = receipt(pipeline, cards, count=1)
    value["entries"][cards[0]["github_repo"]]["status"] = "prepare_failed"
    bp.atomic_json(pipeline.batch_dir / "submission-receipt.json", value)
    pipeline.api = Api([])
    with pytest.raises(RuntimeError, match="known failure"):
        pipeline.tick()
    assert not pipeline.api.calls


def test_successful_generation_wait_clears_stale_controller_error(tmp_path):
    pipeline, cards = batch(tmp_path)
    ids = bp.receipt_jobs(receipt(pipeline, cards))
    pipeline.api = Api([job(value, "running") for value in ids])
    pipeline.state["controller_error"] = "old submission error"
    pipeline.tick()
    assert pipeline.state["status"] == "waiting_generation"
    assert "controller_error" not in pipeline.state


def test_no_receipt_once_waits_without_api_or_model(tmp_path):
    pipeline, _ = batch(tmp_path)
    pipeline.api = Api([])
    pipeline.tick()
    assert pipeline.state["status"] == "waiting_submission"
    assert not pipeline.api.calls


def test_entire_batch_waits_until_all_generation_is_terminal(tmp_path):
    pipeline, cards = batch(tmp_path)
    value = receipt(pipeline, cards)
    ids = bp.receipt_jobs(value)
    pipeline.api = Api([job(ids[0]), job(ids[1], "queued")])
    pipeline.process_one = lambda *_: pytest.fail("Premature postprocessing")
    pipeline.tick()
    assert pipeline.state["status"] == "waiting_generation"


def test_other_live_generation_yields_without_model(tmp_path):
    pipeline, cards = batch(tmp_path)
    ids = bp.receipt_jobs(receipt(pipeline, cards))
    pipeline.api = Api([job(value) for value in ids] + [job("pair-fffffffffe", "running")])
    pipeline.process_one = lambda *_: pytest.fail("Should yield CPU/display")
    pipeline.tick()
    assert pipeline.state["status"] == "waiting_other_generation"


@pytest.mark.parametrize("entry", [{"status": "pending"}, {"status": "prepare_failed", "job_id": "pair-0000000000"},
                                   {"status": "queued", "job_id": "../../other"}])
def test_unresolved_receipt_or_unsafe_job_cannot_trigger_requests(entry):
    with pytest.raises(ValueError):
        bp.receipt_jobs({"batch_sha256": "a" * 64, "entries": {"repo": entry}})


def test_duplicate_job_ids_are_rejected():
    item = {"status": "queued", "job_id": "pair-0000000000"}
    with pytest.raises(ValueError):
        bp.receipt_jobs({"batch_sha256": "a" * 64, "entries": {"a": item, "b": item}})


def test_wrong_receipt_repository_set_is_rejected(tmp_path):
    pipeline, cards = batch(tmp_path)
    value = receipt(pipeline, cards)
    value["entries"]["unauthorized"] = value["entries"].pop(cards[0]["github_repo"])
    bp.atomic_json(pipeline.batch_dir / "submission-receipt.json", value)
    with pytest.raises(RuntimeError, match="repository set"):
        pipeline.tick()


def test_evaluation_cannot_decide_validity_or_human_no_ai_confirmation():
    for forbidden in ("validity", "ai_confirmed", "lock", "reviewer"):
        with pytest.raises(ValueError, match="unsupported"):
            bp.validate_evaluation({**evaluation(), forbidden: "有效"})


@pytest.mark.parametrize("score", [True, 0, 6, "5"])
def test_invalid_score_is_not_coerced_to_valid(score):
    with pytest.raises(ValueError, match="integer"):
        bp.validate_evaluation({**evaluation(), "a_delivery_score": score})


def test_original_artifact_changes_are_detected_but_new_helpers_allowed(tmp_path):
    product = tmp_path / "impl.py"
    product.write_text("print('真实输出')\n", encoding="utf-8")
    original = bp.original_manifest(tmp_path)
    helper = tmp_path / ".atk-review"
    helper.mkdir()
    (helper / "probe.py").write_text("print('helper')\n", encoding="utf-8")
    bp.verify_originals(tmp_path, original)
    product.write_text("print('invented fixed result')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="changed"):
        bp.verify_originals(tmp_path, original)


def test_committed_symlink_is_verified_without_rejecting_it(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("data", encoding="utf-8")
    link = tmp_path / "alias.txt"
    try:
        link.symlink_to("real.txt")
    except OSError:
        pytest.skip("Symlink privilege unavailable")
    manifest = bp.original_manifest(tmp_path)
    bp.verify_originals(tmp_path, manifest)
    link.unlink()
    link.symlink_to("different.txt")
    with pytest.raises(RuntimeError, match="changed"):
        bp.verify_originals(tmp_path, manifest)


def test_desk_rejects_nonloopback_and_redirect_targets():
    for value in ("https://127.0.0.1:8765", "http://example.com", "http://user:password@127.0.0.1:8765",
                  "http://127.0.0.1:8765/api", "http://127.0.0.1:8765?token=x"):
        with pytest.raises(ValueError):
            bp.DeskApi(value)


def test_mutation_transport_error_retains_unknown_outcome_without_retry():
    api = bp.DeskApi("http://127.0.0.1:8765")
    class Opener:
        calls = 0
        def open(self, *args, **kwargs):
            self.calls += 1
            raise URLError("connection lost")
    api.opener = Opener()
    with pytest.raises(bp.UnknownOutcome):
        api.call("/api/auto_review", {"job": "pair-0000000000"}, mutation=True)
    assert api.opener.calls == 1


def test_desk_reader_preserves_terminal_failed_job_error_as_evidence():
    api = bp.DeskApi("http://127.0.0.1:8765")
    failed = job("pair-0000000000", "failed")
    failed["error"] = "Product actually failed on the recorded input"
    class Opener:
        def open(self, request, timeout):
            assert request.get_method() == "POST"
            body = json.loads(request.data)
            result = failed if request.full_url.endswith("/api/job") else {"jobs": [failed]}
            if request.full_url.endswith("/api/job"):
                assert body == {"id": failed["id"]}
            return BytesIO(json.dumps(result).encode())
    api.opener = Opener()
    assert api.call("/api/job", {"id": failed["id"]})["error"] == failed["error"]
    assert api.call("/api/jobs", {})["jobs"][0]["sides"]["A"]["status"] == "failed"


@pytest.mark.parametrize("route,body,response", [
    ("/api/job", {"id": "pair-0000000000"}, {"ok": False, "error": "not found"}),
    ("/api/job", {"id": "pair-0000000000"}, {"error": "not found"}),
    ("/api/jobs", {}, {"error": "inventory unavailable"}),
    ("/api/job_action", {"job": "pair-0000000000", "action": "upload"}, {"error": "upload rejected"}),
])
def test_failed_job_exception_does_not_weaken_mutation_or_protocol_error_checks(route, body, response):
    api = bp.DeskApi("http://127.0.0.1:8765")
    class Opener:
        def open(self, *args, **kwargs): return BytesIO(json.dumps(response).encode())
    api.opener = Opener()
    with pytest.raises(RuntimeError):
        api.call(route, body, mutation=route == "/api/job_action")


def test_terminal_failed_generations_remain_eligible_for_postprocessing(tmp_path):
    pipeline, cards = batch(tmp_path, count=1)
    ids = bp.receipt_jobs(receipt(pipeline, cards))
    failed = job(ids[0], "failed")
    failed["error"] = "Real generation/product failure"
    pipeline.api = Api([failed])
    recorded = []
    def process(current, entry):
        recorded.append(current["id"])
        entry["status"] = "needs_recovery"
    pipeline.process_one = process
    pipeline.tick()
    assert recorded == ids
    assert pipeline.state["status"] == "needs_attention"


def test_authorized_review_success_returns_failed_job_without_false_failure():
    api = bp.DeskApi("http://127.0.0.1:8765")
    failed = job("pair-0000000000", "failed")
    failed["error"] = "Actual product/generation failure retained"
    failed["review"]["auto_review"] = {"origin": "ai-authorized", "model": bp.MODEL}
    class Opener:
        def open(self, *args, **kwargs): return BytesIO(json.dumps(failed).encode())
    api.opener = Opener()
    result = api.call("/api/auto_review", {"job": failed["id"], "origin": "ai-authorized", "model": bp.MODEL}, mutation=True)
    assert result["error"] == failed["error"]
    failed["review"]["auto_review"] = {}
    with pytest.raises(RuntimeError, match="did not confirm"):
        api.call("/api/auto_review", {"job": failed["id"], "origin": "ai-authorized", "model": bp.MODEL}, mutation=True)


def test_human_validity_selection_exports_without_new_evaluation_or_attestation(tmp_path):
    pipeline, cards = batch(tmp_path, count=1)
    ids = bp.receipt_jobs(receipt(pipeline, cards))
    current = job(ids[0])
    current["review"] = {"validity": "有效"}
    pipeline.api = Api([current])
    pipeline.state["jobs"][ids[0]] = {"status": "needs_validity", "attempt": 1}
    pipeline.process_one = lambda *_: pytest.fail("Must not rerun completed evaluation")
    pipeline.tick()
    assert pipeline.state["status"] == "complete"
    assert [call[0] for call in pipeline.api.calls].count("/api/export") == 1
    assert all("validity" not in call[1] and "ai_confirmed" not in call[1] for call in pipeline.api.calls)


def test_saved_review_unknown_is_reconciled_without_resubmitting(tmp_path):
    pipeline, _ = batch(tmp_path, count=1)
    current = job("pair-0000000000")
    proof = [{"path": "auto-review/run/evidence.json", "sha256": "a" * 64}]
    current["review"] = {"auto_review": {"evidence_files": proof}}
    entry = {"status": "unknown", "review_evidence": proof}
    pipeline.api = Api([current])
    pipeline._finish_review(current, entry)
    assert entry["status"] == "needs_validity"
    assert not pipeline.api.calls


def test_restart_unknown_model_session_does_not_repeat_calls(tmp_path):
    pipeline, _ = batch(tmp_path, count=1)
    entry = {"status": "running", "attempt": 1}
    pipeline.api = Api([])
    with pytest.raises(bp.UnknownOutcome, match="stopped mid-job"):
        pipeline.process_one(job("pair-0000000000"), entry)
    assert not pipeline.api.calls


def test_codex_uses_separate_session_non_git_flag_and_retains_utf8_evidence(tmp_path, monkeypatch):
    pipeline, _ = batch(tmp_path, count=1)
    pipeline.api = Api([])
    folder = tmp_path / "review"
    folder.mkdir()
    captured = {}
    def runtime(self, bound, env):
        captured["bound"] = bound
        return {**env, "ATK_CODEX_API_KEY": "private-test-key"}, ["-c", 'model_provider="pair_desk"']
    monkeypatch.setattr(bp.CliConnections, "runtime", runtime)
    class Proc:
        pid = 12345
        returncode = 0
        def __init__(self, command, **kwargs):
            captured["command"], captured["kwargs"] = command, kwargs
        def communicate(self, prompt, timeout):
            Path(captured["command"][captured["command"].index("-o") + 1]).write_text(json.dumps(evaluation(), ensure_ascii=False), encoding="utf-8")
            captured["kwargs"]["stdout"].write('{"type":"thread.started","thread_id":"review-session"}\n{"type":"turn.completed"}\n')
        def poll(self): return 0
    monkeypatch.setattr(bp.subprocess, "Popen", Proc)
    result = pipeline._run_codex(job("pair-0000000000"), folder, "evaluate", "中文真实评价", bp.evaluation_schema())
    assert result["a_delivery_score"] == 4
    assert "--skip-git-repo-check" in captured["command"]
    assert captured["bound"]["id"].startswith("review-pair-0000000000-")
    assert captured["bound"]["codex_model"] == bp.MODEL
    assert captured["kwargs"]["cwd"] == folder
    assert "private-test-key" not in json.dumps(captured["command"])
    assert "private-test-key" not in (folder / "evaluate/process.json").read_text(encoding="utf-8")
    assert (folder / "evaluate/prompt.txt").read_text(encoding="utf-8") == "中文真实评价"


def test_transient_provider_retries_have_bounded_slow_backoff_and_separate_logs(tmp_path, monkeypatch):
    pipeline, _ = batch(tmp_path, count=1)
    folder = tmp_path / "review"
    folder.mkdir()
    attempts, waits = [], []
    def run_once(bound, root, phase, prompt, schema, **kwargs):
        attempts.append(phase)
        if len(attempts) < 3:
            raise bp.SessionUnavailable("429")
        return evaluation()
    monkeypatch.setattr(pipeline, "_run_codex_once", run_once)
    monkeypatch.setattr(pipeline, "_backoff", lambda seconds: waits.append(seconds))
    pipeline._run_codex(job("pair-0000000000"), folder, "evaluate", "prompt", bp.evaluation_schema())
    assert attempts == ["evaluate", "evaluate-retry-2", "evaluate-retry-3"]
    assert 298 <= waits[0] <= 300 and 898 <= waits[1] <= 900


def test_provider_backoff_restarts_skip_completed_failed_attempts(tmp_path, monkeypatch):
    pipeline, _ = batch(tmp_path, count=1)
    folder = tmp_path / "review"
    previous = folder / "evaluate"
    previous.mkdir(parents=True)
    bp.atomic_json(previous / "result.json", {"exit_code": 1, "completed": False,
                                               "failure_classification": "transient-provider"})
    pipeline.state["provider_backoff"] = {"job": "pair-0000000000", "run_id": folder.name,
                                            "phase": "evaluate", "attempt": 1, "retry_at": 0}
    attempts, waits = [], []
    monkeypatch.setattr(pipeline, "_run_codex_once", lambda job, folder, phase, *args, **kwargs: (attempts.append(phase) or evaluation()))
    monkeypatch.setattr(pipeline, "_backoff", lambda seconds: waits.append(seconds))
    pipeline._run_codex(job("pair-0000000000"), folder, "evaluate", "prompt", bp.evaluation_schema())
    assert attempts == ["evaluate-retry-2"] and waits == [0]


def test_completed_model_result_is_reused_without_another_provider_call(tmp_path, monkeypatch):
    pipeline, _ = batch(tmp_path, count=1)
    folder = tmp_path / "review"
    previous = folder / "evaluate"
    previous.mkdir(parents=True)
    bp.atomic_json(previous / "result.json", {"exit_code": 0, "completed": True})
    bp.atomic_json(previous / "final.json", evaluation())
    monkeypatch.setattr(pipeline, "_run_codex_once", lambda *args, **kwargs: pytest.fail("Duplicate completed model call"))
    assert pipeline._run_codex(job("pair-0000000000"), folder, "evaluate", "prompt", bp.evaluation_schema()) == evaluation()


def test_unknown_upload_is_read_only_until_all_uploaded_urls_are_confirmed(tmp_path, monkeypatch):
    pipeline, _ = batch(tmp_path, count=1)
    current = job("pair-0000000000")
    current["sides"]["B"]["video_url"] = ""
    entry = {"status": "unknown", "phase": "upload_unknown", "upload_dispatched_at": bp.time.time(), "run_id": "run-1"}
    pipeline.api = Api([current])
    monkeypatch.setattr(pipeline, "_persist_review", lambda *args: pytest.fail("Cannot bind half-uploaded state"))
    pipeline._finish_review(current, entry)
    assert entry["status"] == "unknown" and not pipeline.api.calls


def test_only_validity_is_ready_requires_all_other_mechanical_fields():
    current = job("pair-0000000000")
    current["check"]["items"] = [{"id": "validity", "blocking": True, "ok": False},
                                  {"id": "A_video_url", "blocking": True, "ok": False}]
    assert bp.BatchPipeline.remaining_fields(current) == ["A_video_url"]


def test_failed_capture_unknown_reply_only_reconciles_and_never_resends(tmp_path):
    pipeline, _ = batch(tmp_path, count=1)
    current = job("pair-0000000000")
    current["sides"]["A"].update(status="failed", head_sha="", session_id="", jsonl_local="")
    entry = {"failure_capture_requests": {"A": {"status": "dispatched", "attempts": 1,
                                                "expected_side_sha256": bp.side_sha256(current["sides"]["A"])}}}
    pipeline.api = Api([current])
    with pytest.raises(bp.UnknownOutcome, match="read-only"):
        pipeline._capture_failed_artifacts(current, entry)
    assert [call[0] for call in pipeline.api.calls] == ["/api/job"]
    capture_receipt(pipeline, current)
    recovered = pipeline._capture_failed_artifacts(current, entry)
    assert recovered["sides"]["A"]["status"] == "failed"
    assert entry["failure_capture_requests"]["A"]["status"] == "confirmed"
    assert all(call[0] == "/api/job" for call in pipeline.api.calls)


def test_different_capture_request_cannot_confirm_lost_reply(tmp_path):
    pipeline, _ = batch(tmp_path, count=1)
    current = job("pair-0000000000")
    capture_receipt(pipeline, current)
    entry = {"failure_capture_requests": {"A": {"status": "dispatched", "attempts": 1,
                                                "expected_side_sha256": "different-original-artifact"}}}
    pipeline.api = Api([current])
    with pytest.raises(bp.UnknownOutcome, match="different request"):
        pipeline._capture_failed_artifacts(current, entry)
    assert [call[0] for call in pipeline.api.calls] == ["/api/job"]
    assert entry["failure_capture_requests"]["A"]["status"] == "dispatched"


def test_failed_capture_records_intent_before_mutation(tmp_path):
    pipeline, _ = batch(tmp_path, count=1)
    current = job("pair-0000000000")
    current["sides"]["A"].update(status="failed", head_sha="", session_id="", jsonl_local="")
    entry = {}
    class CaptureApi(Api):
        def call(self, path, body, **kwargs):
            if path == "/api/failed_capture":
                pending = bp.read_json(pipeline.state_path)["jobs"][current["id"]]["failure_capture_requests"]["A"]
                assert pending["status"] == "dispatched" and pending["attempts"] == 1
                assert body["expected_side_sha256"] == bp.side_sha256(current["sides"]["A"])
                capture_receipt(pipeline, current)
                return {"captured": True, "job": current["id"], "side": "A"}
            return super().call(path, body, **kwargs)
    pipeline.api = CaptureApi([current])
    pipeline.state["jobs"][current["id"]] = entry
    pipeline._capture_failed_artifacts(current, entry)
    assert entry["failure_capture_requests"]["A"]["status"] == "confirmed"
    assert current["sides"]["A"]["status"] == "failed"


def test_failed_capture_retry_budget_cannot_be_exceeded(tmp_path):
    pipeline, _ = batch(tmp_path, count=1)
    current = job("pair-0000000000")
    current["sides"]["A"].update(status="failed", head_sha="", session_id="")
    pipeline.api = Api([current])
    entry = {"failure_capture_requests": {"A": {"status": "rejected", "attempts": 3}}}
    with pytest.raises(RuntimeError, match="retry budget"):
        pipeline._capture_failed_artifacts(current, entry)
    assert pipeline.api.calls == []


def test_failed_capture_all_attempt_hashes_must_remain_current(tmp_path):
    pipeline, _ = batch(tmp_path, count=1)
    current = job("pair-0000000000")
    root = pipeline.desk_home / "evidence" / current["id"]
    root.mkdir(parents=True)
    traces = [root / f"attempt-{i}.jsonl" for i in range(4)]
    for path in traces:
        path.write_text("真实失败记录", encoding="utf-8")
    capture = capture_receipt(pipeline, current, traces)
    assert pipeline._failure_capture_files(current) == [capture, *traces]
    traces[0].write_text("被修改", encoding="utf-8")
    with pytest.raises(RuntimeError, match="attempt changed"):
        pipeline._failure_capture_files(current)


def test_failed_capture_defers_only_user_validity_constraints(tmp_path):
    pipeline, _ = batch(tmp_path, count=1)
    current = job("pair-0000000000")
    capture_receipt(pipeline, current)
    current["check"]["items"] = [{"id": key, "blocking": True, "ok": False}
                                    for key in ("validity", "A_run", "A_trace_complete", "A_url")]
    assert bp.BatchPipeline.remaining_fields(current) == ["A_url"]
    current["review"]["validity"] = "有效"
    assert bp.BatchPipeline.remaining_fields(current) == ["A_run", "A_trace_complete", "A_url"]
    current["review"]["validity"] = ""
    current["sides"]["A"].pop("failure_capture")
    assert bp.BatchPipeline.remaining_fields(current) == ["A_run", "A_trace_complete", "A_url"]


@pytest.mark.parametrize("changed", ["status", "head_sha", "session_id", "jsonl_local", "workspace", "attempts"])
def test_stale_failure_capture_cannot_defer_or_bind_a_new_artifact(tmp_path, changed):
    pipeline, _ = batch(tmp_path, count=1)
    current = job("pair-0000000000")
    current["sides"]["A"]["attempts"] = [{"session_id": "cut-session-1"}]
    capture_receipt(pipeline, current)
    current["sides"]["A"][changed] = [] if changed == "attempts" else "new-artifact"
    with pytest.raises(RuntimeError, match="capture|lineage"):
        pipeline._failure_capture_files(current)
    current["check"]["items"] = [{"id": "A_trace_complete", "blocking": True, "ok": False}]
    assert bp.BatchPipeline.remaining_fields(current) == ["A_trace_complete"]


def test_finished_recorder_error_still_exposes_explicit_inactive_status(monkeypatch):
    api = bp.DeskApi("http://127.0.0.1:8765")
    response = {"recording": False, "error": "真实录制曾失败"}
    monkeypatch.setattr(api.opener, "open", lambda *a, **kw: BytesIO(json.dumps(response).encode("utf-8")))
    assert api.call("/api/rec_status", {"job": "pair-test", "side": "A"}) == response


def test_failure_capture_is_explicit_in_both_model_prompts(tmp_path):
    pipeline, _ = batch(tmp_path, count=1)
    current = job("pair-0000000000")
    current["sides"]["A"].update(status="failed", failure_capture={"receipt_path": "/actual/posthoc.json", "completed": False},
                                   attempts=[{"session_id": "failed-1"}, {"session_id": "failed-4"}])
    assert "/actual/posthoc.json" in pipeline._prepare_prompt(current, {})
    prompt = pipeline._evaluation_prompt(current, tmp_path, False)
    assert "failed-1" in prompt and "failed-4" in prompt and "全部尝试" in prompt


def test_fallback_only_executes_real_project_commands_and_cleans_known_process_group():
    result = bp.fallback_preparation({"check_commands": "python -m unittest\ncmake --build build"})
    script = result["A"]["extra_files"][".atk-review/unverified_demo.py"]
    assert "start_new_session=True" in script and "procmon.kill_tree(p.pid)" in script
    assert "fallback-check-" in script and "path.write_text(output, encoding='utf-8')" in script
    assert "python -m unittest" in script and "core behavior remains unverified" in script


@pytest.mark.parametrize("changed", ["head_sha", "session_id", "demo_run_id", "video_sha256", "jsonl_sha256"])
def test_old_scores_cannot_be_rebound_after_new_head_or_same_head_recording(changed):
    expected = {name: {"head_sha": "a" * 40, "session_id": "s-" + name, "demo_run_id": "r-1",
                       "video_sha256": "v-1", "jsonl_sha256": "t-1", "trace_url": "", "video_url": ""}
                for name in ("A", "B")}
    current = json.loads(json.dumps(expected))
    current["A"][changed] = "new-evidence"
    with pytest.raises(RuntimeError, match="refusing to rebind"):
        bp.verify_evaluation_bindings(expected, current)


def test_upload_urls_can_fill_after_evaluation_but_existing_links_cannot_change():
    expected = {name: {"head_sha": "a" * 40, "trace_url": "", "video_url": ""} for name in ("A", "B")}
    current = {name: {**expected[name], "trace_url": "https://evidence/trace", "video_url": "https://evidence/video"} for name in ("A", "B")}
    bp.verify_evaluation_bindings(expected, current)
    expected["A"]["trace_url"] = "https://evidence/old-trace"
    with pytest.raises(RuntimeError, match="changed"):
        bp.verify_evaluation_bindings(expected, current)
