from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_trace_kit import batch_pipeline as bp


def error(message):
    return {"type": "error", "message": message}


@pytest.mark.parametrize("message", ["429 Too Many Requests", "HTTP 502", "HTTP 503", "HTTP 504",
                                     "stream disconnected before completion", "connection reset", "request timed out"])
def test_genuine_transport_and_rate_limit_errors_remain_transient(message):
    assert bp.classify_provider_failure([error(message)]) == "transient-provider"


@pytest.mark.parametrize("event", [error("stream disconnected before completion: Incomplete response returned, reason: max_output_tokens"),
                                 {"type": "turn.failed", "error": {"message": "stream disconnected before completion: Incomplete response returned, reason: max_output_tokens"}},
                                 error(json.dumps({"error": {"message": "stream disconnected before completion: Incomplete response returned, reason: max_output_tokens"}})),
                                 {"type": "error", "error": {"type": "response.incomplete", "reason": "max_output_tokens", "message": "stream disconnected"}}])
def test_output_budget_incomplete_is_semantic_even_when_wrapped_as_stream_error(event):
    assert bp.classify_provider_failure([error("429 Too Many Requests"), event]) == "semantic-incomplete-output-budget"


@pytest.mark.parametrize("reason", ["context_length_exceeded", "context_window_exceeded", "max_context_length"])
def test_context_budget_incomplete_is_not_a_network_retry(reason):
    assert bp.classify_provider_failure([error("stream disconnected: Incomplete response returned, reason: " + reason)]) == "semantic-incomplete-context-budget"


@pytest.mark.parametrize("message", ["stream disconnected: Incomplete response returned, reason: unknown",
                                     "stream disconnected: Incomplete response returned, reason:",
                                     "stream disconnected: response.incomplete",
                                     "stream disconnected: Incomplete response returned, reason: max_output_tokens_extra"])
def test_unknown_incomplete_reason_fails_closed_instead_of_retrying_transport(message):
    assert bp.classify_provider_failure([error(message)]) == "incomplete-response-unclassified"


def test_content_filter_incomplete_is_separate_from_provider_transport():
    assert bp.classify_provider_failure([error("stream disconnected: Incomplete response returned, reason: content_filter")]) == "semantic-incomplete-content-filter"


