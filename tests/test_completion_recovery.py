"""Completion-first recovery preserves work, evidence and bounded budgets."""
import json
import time
from pathlib import Path

import pytest

from agent_trace_kit import engines, runner
from agent_trace_kit.desk_store import DeskStore
from test_codex_retry_policy import make_job
from test_codex_desk import write_rollout


def setup_run(tmp_path):
    store = DeskStore(tmp_path / "desk")
    store.save_settings({"codex_clean_single_turn": False, "codex_completion_recovery": True, "codex_side_max_attempts": 3})
    job = make_job(store, tmp_path)
    return store, job, runner.PairRunner(store)


def test_resume_arguments_use_exact_session_and_stdin():
    args = engines.codex_resume_args({"codex_sandbox": "danger-full-access"}, {}, "specific-id")
    assert args[1:3] == ["exec", "resume"]
    assert args[-2:] == ["specific-id", "-"]
    assert "--last" not in args and "--color" not in args
    assert 'sandbox_mode="danger-full-access"' in args


def test_recovery_keeps_workspace_ledger_and_attempt_files(tmp_path, monkeypatch):
    store, job, run = setup_run(tmp_path)
    work = Path(job["sides"]["A"]["workspace"])
    (work / "progress.txt").write_text("已完成的代码", encoding="utf-8")
    evidence = store.evidence_dir(job["id"])
    streams = evidence / "attempts"
    streams.mkdir()
    previous = streams / "a-01-stream.jsonl"
    previous.write_text('{"type":"thread.started","thread_id":"saved"}\n', encoding="utf-8")
    store.update_side(job["id"], "A", {"attempts": [{"attempt": 1, "session_id": "saved"}]})
    calls = []
    def attempt(*args, **kwargs):
        calls.append(kwargs["attempt"])
        assert (work / "progress.txt").read_text(encoding="utf-8") == "已完成的代码"
        return {"code": 1, "new_session": None, "completed": False, "aborted": False,
                "summary": "stream failed", "failure": "stream disconnected", "retryable": True}
    monkeypatch.setattr(run, "_run_attempt", attempt)
    monkeypatch.setattr(run, "_fresh_workspace", lambda *a, **k: pytest.fail("Work was reset"))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    run.run_side(job["id"], "A")
    assert calls == [2, 3]
    assert "saved" in previous.read_text(encoding="utf-8")
    side = store.get_job(job["id"])["sides"]["A"]
    assert [x["attempt"] for x in side["attempts"]] == [1, 2, 3]
    deadline = side["completion_deadline_epoch"]
    run.run_side(job["id"], "A")
    assert calls == [2, 3], "Restart must not reset attempt budget"
    assert store.get_job(job["id"])["sides"]["A"]["completion_deadline_epoch"] == deadline


def test_expired_deadline_does_not_launch(tmp_path, monkeypatch):
    store, job, run = setup_run(tmp_path)
    store.update_side(job["id"], "A", {"completion_deadline_epoch": time.time() - 1})
    monkeypatch.setattr(run, "_run_attempt", lambda *a, **k: pytest.fail("Budget expired"))
    run.run_side(job["id"], "A")
    assert "预算" in store.get_job(job["id"])["sides"]["A"]["error"]


def test_deadline_expiring_during_first_resume_backoff_is_recorded(tmp_path, monkeypatch):
    store, job, run = setup_run(tmp_path)
    clock = [time.time()]
    store.update_side(job["id"], "A", {"completion_deadline_epoch": clock[0] + 2,
                                     "attempts": [{"attempt": 1}]})
    monkeypatch.setattr(runner.time, "time", lambda: clock[0])
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(run, "_run_attempt", lambda *a, **k: pytest.fail("Budget expired"))
    run.run_side(job["id"], "A")
    side = store.get_job(job["id"])["sides"]["A"]
    assert side["status"] == "failed" and "预算" in side["error"]


