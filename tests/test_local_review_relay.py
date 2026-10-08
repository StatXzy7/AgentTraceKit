import copy
import io
import json
from pathlib import Path
import tarfile
import time

import pytest

from agent_trace_kit import authorized_review_model as auth
from agent_trace_kit import local_review_worker as worker
from agent_trace_kit.batch_pipeline import UnknownOutcome, atomic_json, sha256
from agent_trace_kit.local_review_relay import LocalReviewPipeline

pytestmark = pytest.mark.usefixtures("configured_review_worker")


def request():
    job, run, phase = "pair-1111111111", "20261001-150000-abcdef12", "prepare"
    folder = f"{worker.REMOTE_EVIDENCE}/{job}/auto-review/{run}"
    return {"schema": 1, "job": job, "run_id": run, "phase": phase, "id": f"{job}-{run}-{phase}",
            "model": worker.MODEL, "authorization": {"id": worker.AUTHORIZATION, "model": worker.MODEL,
            "generation_model": "auto_model/urm", "origin": "human-authorized-local-evaluation"},
            "workspace": folder, "directory": folder + "/prepare", "schema_sha256": "a" * 64,
            "prompt_sha256": "b" * 64, "bundle_sha256": "c" * 64, "bundle_size": 100,
            "deadline": time.time() + 300, "images": []}


def terminal(folder, req):
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in (("schema.json", '{"type":"object"}'), ("prompt.txt", "真实测试"),
                          ("final.json", '{}')):
        (folder / name).write_text(content, encoding="utf-8")
    req["schema_sha256"], req["prompt_sha256"] = sha256(folder / "schema.json"), sha256(folder / "prompt.txt")
    (folder / "events.jsonl").write_text('\n'.join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "local-real-sid"}, {"type": "turn.completed"}]), encoding="utf-8")
    atomic_json(folder / "process.json", {"pid": 100, "model": worker.MODEL, "execution_host": "local-windows",
                                        "request_id": req["id"], "command": ["codex", "exec", "--model", worker.MODEL]})
    result = {"model": worker.MODEL, "authorization_id": worker.AUTHORIZATION, "execution_host": "local-windows",
              "request_id": req["id"], "schema_sha256": req["schema_sha256"], "prompt_sha256": req["prompt_sha256"],
              "session_id": "local-real-sid", "completed": True, "exit_code": 0, "failure_classification": "",
              "events_sha256": sha256(folder / "events.jsonl"), "process_sha256": sha256(folder / "process.json"),
              "final_sha256": sha256(folder / "final.json")}
    atomic_json(folder / "result.json", result)
    atomic_json(folder / "relay-request.json", req)
    return result


@pytest.mark.parametrize("key,value", [("job", "pair-0000000000"), ("model", "gpt-5.6-sol"),
    ("directory", "/etc/private"), ("run_id", "../escape"), ("phase", "prepare-local-json"),
    ("bundle_sha256", "bad"), ("deadline", float("nan")), ("images", ["/etc/private.png"])])
def test_unapproved_requests_never_reach_cli(key, value):
    req = request(); req[key] = value
    with pytest.raises(ValueError): worker.validate_request(req)


def test_expired_terminal_publishes_same_bytes_without_dispatch(tmp_path, monkeypatch):
    req = request(); req["deadline"] = time.time() - 5
    folder = tmp_path / req["id"]; terminal(folder, req)
    atomic_json(folder / "request.json", req)
    calls = []
    monkeypatch.setattr(worker, "remote", lambda source, **kwargs: calls.append(source) or {
        "confirmed": True, "result_sha256": sha256(folder / "result.json")})
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: pytest.fail("duplicate CLI dispatch"))
    worker.execute(req, tmp_path)
    assert len(calls) == 1 and (folder / "published.json").is_file()
    assert calls[0].index("['result.json']") > 0


def test_expired_unclaimed_request_cannot_dispatch(tmp_path, monkeypatch):
    req = request(); req["deadline"] = time.time() - 5
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: pytest.fail("expired CLI dispatch"))
    with pytest.raises(ValueError): worker.execute(req, tmp_path)
    assert not (tmp_path / req["id"]).exists()


def test_unknown_local_claim_cannot_dispatch(tmp_path, monkeypatch):
    req = request(); folder = tmp_path / req["id"]; folder.mkdir(); atomic_json(folder / "request.json", req)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: pytest.fail("unknown CLI redispatch"))
    with pytest.raises(RuntimeError, match="no terminal result"): worker.execute(req, tmp_path)


@pytest.mark.parametrize("filename", ["final.json", "events.jsonl", "schema.json", "prompt.txt", "process.json"])
def test_changed_terminal_evidence_is_not_accepted(tmp_path, filename):
    req = request(); terminal(tmp_path, req)
    validator = object.__new__(LocalReviewPipeline)
    validator.review_model, validator.review_authorization_id = worker.MODEL, worker.AUTHORIZATION
    (tmp_path / filename).write_text("changed", encoding="utf-8")
    with pytest.raises(UnknownOutcome): validator._validate_terminal(tmp_path, req)