@pytest.mark.parametrize("event", [{"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "429 504 timeout Incomplete response returned, reason: max_output_tokens"}},
                                 {"type": "item.completed", "item": {"type": "agent_message", "text": "stream disconnected max_output_tokens"}},
                                 {"type": "turn.completed", "message": "429"},
                                 {"type": "unknown", "error": {"message": "HTTP 504"}},
                                 {"type": "turn.failed", "error": {"output": "429 stream disconnected"}},
                                 {"type": "error", "message": {"stdout": "Incomplete response returned, reason: max_output_tokens"}}])
def test_command_output_and_unrecognized_payload_fields_do_not_classify_provider_failure(event):
    assert bp.classify_provider_failure([event, {"type": "turn.failed", "error": {"message": "unspecified error"}}]) == ""


@pytest.mark.parametrize("event", [{"type": "error", "message": '{"message":"429",'},
                                 {"type": "error", "message": '{"message":"429","message":"unknown"}'},
                                 {"type": "error", "message": ["HTTP 504"]},
                                 {"type": "error", "message": '[{"message":"HTTP 504"}]'},
                                 {"type": "turn.failed", "error": 429},
                                 {"type": "error", "message": {"status_code": True}}])
def test_malformed_terminal_error_does_not_authorize_retry(event):
    assert bp.classify_provider_failure([event]) == ""


def test_structured_terminal_status_and_rate_limit_code_are_recognized():
    assert bp.classify_provider_failure([{"type": "turn.failed", "error": {"http_status": 504}}]) == "transient-provider"
    assert bp.classify_provider_failure([error(json.dumps({"error": {"code": "rate_limit_exceeded"}}))]) == "transient-provider"


def pipeline(tmp_path):
    batch = tmp_path / "batch"
    batch.mkdir()
    bp.atomic_json(batch / "create-requests.json", [{"github_repo": "repo"}])
    scoring = tmp_path / "scoring.txt"
    scoring.write_text("按实际证据评分", encoding="utf-8")
    controller = bp.BatchPipeline(batch, "http://localhost:8765", scoring, desk_home=tmp_path / "desk")
    controller.api = type("Api", (), {"call": lambda *_: {"codex_command": "codex"}})()
    job = {"id": "pair-0000000000", "agent": "codex", "codex_model": bp.MODEL,
           "cli_connection": {"mode": "project", "id": "frozen"}}
    folder = controller.desk_home / "evidence" / job["id"] / "auto-review" / "run"
    folder.mkdir(parents=True)
    return controller, job, folder


def test_future_semantic_result_is_recorded_once_without_retry_or_backoff(tmp_path, monkeypatch):
    controller, job, folder = pipeline(tmp_path)
    calls = []
    monkeypatch.setattr(bp.CliConnections, "runtime", lambda self, bound, env: (env, []))

    class Proc:
        pid = 12345
        returncode = 1

        def __init__(self, argv, **kwargs):
            calls.append(argv)
            self.log = kwargs["stdout"]

        def communicate(self, prompt, timeout):
            for event in ({"type": "thread.started", "thread_id": "actual-incomplete-session"},
                          error("stream disconnected before completion: Incomplete response returned, reason: max_output_tokens"),
                          {"type": "turn.failed", "error": {"message": "Incomplete response returned, reason: max_output_tokens"}}):
                self.log.write(json.dumps(event) + "\n")

        def poll(self):
            return 1

    monkeypatch.setattr(bp.subprocess, "Popen", Proc)
    monkeypatch.setattr(controller, "_backoff", lambda *_: pytest.fail("Semantic truncation cannot start transport backoff"))
    with pytest.raises(RuntimeError, match="completed structured"):
        controller._run_codex(job, folder, "evaluate", "unchanged prompt", bp.evaluation_schema())
    result = bp.read_json(folder / "evaluate/result.json")
    assert result["failure_classification"] == "semantic-incomplete-output-budget"
    assert result["completed"] is False and result["exit_code"] == 1
    assert len(calls) == 1 and "provider_backoff" not in controller.state
    assert not (folder / "evaluate-retry-2").exists()


def test_past_terminal_result_and_logs_remain_immutable_when_reused(tmp_path, monkeypatch):
    controller, job, folder = pipeline(tmp_path)
    directory = folder / "evaluate"
    directory.mkdir()
    bp.atomic_json(directory / "result.json", {"exit_code": 1, "completed": False,
                                               "failure_classification": "semantic-incomplete-output-budget"})
    (directory / "events.jsonl").write_text(json.dumps(error("Incomplete response returned, reason: max_output_tokens")) + "\n", encoding="utf-8")
    before = {name: (directory / name).read_bytes() for name in ("result.json", "events.jsonl")}
    monkeypatch.setattr(controller, "_run_codex_once", lambda *_a, **_kw: pytest.fail("Do not repeat recorded semantic failure"))
    with pytest.raises(RuntimeError, match="failed permanently"):
        controller._run_codex(job, folder, "evaluate", "unchanged prompt", bp.evaluation_schema())
    assert before == {name: (directory / name).read_bytes() for name in before}
    assert not (folder / "evaluate-retry-2").exists()


def test_legacy_saved_classification_is_not_rewritten(tmp_path, monkeypatch):
    controller, job, folder = pipeline(tmp_path)
    directory = folder / "evaluate"
    directory.mkdir()
    bp.atomic_json(directory / "result.json", {"exit_code": 1, "completed": False,
                                               "failure_classification": "transient-provider"})
    (directory / "events.jsonl").write_text(json.dumps(error("Incomplete response returned, reason: max_output_tokens")) + "\n", encoding="utf-8")
    before = {name: (directory / name).read_bytes() for name in ("result.json", "events.jsonl")}
    controller.stop = True
    with pytest.raises(bp.UnknownOutcome, match="during provider backoff"):
        controller._run_codex(job, folder, "evaluate", "unchanged prompt", bp.evaluation_schema())
    assert before == {name: (directory / name).read_bytes() for name in before}


# Projections of four read-only copied terminal logs; private URLs/provider bodies omitted.
ACTUAL_TERMINAL_CASES = {'prepare-local-json': [{'type': 'error',
                         'message': 'exceeded retry limit, last status: 429 Too Many Requests, '
                                    'Request id: redacted'},
                        {'type': 'turn.failed',
                         'error': {'message': 'exceeded retry limit, last status: 429 Too Many '
                                              'Requests, Request id: redacted'}}],
 'prepare-local-json-retry-2': [{'type': 'error',
                                 'message': 'exceeded retry limit, last status: 429 Too Many '
                                            'Requests, Request id: redacted'},
                                {'type': 'turn.failed',
                                 'error': {'message': 'exceeded retry limit, last status: 429 Too '
                                                      'Many Requests, Request id: redacted'}}],
 'prepare-local-json-retry-3': [{'type': 'error',
                                 'message': 'stream disconnected before completion: Incomplete '
                                            'response returned, reason: max_output_tokens'},
                                {'type': 'turn.failed',
                                 'error': {'message': 'stream disconnected before completion: '
                                                      'Incomplete response returned, reason: '
                                                      'max_output_tokens'}}],
 'evaluate-local-json': [{'type': 'error',
                          'message': 'exceeded retry limit, last status: 429 Too Many Requests, '
                                     'Request id: redacted'},
                         {'type': 'turn.failed',
                          'error': {'message': 'exceeded retry limit, last status: 429 Too Many '
                                               'Requests, Request id: redacted'}}]}


@pytest.mark.parametrize("phase,expected", [
    ("prepare-local-json", "transient-provider"),
    ("prepare-local-json-retry-2", "transient-provider"),
    ("prepare-local-json-retry-3", "semantic-incomplete-output-budget"),
    ("evaluate-local-json", "transient-provider"),
])
def test_actual_read_only_terminal_error_projections(phase, expected):
    assert bp.classify_provider_failure(ACTUAL_TERMINAL_CASES[phase]) == expected
