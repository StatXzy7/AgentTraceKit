from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agent_trace_kit import batch_pipeline as bp


def evaluation():
    return {"a_delivery_score": 4, "b_delivery_score": 3,
            "a_delivery_description": "实际执行证据A", "b_delivery_description": "实际执行证据B",
            "conclusion": "A 更好", "reason": "真实行为比较" * 12,
            "qc_status": "待有效性判断", "qc_feedback": "保留验证证据", "note": "用户授权AI评价"}


def setup(tmp_path):
    batch = tmp_path / "batch"
    batch.mkdir()
    bp.atomic_json(batch / "create-requests.json", [{"github_repo": "frozen-repo"}])
    score = tmp_path / "score.txt"
    score.write_text("按真实证据评分", encoding="utf-8")
    pipeline = bp.BatchPipeline(batch, "http://localhost:8765", score, desk_home=tmp_path / "desk")
    job = {"id": "pair-0000000000", "agent": "codex", "codex_model": bp.MODEL,
           "cli_connection": {"mode": "project", "id": "frozen-connection"}}
    folder = pipeline.desk_home / "evidence" / job["id"] / "auto-review" / "run-1"
    folder.mkdir(parents=True)
    pipeline.api = type("Api", (), {"call": lambda self, *a, **kw: {"codex_command": "codex"}})()
    return pipeline, job, folder


def rejected_event(message="json_schema must be provided. Request id: test-request", *, code="InvalidParameter", kind="BadRequest"):
    # The real CLI carries the provider error object as a JSON string.
    return {"type": "error", "message": json.dumps({"error": {"code": code, "type": kind, "message": message}})}


def native_rejection(folder, schema=None, event=None):
    directory = folder / "evaluate"
    directory.mkdir()
    bp.atomic_json(directory / "schema.json", schema or bp.evaluation_schema())
    bp.atomic_json(directory / "result.json", {"exit_code": 1, "completed": False, "model": bp.MODEL})
    (directory / "events.jsonl").write_text(json.dumps(event or rejected_event()) + "\n", encoding="utf-8")
    return directory


def fake_cli(monkeypatch, responses):
    calls = []

    def runtime(self, bound, env):
        return {**env, "CODEX_HOME": bound["id"]}, ["-c", 'model_provider="pair_desk"']

    monkeypatch.setattr(bp.CliConnections, "runtime", runtime)

    class Proc:
        pid = 12345

        def __init__(self, argv, **kwargs):
            self.argv, self.kwargs = argv, kwargs
            self.response = responses[len(calls)]
            self.returncode = self.response.get("exit_code", 0)
            calls.append({"argv": argv, "env": kwargs["env"]})

        def communicate(self, prompt, timeout):
            calls[-1]["prompt"] = prompt
            events = [{"type": "thread.started", "thread_id": "isolated-" + str(len(calls))}]
            events.extend(self.response.get("events", [{"type": "turn.completed"}]))
            for event in events:
                self.kwargs["stdout"].write(json.dumps(event) + "\n")
            if "output" in self.response:
                output = self.response["output"]
                Path(self.argv[self.argv.index("-o") + 1]).write_text(
                    output if isinstance(output, str) else json.dumps(output, ensure_ascii=False), encoding="utf-8")

        def poll(self):
            return self.returncode

    monkeypatch.setattr(bp.subprocess, "Popen", Proc)
    return calls


def rejection_response():
    return {"exit_code": 1, "events": [rejected_event(), {"type": "turn.failed"}]}


def test_native_schema_remains_default_and_output_is_locally_verified(tmp_path, monkeypatch):
    pipeline, job, folder = setup(tmp_path)
    calls = fake_cli(monkeypatch, [{"output": evaluation()}])
    assert pipeline._run_codex(job, folder, "evaluate", "原评价任务", bp.evaluation_schema()) == evaluation()
    assert "--output-schema" in calls[0]["argv"]
    assert calls[0]["prompt"] == "原评价任务"
    assert "schema_compatibility" not in pipeline.state


