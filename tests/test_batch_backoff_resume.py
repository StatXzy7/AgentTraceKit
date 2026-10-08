from __future__ import annotations

import copy

import pytest

from agent_trace_kit import batch_pipeline as bp


def setup(tmp_path, monkeypatch, *, local=False):
    batch = tmp_path / "batch"
    batch.mkdir()
    bp.atomic_json(batch / "create-requests.json", [{"github_repo": "repo"}])
    scoring = tmp_path / "scoring.txt"
    scoring.write_text("按实际证据评分", encoding="utf-8")
    controller = bp.BatchPipeline(batch, "http://localhost:8765", scoring, desk_home=tmp_path / "desk")
    job = {"id": "pair-0000000000", "agent": "codex", "codex_model": bp.MODEL}
    folder = tmp_path / "run"
    folder.mkdir()
    monkeypatch.setattr(controller, "_schema_compatibility_enabled", lambda *_: local)
    monkeypatch.setattr(controller, "progress", lambda *_a, **_kw: None)
    monkeypatch.setattr(bp.time, "time", lambda: 1000)
    schema = bp.evaluation_schema()
    for attempt in (1, 2):
        prefix = "evaluate-local-json" if local else "evaluate"
        label = prefix if attempt == 1 else f"{prefix}-retry-{attempt}"
        directory = folder / label
        directory.mkdir()
        bp.atomic_json(directory / "schema.json", schema)
        bp.atomic_json(directory / "result.json", {"completed": False, "exit_code": 1,
                      "failure_classification": "transient-provider", "schema_sha256": bp.sha256(directory / "schema.json")})
    controller.state["provider_backoff"] = {"job": job["id"], "run_id": folder.name, "phase": "evaluate",
                     "attempt": 2, "seconds": 900, "retry_at": 1500, "classification": "transient-provider"}
    return controller, job, folder, schema


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("deadline, expected_wait", [(1500, 500), (900, 0)])
def test_resume_second_failure_keeps_deadline_and_only_dispatches_slot_three(tmp_path, monkeypatch, local, deadline, expected_wait):
    controller, job, folder, schema = setup(tmp_path, monkeypatch, local=local)
    controller.state["provider_backoff"]["retry_at"] = deadline
    before = copy.deepcopy(controller.state["provider_backoff"])
    original = {str(p): p.read_bytes() for p in folder.glob("*/result.json")}
    waits, calls = [], []
    monkeypatch.setattr(controller, "_backoff", waits.append)
    monkeypatch.setattr(controller, "_run_codex_once", lambda _j, _f, label, *_a, **_kw: calls.append(label) or {"accepted": True})
    assert controller._run_codex(job, folder, "evaluate", "unchanged", schema) == {"accepted": True}
    assert waits == [expected_wait]
    assert calls == ["evaluate-local-json-retry-3" if local else "evaluate-retry-3"]
    assert controller.state["provider_backoff"] == before
    assert original == {str(p): p.read_bytes() for p in folder.glob("*/result.json")}


@pytest.mark.parametrize("change", ["missing", "nonterminal", "success", "permanent", "schema", "missing_schema", "malformed", "list_result", "boolean_cursor", "out_of_range", "nan", "boolean_deadline", "wrong_delay"])
def test_inconsistent_durable_backoff_never_dispatches_or_rewrites_evidence(tmp_path, monkeypatch, change):
    controller, job, folder, schema = setup(tmp_path, monkeypatch)
    result = folder / "evaluate/result.json"
    saved = bp.read_json(result)
    if change == "missing":
        result.unlink()
    elif change == "nonterminal":
        saved.pop("completed")
        bp.atomic_json(result, saved)
    elif change == "success":
        saved.update(completed=True, exit_code=0)
        bp.atomic_json(result, saved)
    elif change == "permanent":
        saved["failure_classification"] = "semantic-incomplete-output-budget"
        bp.atomic_json(result, saved)
    elif change == "schema":
        bp.atomic_json(folder / "evaluate/schema.json", {"type": "string"})
    elif change == "missing_schema":
        (folder / "evaluate/schema.json").unlink()
    elif change == "malformed":
        result.write_text("{", encoding="utf-8")
    elif change == "list_result":
        bp.atomic_json(result, [])
    elif change == "boolean_cursor":
        controller.state["provider_backoff"]["attempt"] = True
    elif change == "out_of_range":
        controller.state["provider_backoff"]["attempt"] = 3
    elif change == "nan":
        controller.state["provider_backoff"]["retry_at"] = float("nan")
    elif change == "boolean_deadline":
        controller.state["provider_backoff"]["retry_at"] = True
    elif change == "wrong_delay":
        controller.state["provider_backoff"]["seconds"] = 300
    original = {str(p): p.read_bytes() for p in folder.glob("*/*.json")}
    monkeypatch.setattr(controller, "_run_codex_once", lambda *_a, **_kw: pytest.fail("Unsafe resume must not dispatch"))
    monkeypatch.setattr(controller, "_backoff", lambda *_: pytest.fail("Unsafe resume must not reset backoff"))
    with pytest.raises(bp.UnknownOutcome):
        controller._run_codex(job, folder, "evaluate", "unchanged", schema)
    assert original == {str(p): p.read_bytes() for p in folder.glob("*/*.json")}


def test_prior_native_schema_rejection_does_not_replay_or_reset_local_slot_two_wait(tmp_path, monkeypatch):
    controller, job, folder, schema = setup(tmp_path, monkeypatch, local=True)
    local_one = folder / "evaluate-local-json"
    native = folder / "evaluate"
    local_one.rename(native)
    saved = bp.read_json(native / "result.json")
    saved["failure_classification"] = "schema-contract-rejected"
    bp.atomic_json(native / "result.json", saved)
    monkeypatch.setattr(bp, "schema_contract_rejection", lambda directory, _r: directory == native)
    waits, calls = [], []
    monkeypatch.setattr(controller, "_backoff", waits.append)
    monkeypatch.setattr(controller, "_enable_schema_compatibility", lambda *_: pytest.fail("Do not replay accepted rejection"))
    monkeypatch.setattr(controller, "_run_codex_once", lambda _j, _f, label, *_a, **_kw: calls.append(label) or {"accepted": True})
    controller._run_codex(job, folder, "evaluate", "unchanged", schema)
    assert waits == [500] and calls == ["evaluate-local-json-retry-3"]


def test_stop_during_restored_backoff_keeps_existing_slots_and_deadline(tmp_path, monkeypatch):
    controller, job, folder, schema = setup(tmp_path, monkeypatch)
    before = copy.deepcopy(controller.state["provider_backoff"])
    def stopped(_seconds):
        raise bp.UnknownOutcome("Controller stopped during provider backoff")
    monkeypatch.setattr(controller, "_backoff", stopped)
    monkeypatch.setattr(controller, "_run_codex_once", lambda *_a, **_kw: pytest.fail("Stopped backoff cannot dispatch"))
    with pytest.raises(bp.UnknownOutcome, match="stopped"):
        controller._run_codex(job, folder, "evaluate", "unchanged", schema)
    assert controller.state["provider_backoff"] == before
    assert not (folder / "evaluate-retry-3").exists()