def test_orphan_stream_after_crash_is_counted_and_kept(tmp_path, monkeypatch):
    store, job, run = setup_run(tmp_path)
    streams = store.evidence_dir(job["id"]) / "attempts"
    streams.mkdir()
    orphan = streams / 'a-02-stream.jsonl'
    orphan.write_text('{"type":"thread.started","thread_id":"orphan-sid"}\n', encoding='utf-8')
    calls = []
    def attempt(*args, **kwargs):
        calls.append(kwargs['attempt'])
        return {'code': 1, 'new_session': None, 'completed': False, 'aborted': False,
                'summary': 'permanent', 'failure': 'HTTP 401', 'retryable': False}
    monkeypatch.setattr(run, '_run_attempt', attempt)
    monkeypatch.setattr(runner.time, 'sleep', lambda _: None)
    run.run_side(job['id'], 'A')
    side = store.get_job(job['id'])['sides']['A']
    assert calls == [3]
    assert side['attempts'][0]['session_id'] == 'orphan-sid'
    assert 'orphan-sid' in orphan.read_text(encoding='utf-8')


def test_resume_validation_rejects_sibling_session(tmp_path):
    store, job, run = setup_run(tmp_path)
    home = tmp_path / "codex"
    a, b = [Path(job["sides"][s]["workspace"]) for s in ("A", "B")]
    write_rollout(home, b, "session-b", prompt=job["prompt"])
    job["sides"]["A"]["attempts"] = [{"attempt": 1, "session_id": "session-b"}]
    with pytest.raises(RuntimeError, match="工作区|会话"):
        run._completion_resume_id(job, "A", {"CODEX_HOME": str(home)})
    write_rollout(home, a, "session-a", complete=False, prompt=job["prompt"])
    job["sides"]["A"]["attempts"] = [{"attempt": 1, "session_id": "session-a"}]
    assert run._completion_resume_id(job, "A", {"CODEX_HOME": str(home)}) == "session-a"


def test_recover_does_not_reset_workspace(tmp_path, monkeypatch):
    store, job, run = setup_run(tmp_path)
    store.update_job(job["id"], {"baseline_sha": "fixture", "status": "running"})
    store.update_side(job["id"], "A", {"status": "running"})
    monkeypatch.setattr(run, "_fresh_workspace", lambda *a, **k: pytest.fail("Work was reset"))
    run.recover()
    assert store.get_job(job["id"])["sides"]["A"]["status"] == "pending"


@pytest.mark.parametrize("message,retryable", [
    ("stream disconnected before completion", True),
    ("incomplete: max_output_tokens", True),
    ("HTTP 401 Unauthorized", False),
    ("insufficient_quota", False),
    ("HTTP 400 invalid max_output_tokens", False),
    ("unknown terminal failure", False),
])
def test_completion_failure_policy(message, retryable):
    assert runner.completion_retryable({"failure": message}) is retryable


@pytest.mark.parametrize('event_users', [True, False])
def test_recovered_turn_keeps_prior_unresolved_tools_visible(tmp_path, event_users):
    path = write_rollout(tmp_path, tmp_path / 'work', complete=False)
    rows = [
        {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'cut-tool'}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Continue'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                                            'content': [{'type': 'output_text', 'text': 'done'}]}},
        {'type': 'event_msg', 'payload': {'type': 'task_complete'}},
    ]
    if not event_users:
        initial = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        initial = [r for r in initial if r.get('payload', {}).get('type') != 'user_message']
        path.write_text(''.join(json.dumps(r) + '\n' for r in initial), encoding='utf-8')
        rows[1] = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                                                     'content': [{'type': 'input_text', 'text': 'Continue'}]}}
    rows.insert(1, {'type': 'event_msg', 'payload': {'type': 'turn_failed'}})
    with path.open('a', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')
    assert engines.codex_evidence(path)['reason'] in ('dangling_tool_result', 'turn_failed')
    evidence = engines.codex_evidence(path, allow_recovered_turns=True)
    assert evidence['reason'] is None
    assert evidence['historical_unresolved_tool_calls'] == ['cut-tool']
    # A dangling tool in the CURRENT turn must still block completion.
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps({'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'new-tool'}}) + '\n')
    assert engines.codex_evidence(path, allow_recovered_turns=True)['reason'] == 'dangling_tool_result'
