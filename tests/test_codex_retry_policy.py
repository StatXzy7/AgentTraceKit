"""Bounded Codex retries, using local fixtures only."""

import io
import random
import threading
from pathlib import Path

import pytest

from agent_trace_kit import engines, runner
from agent_trace_kit.cli_config import CliConnections
from agent_trace_kit.desk_store import DeskStore


@pytest.fixture(autouse=True)
def cli_capability_fixture(monkeypatch):
    from agent_trace_kit import run_policy
    monkeypatch.setattr(run_policy, "preflight", lambda *a: {"capability_check": "fixture"})


def make_job(store, tmp_path):
    job = store.create_job({"prompt": "测试重试", "agent": "codex"})
    store.update_job(job["id"], {"cli_connection": {
        "mode": "project", "base_url": "https://provider.example/v1", "id": "revision",
    }})
    for side in ("A", "B"):
        work = tmp_path / job["id"] / side
        work.mkdir(parents=True)
        store.update_side(job["id"], side, {"workspace": str(work), "branch": side})
    return store.get_job(job["id"])


def test_dedicated_config_disables_both_internal_retry_layers(tmp_path):
    cli = CliConnections(tmp_path)
    cli.save("codex", {"mode": "project", "base_url": "https://provider.example/v1",
                       "api_key": "fixture-key", "model": "fixture"})
    job = {"id": "test", "agent": "codex", "cli_connection": cli.bind("codex")}
    env, _ = cli.runtime(job, {})
    config = (Path(env["CODEX_HOME"]) / "config.toml").read_text(encoding="utf-8")
    assert "request_max_retries = 0" in config
    assert "stream_max_retries = 0" in config
    assert "fixture-key" not in config


@pytest.mark.parametrize("message,retryable", [
    ("exceeded retry limit, last status: 429 Too Many Requests", True),
    ("unexpected status 504 Gateway Timeout", True),
    ("HTTP 401 Unauthorized", False),
    ("HTTP 403 Forbidden", False),
    ("stream disconnected before completion: unexpected status 400 Bad Request", False),
    ("stream disconnected before completion: unexpected status 404 Not Found", False),
    ("HTTP 400 Bad Request: timeout must be a positive integer", False),
    ("insufficient_quota", False),
    ("blocked by policy", False),
    ("unknown terminal failure", False),
])
def test_failure_classification(message, retryable):
    signal = {}
    engines.consume_codex_event({"type": "turn.failed", "error": {"message": message}}, signal, lambda _: None)
    assert signal["retryable"] is retryable
    assert message in signal["error_message"]


def test_item_warnings_keep_message_without_failing_turn():
    lines, signal = [], {}
    engines.consume_codex_event({"type": "item.completed", "item": {
        "type": "error", "message": "Model metadata not found",
    }}, signal, lines.append)
    assert "Model metadata not found" in lines[0]
    assert "result_is_error" not in signal


def test_codex_backoff_is_minutes_not_seconds():
    for seed in range(20):
        rng = random.Random(seed)
        waits = [runner.codex_retry_backoff_seconds(i, rng) for i in range(2, 8)]
        assert 60 <= waits[0] <= 75
        assert 120 <= waits[1] <= 150
        assert all(60 <= wait <= 900 for wait in waits)


@pytest.mark.parametrize("retryable,expected", [(True, 3), (False, 1)])
def test_attempt_cap_and_permanent_failure_stop(tmp_path, monkeypatch, retryable, expected):
    store = DeskStore(tmp_path / "desk")
    job = make_job(store, tmp_path)
    run = runner.PairRunner(store)
    calls, recopies, sleeps = [], [], []
    def attempt(*args, **kwargs):
        calls.append(kwargs["attempt"])
        return {"code": 1, "new_session": None, "completed": False, "aborted": False,
                "summary": "failed", "failure": "429" if retryable else "401",
                "retryable": retryable}
    monkeypatch.setattr(run, "_run_attempt", attempt)
    monkeypatch.setattr(run, "_fresh_workspace", lambda *a, **k: recopies.append(1))
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    run.run_side(job["id"], "A")
    assert len(calls) == expected
    assert len(recopies) == expected - 1
    assert sum(sleeps) >= (180 if retryable else 0)
    assert store.get_job(job["id"])["sides"]["A"]["status"] == "failed"
    if not retryable:
        error = store.get_job(job["id"])["sides"]["A"]["error"]
        assert "停止自动重试" in error
        assert "每次断流" not in error