def test_result_cannot_forge_completed_or_sid(tmp_path):
    req = request(); result = terminal(tmp_path, req)
    validator = object.__new__(LocalReviewPipeline)
    validator.review_model, validator.review_authorization_id = worker.MODEL, worker.AUTHORIZATION
    for key, value in [("session_id", "different"), ("completed", False), ("exit_code", True)]:
        changed = dict(result); changed[key] = value; atomic_json(tmp_path / "result.json", changed)
        with pytest.raises(UnknownOutcome): validator._validate_terminal(tmp_path, req)


@pytest.mark.parametrize("command", [None, "codex --model gpt-6.1-sol", ["codex", "--model"],
    ["codex", "--model", worker.MODEL, "--model", worker.MODEL], ["codex", 123]])
def test_malformed_command_proof_is_unknown_not_retryable(tmp_path, command):
    req = request(); result = terminal(tmp_path, req)
    process = json.loads((tmp_path / "process.json").read_text(encoding="utf-8")); process["command"] = command
    atomic_json(tmp_path / "process.json", process)
    result["process_sha256"] = sha256(tmp_path / "process.json"); atomic_json(tmp_path / "result.json", result)
    validator = object.__new__(LocalReviewPipeline)
    validator.review_model, validator.review_authorization_id = worker.MODEL, worker.AUTHORIZATION
    with pytest.raises(UnknownOutcome): validator._validate_terminal(tmp_path, req)


def test_pending_phase_is_durable_before_request_visible(tmp_path, monkeypatch):
    pipeline = object.__new__(LocalReviewPipeline)
    pipeline.review_model, pipeline.review_authorization_id = worker.MODEL, worker.AUTHORIZATION
    pipeline.batch_dir = tmp_path / "batch"; pipeline.phase_timeout = 2400; pipeline.stop = True
    pipeline.state = {"jobs": {"pair-1111111111": {}}}
    persisted = []
    monkeypatch.setattr(pipeline, "save", lambda: persisted.append(copy.deepcopy(pipeline.state)))
    import agent_trace_kit.local_review_relay as relay
    monkeypatch.setattr(relay, "authorized_model", lambda *a: request()["authorization"])
    original = relay.atomic_json
    def write(path, value):
        if path.name == "request.json":
            assert persisted[-1]["jobs"]["pair-1111111111"]["local_pending_phase"] == "prepare"
        original(path, value)
    monkeypatch.setattr(relay, "atomic_json", write)
    folder = tmp_path / "work"; folder.mkdir()
    with pytest.raises(UnknownOutcome):
        pipeline._run_codex_once({"id": "pair-1111111111", "sides": {"A": {}, "B": {}}}, folder,
                                 "prepare", "真实探针", {"type": "object", "properties": {}, "required": [], "additionalProperties": False})
    assert persisted[-1]["jobs"]["pair-1111111111"]["local_pending_phase"] == "prepare"


def test_late_terminal_resume_retains_run_and_attempt(tmp_path, monkeypatch):
    req = request(); folder = tmp_path / "evidence" / req["job"] / "auto-review" / req["run_id"] / req["phase"]
    terminal(folder, req)
    # The transport identity always uses the authoritative coordinator path.
    req["directory"] = str(folder); atomic_json(folder / "relay-request.json", req)
    pipeline = object.__new__(LocalReviewPipeline)
    pipeline.desk_home = tmp_path
    pipeline.review_model, pipeline.review_authorization_id = worker.MODEL, worker.AUTHORIZATION
    entry = {"status": "unknown", "run_id": req["run_id"], "attempt": 1, "local_pending_phase": "prepare"}
    called = []
    monkeypatch.setattr(pipeline, "save", lambda: None)
    monkeypatch.setattr(pipeline, "process_one", lambda job, e: called.append((job, copy.deepcopy(e))))
    pipeline._finish_review({"id": req["job"]}, entry)
    assert called and entry["run_id"] == req["run_id"] and entry["attempt"] == 1
    assert pipeline._resume_completed_job({"id": req["job"]}, entry)


@pytest.mark.parametrize("phase", ["upload_unknown", "uploading", "unrecognized", "recording_A", "recording_B", "failed_capture"])
def test_unknown_upload_is_read_only_and_never_replayed(tmp_path, monkeypatch, phase):
    pipeline = object.__new__(LocalReviewPipeline)
    entry = {"status": "unknown", "phase": phase, "local_pending_phase": "evaluate", "run_id": "old"}
    monkeypatch.setattr(pipeline, "_terminal_reconciliation", lambda *a: pytest.fail("upload must use its own reconciliation"))
    monkeypatch.setattr(pipeline, "process_one", lambda *a: pytest.fail("unknown upload replayed"))
    if phase == "failed_capture":
        monkeypatch.setattr(pipeline, "_capture_failed_artifacts", lambda *a: None)
        monkeypatch.setattr(pipeline, "save", lambda: None)
    pipeline._finish_review({"id": "pair-1111111111", "review": {}, "sides": {"A": {}, "B": {}}}, entry)
    assert entry["status"] == ("waiting_generation" if phase == "failed_capture" else "unknown")