def test_exact_rejection_starts_independent_compatible_session_and_reuses_it(tmp_path, monkeypatch):
    pipeline, job, folder = setup(tmp_path)
    calls = fake_cli(monkeypatch, [rejection_response(), {"output": evaluation()}])
    assert pipeline._run_codex(job, folder, "evaluate", "原评价任务", bp.evaluation_schema()) == evaluation()
    assert "--output-schema" in calls[0]["argv"] and "--output-schema" not in calls[1]["argv"]
    assert calls[0]["env"]["CODEX_HOME"] != calls[1]["env"]["CODEX_HOME"]
    for call in calls:
        assert call["argv"][call["argv"].index("--model") + 1] == bp.MODEL
        assert 'model_provider="pair_desk"' in call["argv"]
    compatible = folder / "evaluate-local-json-retry-2"
    assert calls[1]["prompt"].endswith((compatible / "schema.json").read_text(encoding="utf-8"))
    assert bp.read_json(folder / "evaluate/result.json")["failure_classification"] == "schema-contract-rejected"
    accepted = bp.read_json(folder / "evaluate-attempts.json")
    assert accepted["accepted_attempt"] == 2 and accepted["accepted_directory"] == compatible.name
    assert bp.read_json(compatible / "result.json")["local_validation"] == "passed"
    reloaded = bp.BatchPipeline(pipeline.batch_dir, "http://localhost:8765", pipeline.scoring_file, desk_home=pipeline.desk_home)
    assert reloaded._run_codex(job, folder, "evaluate", "原评价任务", bp.evaluation_schema()) == evaluation()
    assert len(calls) == 2


@pytest.mark.parametrize("event", [rejected_event("json_schema must be provided elsewhere"),
                                  rejected_event(code="OtherError"), rejected_event(kind="OtherType"),
                                  {"type": "error", "message": "json_schema must be provided"}])
def test_other_permanent_rejections_never_enable_compatibility(tmp_path, monkeypatch, event):
    pipeline, job, folder = setup(tmp_path)
    calls = fake_cli(monkeypatch, [{"exit_code": 1, "events": [event, {"type": "turn.failed"}]}])
    with pytest.raises(RuntimeError, match="completed structured"):
        pipeline._run_codex(job, folder, "evaluate", "prompt", bp.evaluation_schema())
    assert len(calls) == 1 and "schema_compatibility" not in pipeline.state


@pytest.mark.parametrize("exit_code,completed,final", [(0, False, False), (1, True, False), (1, False, True)])
def test_rejection_gate_requires_exit_one_incomplete_and_no_output(tmp_path, exit_code, completed, final):
    directory = native_rejection(tmp_path)
    if final:
        bp.atomic_json(directory / "final.json", evaluation())
    assert not bp.schema_contract_rejection(directory, {"exit_code": exit_code, "completed": completed})


def test_unknown_compatible_dispatch_is_not_reissued(tmp_path, monkeypatch):
    pipeline, job, folder = setup(tmp_path)
    rejected = native_rejection(folder)
    pipeline._enable_schema_compatibility(job, rejected)
    (folder / "evaluate-local-json-retry-2").mkdir()
    fake_cli(monkeypatch, [])
    with pytest.raises(bp.UnknownOutcome, match="no terminal result"):
        pipeline._run_codex(job, folder, "evaluate", "prompt", bp.evaluation_schema())


def test_saved_native_backoff_keeps_budget_and_retry_deadline_after_compatibility(tmp_path, monkeypatch):
    pipeline, job, folder = setup(tmp_path)
    proof = folder.parent / "proof"
    proof.mkdir()
    pipeline._enable_schema_compatibility(job, native_rejection(proof))
    directory = folder / "evaluate"
    directory.mkdir()
    bp.atomic_json(directory / "result.json", {"exit_code": 1, "completed": False,
                                               "failure_classification": "transient-provider"})
    pipeline.state["provider_backoff"] = {"job": job["id"], "run_id": folder.name,
                                          "phase": "evaluate", "attempt": 1, "retry_at": 0}
    calls = fake_cli(monkeypatch, [{"output": evaluation()}])
    waits = []
    monkeypatch.setattr(pipeline, "_backoff", waits.append)
    pipeline._run_codex(job, folder, "evaluate", "prompt", bp.evaluation_schema())
    assert waits == [0] and len(calls) == 1
    assert Path(calls[0]["argv"][calls[0]["argv"].index("-o") + 1]).parent.name == "evaluate-local-json-retry-2"


def test_schema_rejection_consumes_budget_then_only_two_provider_attempts_remain(tmp_path, monkeypatch):
    pipeline, job, folder = setup(tmp_path)
    transient = {"exit_code": 1, "events": [{"type": "error", "message": "429 Too Many Requests"}, {"type": "turn.failed"}]}
    calls = fake_cli(monkeypatch, [rejection_response(), transient, transient])
    waits = []
    monkeypatch.setattr(pipeline, "_backoff", waits.append)
    with pytest.raises(bp.SessionUnavailable):
        pipeline._run_codex(job, folder, "evaluate", "prompt", bp.evaluation_schema())
    assert len(calls) == 3 and len(waits) == 1 and 898 <= waits[0] <= 900


