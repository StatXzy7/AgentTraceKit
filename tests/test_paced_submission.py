from __future__ import annotations

import hashlib
import importlib.util
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest


spec = importlib.util.spec_from_file_location("launch_paced_batch", Path(__file__).parents[1] / "scripts/linux/launch_paced_batch.py")
paced = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paced)


def setup(tmp_path, statuses=None):
    cards = [{"github_repo": f"example-repo-{i}", "prompt": "原始中文题目" + str(i), "agent": "codex",
              "cli_model": "auto_model/urm", "source_mode": "new_github", "github_owner": "StatXzy7", "github_private": False}
             for i in range(20)]
    def atomic(path, value): path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    atomic(tmp_path / "create-requests.json", cards)
    digest = hashlib.sha256(json.dumps(cards, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    receipt = {"batch_sha256": digest, "cli_connection_id": "frozen-connection", "entries": {
        card["github_repo"]: {"status": (statuses or {}).get(i, "queued"), "job_id": f"pair-{i:010x}"}
        for i, card in enumerate(cards) if (statuses or {}).get(i, "queued") != "absent"}}
    atomic(tmp_path / "submission-receipt.json", receipt)
    calls = []
    jobs = {f"pair-{i:010x}": {"id": f"pair-{i:010x}", **card, "codex_model": "auto_model/urm",
                               "cli_connection": {"id": "frozen-connection"}, "baseline_prepared": True,
                               "baseline_sha": "a" * 40, "sides": {"A": {"status": "queued"}, "B": {"status": "queued"}}}
            for i, card in enumerate(cards)}
    def post(url, route, body):
        calls.append((route, body))
        if route == "/api/job": return jobs[body["id"]]
        if route == "/api/jobs": return {"jobs": list(jobs.values())}
        if route == "/api/job_action":
            jobs[body["job"]].update(baseline_prepared=True, baseline_sha="a" * 40)
            return {"queued": True}
        if route == "/api/job_create":
            match = next(job for job in jobs.values() if job["github_repo"] == body["github_repo"])
            return {"job": match["id"], "prepared": [match["id"]]}
        raise AssertionError(route)
    now = [10000.0]
    waits = []
    def sleep(seconds): waits.append(seconds); now[0] += seconds
    launcher = SimpleNamespace(read_json=lambda path: json.loads(path.read_text(encoding="utf-8")), atomic_json=atomic,
                               validate=lambda value: None, preflight=lambda *args: "frozen-connection", post=post,
                               read_only_post=post)
    submission = paced.PacedSubmission(tmp_path, "http://127.0.0.1:8765", launcher=launcher,
                                       clock=lambda: now[0], sleep=sleep)
    return submission, cards, jobs, calls, now, waits


def test_known_prepare_failed_recovers_same_job_id_without_creating_another(tmp_path):
    submission, _, jobs, calls, _, _ = setup(tmp_path, {10: "prepare_failed"})
    known = jobs["pair-000000000a"]
    known.update(baseline_prepared=False, baseline_sha="", sides={"A": {"status": "pending"}, "B": {"status": "pending"}})
    assert submission.submit() == 0
    assert [(route, body) for route, body in calls if route == "/api/job_action"] == [
        ("/api/job_action", {"job": "pair-000000000a", "action": "enqueue"})]
    assert not any(route == "/api/job_create" for route, _ in calls)
    assert submission.receipt["entries"][known["github_repo"]]["job_id"] == "pair-000000000a"


def test_registered_jobs_prepare_by_their_frozen_id_without_job_create(tmp_path):
    submission, _, jobs, calls, _, _ = setup(tmp_path, {10: "registered"})
    jobs["pair-000000000a"].update(baseline_prepared=False, baseline_sha="", status="draft",
                                  sides={"A": {"status": "pending"}, "B": {"status": "pending"}})
    assert submission.submit() == 0
    assert [body["job"] for route, body in calls if route == "/api/job_action"] == ["pair-000000000a"]
    assert not any(route == "/api/job_create" for route, _ in calls)


def test_unknown_enqueue_cannot_dispatch_a_second_preparation(tmp_path):
    submission, _, jobs, calls, _, _ = setup(tmp_path, {10: "prepare_failed"})
    jobs["pair-000000000a"].update(baseline_prepared=False, baseline_sha="", status="preparing")
    submission.state["repos"]["example-repo-10"] = {"status": "unknown_enqueue"}
    with pytest.raises(paced.UnknownOutcome, match="unresolved"):
        submission.submit()
    assert not any(route in ("/api/job_action", "/api/job_create") for route, _ in calls)


def test_all_twenty_queued_are_skipped_and_return_complete(tmp_path):
    submission, _, _, calls, _, _ = setup(tmp_path)
    assert submission.submit() == 0
    assert submission.state["status"] == "complete"
    assert not calls


def test_unknown_pending_without_unique_exact_match_never_recreates(tmp_path):
    submission, _, jobs, calls, _, _ = setup(tmp_path, {10: "pending"})
    jobs.pop("pair-000000000a")
    with pytest.raises(paced.UnknownOutcome, match="no unique"):
        submission.submit()
    assert not any(route == "/api/job_create" for route, _ in calls)
    assert submission.receipt["entries"]["example-repo-10"]["status"] == "pending"


def test_rate_backoff_is_persisted_and_restart_honors_existing_deadline(tmp_path):
    submission, cards, _, _, now, waits = setup(tmp_path, {10: "prepare_failed"})
    submission.schedule_rate(cards[10]["github_repo"], "pair-000000000a", retry_after=600)
    deadline = submission.state["repos"][cards[10]["github_repo"]]["next_attempt_at"]
    restarted = paced.PacedSubmission(tmp_path, submission.url, launcher=submission.launcher,
                                      clock=lambda: now[0], sleep=submission.sleep)
    restarted.wait_until(restarted.state["repos"][cards[10]["github_repo"]]["next_attempt_at"], "rate_backoff", cards[10]["github_repo"])
    assert now[0] == deadline and sum(waits) == 600 and max(waits) <= 5


def test_known_repository_rate_error_honors_retry_after_then_same_id_recovers(tmp_path):
    submission, _, jobs, calls, _, waits = setup(tmp_path, {10: "prepare_failed"})
    jobs["pair-000000000a"].update(baseline_prepared=False, baseline_sha="", sides={"A": {"status": "pending"}, "B": {"status": "pending"}})
    original = submission.launcher.post
    attempts = []
    def post(url, route, body):
        if route == "/api/job_action":
            attempts.append(body["job"])
            if len(attempts) == 1:
                raise HTTPError(url + route, 400, "rejected", {"Retry-After": "600"},
                                BytesIO(json.dumps({"error": "You have created too many repositories, too quickly. Please try again later."}).encode()))
        return original(url, route, body)
    submission.launcher.post = post
    assert submission.submit() == 0
    assert attempts == ["pair-000000000a", "pair-000000000a"]
    assert sum(waits) == 600 and max(waits) <= 5
    assert not any(route == "/api/job_create" for route, _ in calls)


@pytest.mark.parametrize("key,value", [("codex_model", "different-model"), ("github_owner", "different-owner"),
                                       ("cli_connection", {"id": "new-connection"})])
def test_known_job_scope_or_model_mismatch_stops_before_any_mutation(tmp_path, key, value):
    submission, _, jobs, calls, _, _ = setup(tmp_path, {10: "prepare_failed"})
    jobs["pair-000000000a"][key] = value
    with pytest.raises(RuntimeError, match="approved card"):
        submission.submit()
    assert not any(route in ("/api/job_action", "/api/job_create") for route, _ in calls)


def test_active_connection_change_stops_before_mutations(tmp_path):
    submission, _, _, calls, _, _ = setup(tmp_path, {10: "absent"})
    submission.launcher.preflight = lambda *args: "different-connection"
    with pytest.raises(RuntimeError, match="connection changed"):
        submission.submit()
    assert not calls


def test_new_creations_are_serialized_at_least_120_seconds_apart(tmp_path):
    submission, _, _, calls, now, waits = setup(tmp_path, {18: "absent", 19: "absent"})
    assert submission.submit() == 0
    assert len([call for call in calls if call[0] == "/api/job_create"]) == 2
    assert sum(waits) == 120 and max(waits) <= 5
    assert len(submission.receipt["entries"]) == 20


def test_registered_repository_preparations_are_also_paced_120_seconds(tmp_path):
    submission, _, jobs, _, now, waits = setup(tmp_path, {18: "registered", 19: "registered"})
    for index in (18, 19):
        jobs[f"pair-{index:010x}"].update(baseline_prepared=False, baseline_sha="",
                                        sides={"A": {"status": "pending"}, "B": {"status": "pending"}})
    timestamps = []
    original = submission.launcher.post
    def post(url, route, body):
        if route == "/api/job_action":
            timestamps.append(now[0])
        return original(url, route, body)
    submission.launcher.post = post
    assert submission.submit() == 0
    assert timestamps[1] - timestamps[0] == 120 and sum(waits) == 120


def test_enqueue_dispatch_is_durable_before_http_can_crash(tmp_path):
    submission, cards, jobs, calls, _, _ = setup(tmp_path, {10: "registered"})
    jobs["pair-000000000a"].update(baseline_prepared=False, baseline_sha="",
                                  sides={"A": {"status": "pending"}, "B": {"status": "pending"}})
    original = submission.launcher.post
    def crash(url, route, body):
        if route == "/api/job_action":
            persisted = json.loads(submission.state_path.read_text(encoding="utf-8"))
            assert persisted["repos"][cards[10]["github_repo"]]["status"] == "unknown_enqueue"
            raise SystemExit("simulated hard process exit after dispatch")
        return original(url, route, body)
    submission.launcher.post = crash
    with pytest.raises(SystemExit):
        submission.submit()
    submission.launcher.post = original
    restarted = paced.PacedSubmission(tmp_path, submission.url, launcher=submission.launcher)
    with pytest.raises(paced.UnknownOutcome, match="unresolved"):
        restarted.submit()
    assert not any(route == "/api/job_action" for route, _ in calls)


def test_success_cooldown_is_durable_before_receipt_can_mark_queued(tmp_path):
    submission, cards, _, _, now, _ = setup(tmp_path, {10: "registered"})
    original = submission.launcher.atomic_json
    def fail_receipt(path, value):
        if path == submission.receipt_path:
            raise OSError("crash before queued receipt persistence")
        original(path, value)
    submission.launcher.atomic_json = fail_receipt
    with pytest.raises(OSError):
        submission.mark_queued(cards[10], "pair-000000000a")
    persisted = json.loads(submission.state_path.read_text(encoding="utf-8"))
    assert persisted["next_create_at"] == now[0] + 120
    assert json.loads(submission.receipt_path.read_text(encoding="utf-8"))["entries"][cards[10]["github_repo"]]["status"] == "registered"


def test_created_success_is_registered_until_validation_and_cooldown_are_durable(tmp_path):
    submission, _, _, calls, _, _ = setup(tmp_path, {19: "absent"})
    submission.frozen_job = lambda *args: (_ for _ in ()).throw(SystemExit("crash during baseline verification"))
    with pytest.raises(SystemExit):
        submission.submit()
    persisted = json.loads(submission.receipt_path.read_text(encoding="utf-8"))
    assert persisted["entries"]["example-repo-19"]["status"] == "registered"
    restarted = paced.PacedSubmission(tmp_path, submission.url, launcher=submission.launcher)
    assert restarted.submit() == 0
    assert restarted.state["next_create_at"] > restarted.clock()
    assert len([call for call in calls if call[0] == "/api/job_create"]) == 1


def test_actual_desk_reads_use_json_post_and_preserve_failed_job_error(monkeypatch):
    failed = {"id": "pair-000000000a", "sides": {"A": {}, "B": {}}, "status": "failed",
              "error": "You have created too many repositories, too quickly. Please try again later."}
    requests = []
    def response(request, timeout):
        requests.append(request)
        assert request.get_method() == "POST"
        assert request.get_header("Content-type") == "application/json"
        assert timeout == 600
        data = failed if request.full_url.endswith("/api/job") else {"jobs": [failed]}
        return BytesIO(json.dumps(data).encode())
    monkeypatch.setattr(paced, "urlopen", response)
    assert paced.read_only_post("http://127.0.0.1:8765", "/api/job", {"id": failed["id"]}) == failed
    assert json.loads(requests[0].data) == {"id": failed["id"]}
    assert paced.read_only_post("http://127.0.0.1:8765", "/api/jobs", {})["jobs"][0]["error"] == failed["error"]


def test_read_only_helper_rejects_protocol_error_envelopes_and_http_errors(monkeypatch):
    monkeypatch.setattr(paced, "urlopen", lambda *args, **kwargs: BytesIO(b'{"ok":false,"error":"not found"}'))
    with pytest.raises(RuntimeError, match="rejected read-only"):
        paced.read_only_post("http://127.0.0.1:8765", "/api/job", {"id": "missing"})
    def rejected(*args, **kwargs):
        raise HTTPError("http://127.0.0.1:8765/api/job", 400, "rejected", {}, BytesIO(b'{"ok":false,"error":"bad id"}'))
    monkeypatch.setattr(paced, "urlopen", rejected)
    with pytest.raises(HTTPError):
        paced.read_only_post("http://127.0.0.1:8765", "/api/job", {"id": "missing"})


def test_failed_job_reader_bypasses_generic_launcher_error_rejection(tmp_path):
    submission, _, jobs, _, _, _ = setup(tmp_path, {10: "prepare_failed"})
    jobs["pair-000000000a"]["error"] = "You have created too many repositories, too quickly. Please try again later."
    submission.launcher.post = lambda *args: (_ for _ in ()).throw(RuntimeError("generic error field rejection"))
    assert submission.frozen_job("pair-000000000a", submission.cards[10])["error"]


def test_transport_unknown_create_keeps_pending_and_never_repeats(tmp_path):
    submission, _, _, calls, _, _ = setup(tmp_path, {19: "absent"})
    previous = submission.launcher.post
    def post(url, route, body):
        if route == "/api/job_create":
            calls.append((route, body))
            raise URLError("lost response")
        return previous(url, route, body)
    submission.launcher.post = post
    with pytest.raises(paced.UnknownOutcome):
        submission.submit()
    assert submission.receipt["entries"]["example-repo-19"] == {"status": "pending"}
    assert len([call for call in calls if call[0] == "/api/job_create"]) == 1