def test_max_pairs_run_both_sides_concurrently_on_same_endpoint(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    store.save_settings({"max_parallel_pairs": 16})
    jobs = [make_job(store, tmp_path) for _ in range(16)]
    for job in jobs:
        store.update_job(job["id"], {"baseline_sha": "fixture", "baseline_prepared": True})
    run = runner.PairRunner(store)
    all_entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
    calls = set()
    def attempt(job_id, side, *args, **kwargs):
        with lock:
            calls.add((job_id, side))
            if len(calls) == 32:
                all_entered.set()
        release.wait(15)
        return {"code": 1, "new_session": None, "completed": False, "aborted": False,
                "summary": "fixture stop", "failure": "fixture stop", "retryable": False}
    monkeypatch.setattr(run, "_run_attempt", attempt)
    assert run.ensure_workers() == 16
    try:
        for job in jobs:
            run.enqueue(job["id"])
        assert all_entered.wait(10), f"Only {len(calls)} sides could run concurrently"
        assert calls == {(job["id"], side) for job in jobs for side in ("A", "B")}
    finally:
        for job in jobs:
            for side in ("A", "B"):
                run.abort_side(job["id"], side)
        release.set()
        run.stop()
        for worker in run._workers:
            worker.join(3)
    assert not any(worker.is_alive() for worker in run._workers)


def test_raw_policy_rejection_is_diagnostic_only(tmp_path):
    raw = 'ERROR codex_core::tools::router: error=exec_command failed: rejected: blocked by policy\n'
    proc = type("Proc", (), {"stdout": io.StringIO(raw)})()
    signal, log = {"agent": "codex"}, io.StringIO()
    runner.PairRunner._pump_stream(proc, log, tmp_path / "stream.jsonl", threading.Event(), signal)
    assert signal == {"agent": "codex"}
    assert "blocked by policy" in log.getvalue()
    assert (tmp_path / "stream.jsonl").read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("terminal", ["exhausted", "permanent_failure", "partial_stream"])
def test_runner_routes_only_dedicated_codex_and_does_not_multiply_relay_failures(tmp_path, monkeypatch, terminal):
    from agent_trace_kit import codex_relay
    store = DeskStore(tmp_path / "desk")
    store.cli_connections.save("codex", {"mode": "project", "base_url": "https://provider.example/v1",
                                        "api_key": "upstream-key", "model": "fixture"})
    job = store.create_job({"prompt": "test", "agent": "codex"})
    run = runner.PairRunner(store)
    flags, child_keys = [], []
    original_enter = codex_relay.CodexRelay.__enter__
    def enter(relay):
        if terminal != "partial_stream":
            getattr(relay, terminal).set()
        return original_enter(relay)
    monkeypatch.setattr(codex_relay.CodexRelay, "__enter__", enter)
    def execute(*args, **kwargs):
        flags.extend(kwargs["connection_args"])
        child_keys.append(kwargs["env"].get("ATK_CODEX_API_KEY"))
        if kwargs["session_job"].get("cli_connection", {}).get("mode") == "project":
            assert "127.0.0.1" in kwargs["env"]["NO_PROXY"]
            assert kwargs["env"]["no_proxy"] == kwargs["env"]["NO_PROXY"]
        return {"retryable": True}
    monkeypatch.setattr(run, "_execute_attempt", execute)
    def attempt():
        return run._run_attempt(job["id"], "A", str(tmp_path), tmp_path, tmp_path / "run.log",
                                tmp_path / "stream.jsonl", timeout=20, stall_after=0,
                                poll_every=1, attempt=1)
    assert attempt()["retryable"] is (terminal != "permanent_failure")
    assert child_keys[0] and child_keys[0] != "upstream-key"
    assert any("base_url=\"http://127.0.0.1:" in value for value in flags)
    flags.clear()
    store.update_job(job["id"], {"cli_connection": {"mode": "inherit"}})
    assert attempt()["retryable"] is True
    assert not flags