def test_failed_previous_run_seeds_compatibility_without_another_native_call_or_budget_reset(tmp_path, monkeypatch):
    pipeline, job, old_folder = setup(tmp_path)
    job["sides"] = {name: {"head_sha": name * 40, "session_id": name, "finished_at": "finished"} for name in ("A", "B")}
    entry = {"status": "failed", "attempt": 1, "run_id": old_folder.name,
             "artifact_keys": {name: bp.artifact_key(job["sides"][name]) for name in ("A", "B")}}
    rejected = native_rejection(old_folder)
    bp.atomic_json(rejected / "process.json", {"model": bp.MODEL, "connection_id": "frozen-connection"})
    original_hash = bp.sha256(rejected / "events.jsonl")
    pipeline._seed_schema_compatibility(job, entry)
    new_folder = old_folder.parent / "run-2"
    new_folder.mkdir()
    calls = fake_cli(monkeypatch, [{"output": evaluation()}])
    pipeline._run_codex(job, new_folder, "evaluate", "prompt", bp.evaluation_schema())
    assert len(calls) == 1 and "--output-schema" not in calls[0]["argv"]
    assert bp.sha256(rejected / "events.jsonl") == original_hash and entry["attempt"] == 1
    assert bp.read_json(new_folder / "evaluate-local-json/compatibility.json") == pipeline.state["schema_compatibility"]
    assert bp.sha256(new_folder / "evaluate-local-json/native-schema-rejection/events.jsonl") == original_hash


def test_previous_rejection_requires_frozen_artifact_and_connection_before_seeding(tmp_path):
    pipeline, job, folder = setup(tmp_path)
    job["sides"] = {name: {"head_sha": name * 40, "session_id": name, "finished_at": "finished"} for name in ("A", "B")}
    entry = {"status": "failed", "attempt": 1, "run_id": folder.name,
             "artifact_keys": {name: bp.artifact_key(job["sides"][name]) for name in ("A", "B")}}
    rejected = native_rejection(folder)
    bp.atomic_json(rejected / "process.json", {"model": bp.MODEL, "connection_id": "other"})
    pipeline._seed_schema_compatibility(job, entry)
    assert "schema_compatibility" not in pipeline.state
    job["sides"]["A"]["head_sha"] = "different"
    with pytest.raises(RuntimeError, match="Frozen artifact"):
        pipeline._seed_schema_compatibility(job, entry)


@pytest.mark.parametrize("change", ["connection", "model", "batch", "proof"])
def test_compatibility_is_bound_to_original_batch_model_connection_and_rejection(tmp_path, change):
    pipeline, job, folder = setup(tmp_path)
    rejected = native_rejection(folder)
    pipeline._enable_schema_compatibility(job, rejected)
    if change == "connection":
        job["cli_connection"]["id"] = "other"
    elif change == "model":
        pipeline.state["schema_compatibility"]["model"] = "other"
    elif change == "batch":
        pipeline.state["schema_compatibility"]["batch_sha256"] = "other"
    else:
        (rejected / "events.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="compatibility"):
        pipeline._schema_compatibility_enabled(job)


@pytest.mark.parametrize("response", [{"output": evaluation(), "events": []},
                                     {"exit_code": 1, "output": evaluation()},
                                     {"events": [{"type": "turn.completed"}]}])
def test_compatible_output_requires_completed_exit_zero_and_real_final_file(tmp_path, monkeypatch, response):
    pipeline, job, folder = setup(tmp_path)
    pipeline._enable_schema_compatibility(job, native_rejection(folder))
    calls = fake_cli(monkeypatch, [response])
    with pytest.raises(RuntimeError, match="completed structured"):
        pipeline._run_codex(job, folder, "evaluate", "prompt", bp.evaluation_schema())
    assert len(calls) == 1 and not (folder / "evaluate-attempts.json").exists()


@pytest.mark.parametrize("value", [True, 1.0, 0, 6, "4"])
def test_strict_scores_are_not_coerced(tmp_path, value):
    result = evaluation()
    result["a_delivery_score"] = value
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError):
        bp.read_schema_output(path, bp.evaluation_schema())


@pytest.mark.parametrize("text", ['{"A":1,"A":2}', '{"outer":{"x":1,"x":2}}',
                                 '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}',
                                 '{"x":1e999}', '```json\n{}\n```', '{} {}'])
def test_json_parser_rejects_duplicates_nonfinite_and_nonjson_wrappers(text):
    with pytest.raises(ValueError):
        bp.strict_json(text)