def test_execute_real_layout_can_publish_and_recover(tmp_path, monkeypatch):
    req = request()
    material = tmp_path / "material"
    for name in ("A", "B", "prepare"):
        (material / name).mkdir(parents=True, exist_ok=True)
    for name in ("A", "B"):
        (material / name / "product.txt").write_text("冻结算法源码", encoding="utf-8")
    atomic_json(material / "source-manifest.json", {"files": {name: {
        "product.txt": sha256(material / name / "product.txt")} for name in ("A", "B")}})
    (material / "prepare" / "schema.json").write_text('{"type":"object"}', encoding="utf-8")
    (material / "prepare" / "prompt.txt").write_text("真实Linux探针", encoding="utf-8")
    req["schema_sha256"] = sha256(material / "prepare" / "schema.json")
    req["prompt_sha256"] = sha256(material / "prepare" / "prompt.txt")
    archive = tmp_path / "input.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        for path in material.rglob("*"):
            if path.is_file(): handle.add(path, arcname=path.relative_to(material).as_posix(), recursive=False)
    req["bundle_sha256"], req["bundle_size"] = sha256(archive), archive.stat().st_size
    root = tmp_path / "worker"; root.mkdir()
    calls = []
    class Process:
        pid, returncode = 1234, 0
        def __init__(self, command, **kwargs):
            calls.append(command)
            self.output = kwargs["stdout"]
            self.final = Path(command[command.index("-o") + 1])
        def communicate(self, prompt, **kwargs):
            assert prompt == "真实Linux探针"
            self.output.write('\n'.join(json.dumps(e) for e in [
                {"type": "thread.started", "thread_id": "real-layout-sid"}, {"type": "turn.completed"}]))
            self.final.write_text('{}', encoding="utf-8")
    def run(command, **kwargs):
        assert command[0] == "scp"
        worker.shutil.copyfile(archive, command[-1])
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(worker.subprocess, "Popen", Process)
    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.subprocess, "check_output", lambda *a, **k: "codex-cli 0.159.3")
    monkeypatch.setattr(worker.shutil, "which", lambda *a: "codex.cmd")
    monkeypatch.setattr(worker, "remote", lambda *a, **k: {"confirmed": True,
        "result_sha256": sha256(root / req["id"] / "result.json")})
    worker.execute(req, root)
    directory = root / req["id"]
    assert (directory / "published.json").is_file()
    assert sha256(directory / "schema.json") == req["schema_sha256"]
    worker.execute(req, root)
    assert len(calls) == 1


@pytest.mark.parametrize("name,kind", [("../escape", "file"), ("/absolute", "file"), ("C:/evil", "file"), ("link", "symlink")])
def test_bundle_cannot_escape_workspace(tmp_path, name, kind):
    bundle = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        info = tarfile.TarInfo(name)
        if kind == "symlink": info.type = tarfile.SYMTYPE; info.linkname = "/etc/private"
        else: info.size = 1
        archive.addfile(info, io.BytesIO(b"x") if kind == "file" else None)
    with pytest.raises(ValueError): worker.extract_bundle(bundle, tmp_path / "extract")
    assert not (tmp_path / "escape").exists()


def test_model_override_requires_matching_root_registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"; monkeypatch.setattr(auth, "REGISTRY_PATH", path)
    job = {"id": "pair-1111111111", "agent": "codex", "codex_model": "auto_model/urm"}
    with pytest.raises(RuntimeError): auth.authorized_model(job, worker.MODEL, worker.AUTHORIZATION)
    atomic_json(path, {"schema": 1, "authorizations": {worker.AUTHORIZATION: {
        "model": worker.MODEL, "generation_model": "auto_model/urm", "job_ids": [job["id"]],
        "origin": "human-authorized-local-evaluation"}}})
    if auth.os.name == "posix":
        original_stat = path.__class__.stat
        def root_owned(candidate, *args, **kwargs):
            info = original_stat(candidate, *args, **kwargs)
            if candidate in (path, path.parent):
                values = list(info); values[4] = 0; values[0] &= ~0o022
                return auth.os.stat_result(values)
            return info
        monkeypatch.setattr(path.__class__, "stat", root_owned)
    proof = auth.authorized_model(job, worker.MODEL, worker.AUTHORIZATION)
    assert proof["registry_sha256"] == sha256(path)
    for changed in ({**job, "id": "pair-0000000000"}, {**job, "codex_model": "other"}):
        with pytest.raises(RuntimeError): auth.authorized_model(changed, worker.MODEL, worker.AUTHORIZATION)