@pytest.mark.parametrize("mutate", [lambda s: s.update(unknown=True),
                                   lambda s: s["properties"].update(optional={"type": "string", "pattern": "x"}),
                                   lambda s: s.update(required="A"),
                                   lambda s: s.update(additionalProperties=0),
                                   lambda s: s["properties"]["a_delivery_score"].update(minimum=True),
                                   lambda s: s["properties"]["a_delivery_score"].update(minimum=6)])
def test_unknown_or_malformed_schema_is_rejected_before_model_call(tmp_path, monkeypatch, mutate):
    pipeline, job, folder = setup(tmp_path)
    schema = bp.evaluation_schema()
    mutate(schema)
    fake_cli(monkeypatch, [])
    with pytest.raises(ValueError):
        pipeline._run_codex(job, folder, "evaluate", "prompt", schema)


def test_nested_schema_enforces_items_required_additional_and_max_items(tmp_path):
    schema = bp.preparation_schema()
    side = {"recipe_json": "{}", "extra_files": [{"path": "p", "content": "c"}] * 8, "observations": ["实际证据"]}
    bp.validate_output_schema(schema)
    bp.validate_schema_value({"A": side, "B": copy.deepcopy(side)}, schema)
    for changed in ({**side, "extra_files": side["extra_files"] * 2},
                    {**side, "extra_files": [{"path": "p"}]},
                    {**side, "extra_files": [{"path": "p", "content": "c", "extra": 1}]},
                    {**side, "observations": [False]}):
        with pytest.raises(ValueError):
            bp.validate_schema_value({"A": changed, "B": side}, schema)


def test_evaluation_schema_rejects_missing_additional_and_wrong_enum():
    for value in ({k: v for k, v in evaluation().items() if k != "reason"},
                  {**evaluation(), "validity": "有效"}, {**evaluation(), "conclusion": "same"}):
        with pytest.raises(ValueError):
            bp.validate_schema_value(value, bp.evaluation_schema())


def test_persisted_review_includes_authoritative_schema_compatibility_and_native_rejection(tmp_path, monkeypatch):
    pipeline, job, folder = setup(tmp_path)
    calls = fake_cli(monkeypatch, [rejection_response(), {"output": evaluation()}])
    pipeline._run_codex(job, folder, "evaluate", "prompt", bp.evaluation_schema())
    assert len(calls) == 2
    job["sides"] = {name: {"head_sha": name * 40, "session_id": name, "finished_at": "finished",
                           "trace_url": "trace-" + name, "video_url": "video-" + name} for name in ("A", "B")}
    job["check"] = {"items": []}
    bindings = {"A": {"head_sha": "A"}, "B": {"head_sha": "B"}}
    entry = {"artifact_keys": {name: bp.artifact_key(job["sides"][name]) for name in ("A", "B")},
             "evaluation_bindings": bindings}
    for filename in ("source-manifest.json", "prepared.json"):
        bp.atomic_json(folder / filename, {})
    (folder / "scoring-requirements.txt").write_text("真实标准", encoding="utf-8")
    saved = []
    pipeline.api = type("Api", (), {"call": lambda self, path, body, **kw: saved.append(body)})()
    monkeypatch.setattr(pipeline, "job", lambda *_: job)
    monkeypatch.setattr(pipeline, "_bindings_and_frames", lambda *_: (bindings, []))
    pipeline._persist_review(job, entry, folder, evaluation(), False)
    evidence = {item["path"]: item["sha256"] for item in saved[0]["evidence_files"]}
    prefix = "auto-review/run-1/evaluate-local-json-retry-2/"
    for filename in ("schema.json", "compatibility.json", "native-schema-rejection/events.jsonl",
                     "native-schema-rejection/result.json", "native-schema-rejection/schema.json"):
        assert evidence[prefix + filename] == bp.sha256(folder / "evaluate-local-json-retry-2" / filename)


def test_semantic_evaluation_and_recipe_guards_remain_after_schema_validation(tmp_path):
    value = evaluation()
    value["reason"] = "x"
    bp.validate_schema_value(value, bp.evaluation_schema())
    with pytest.raises(ValueError, match="too short"):
        bp.validate_evaluation(value)
    side = {"recipe_json": json.dumps({"kind": "terminal", "start": ["echo", "x"]}),
            "extra_files": [{"path": "../../bad", "content": "x"}], "observations": []}
    value = {"A": side, "B": copy.deepcopy(side)}
    bp.validate_schema_value(value, bp.preparation_schema())
    with pytest.raises(ValueError):
        bp.validate_preparation(value)
