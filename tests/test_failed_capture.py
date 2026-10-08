from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from agent_trace_kit import failed_capture as capture


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@localhost",
                                    "-c", "commit.gpgsign=false", *args], cwd=path,
                                   encoding="utf-8").strip()


def trace(path: Path, source: Path, sid: str, prompt: str, model: str) -> None:
    records = [
        {"type": "session_meta", "payload": {"id": sid, "cwd": str(source)}},
        {"type": "turn_context", "payload": {"model": model}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "pending"}},
        {"type": "event_msg", "payload": {"type": "turn_failed", "error": "timeout"}},
    ]
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")


@pytest.fixture
def failed_job(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    (source / "baseline.txt").write_text("原始基线\n", encoding="utf-8")
    (source / ".gitignore").write_text("ignored-source.txt\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "initial")
    initial = git(source, "rev-parse", "HEAD")
    (source / "baseline.txt").write_text("失败尝试的实际改动\n", encoding="utf-8")
    (source / "product.py").write_text("raise RuntimeError('真实失败')\n", encoding="utf-8")
    (source / "ignored-source.txt").write_text("仍需冻结的源文件\n", encoding="utf-8")
    for name in ("target", "dist", "node_modules", ".venv", "__pycache__"):
        (source / name).mkdir()
        (source / name / "generated.bin").write_bytes(b"generated cache")
    evidence = tmp_path / "evidence"
    attempts_dir = evidence / "attempts"
    attempts_dir.mkdir(parents=True)
    prompt, model = "完整原始任务，保留失败现场", "gpt-test-frozen"
    attempts = []
    for number in range(1, 5):
        sid = f"failed-session-{number}"
        transcript = attempts_dir / f"a-{number:02}-{sid}.jsonl"
        stream = attempts_dir / f"a-{number:02}-stream.jsonl"
        trace(transcript, source, sid, prompt, model)
        stream.write_text(json.dumps({"type": "thread.started", "thread_id": sid}) + "\n", encoding="utf-8")
        attempts.append({"attempt": number, "agent": "codex", "status": "cut", "exit_code": 124,
                         "session_id": sid, "transcript_path": str(transcript), "stream_path": str(stream),
                         "failure": "达到总超时", "started_at": f"start-{number}", "finished_at": f"finish-{number}"})
    side = {"status": "failed", "workspace": str(source), "branch": "original-a", "initial_sha": initial,
            "head_sha": "", "head_url": "", "session_id": "", "jsonl_local": "", "pushed": False,
            "exit_code": 124, "error": "四次尝试未完成，保留现场", "finished_at": "original-finish",
            "attempts": attempts, "duration_seconds": 1234}
    job = {"id": "pair-deadbeef00", "agent": "codex", "codex_model": model, "prompt": prompt,
           "baseline_sha": initial, "review": {}, "sides": {"A": side, "B": {"status": "done"}}}
    return job, source, evidence


def capture_job(failed_job):
    job, _, evidence = failed_job
    return capture.capture_failed_side(job, "A", evidence, expected_job=copy.deepcopy(job))


def test_freeze_preserves_original_bytes_git_state_and_all_attempts(failed_job):
    job, source, evidence = failed_job
    original = copy.deepcopy(job)
    files = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    trace_bytes = {str(path): path.read_bytes() for path in (evidence / "attempts").iterdir()}
    source_status = git(source, "status", "--porcelain")
    result = capture_job(failed_job)
    assert job == original
    assert files == {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    assert source_status == git(source, "status", "--porcelain")
    assert trace_bytes == {str(path): path.read_bytes() for path in (evidence / "attempts").iterdir()}
    snapshot = Path(result["patch"]["workspace"])
    assert snapshot != source and snapshot.is_relative_to(evidence / "failure-snapshots")
    for relative in ("baseline.txt", "product.py", "ignored-source.txt"):
        assert (snapshot / relative).read_bytes() == (source / relative).read_bytes()
    assert git(source, "rev-parse", "HEAD") == original["sides"]["A"]["initial_sha"]
    git(snapshot, "merge-base", "--is-ancestor", original["sides"]["A"]["initial_sha"], "HEAD")
    assert git(snapshot, "remote") == ""
    assert result["receipt"]["original_side"] == original["sides"]["A"]
    assert len(result["receipt"]["attempts"]) == 4
    assert len(result["receipt"]["evidence_files"]) == 8
    for row in result["receipt"]["evidence_files"]:
        assert row["sha256"] == hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest()
    assert result["patch"]["session_id"] == "failed-session-4"
    assert result["patch"]["jsonl_local"] == original["sides"]["A"]["attempts"][-1]["transcript_path"]
    assert result["patch"]["failure_capture"]["completed"] is False
    assert result["receipt"]["last_attempt"]["interruption_reason"] == "turn_failed"
    assert not ({"status", "error", "exit_code", "finished_at", "attempts"} & result["patch"].keys())


def test_cache_directories_are_excluded(failed_job):
    result = capture_job(failed_job)
    snapshot = Path(result["patch"]["workspace"])
    paths = git(snapshot, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert not any(Path(path).parts[0] in capture.EXCLUDED_DIRECTORIES for path in paths)
    assert not any("failure-snapshots" in Path(path).parts for path in paths)
    assert "target/" in result["receipt"]["excluded_directories"]


def test_capture_destination_inside_source_is_rejected(failed_job):
    job, source, _ = failed_job
    evidence = source / "evidence"
    evidence.mkdir()
    with pytest.raises(RuntimeError, match="outside the original workspace"):
        capture.capture_failed_side(job, "A", evidence, expected_job=job)
    assert not (evidence / "failure-snapshots").exists()


def test_idempotence_verifies_existing_snapshot_and_receipt(failed_job):
    first = capture_job(failed_job)
    second = capture_job(failed_job)
    assert first == second
    snapshot = Path(first["patch"]["workspace"])
    (snapshot / "product.py").write_text("tampered", encoding="utf-8")
    with pytest.raises(RuntimeError, match="manifest"):
        capture_job(failed_job)


@pytest.mark.parametrize("changed", ["sid", "cwd", "prompt", "model", "missing_trace", "missing_old_stream"])
def test_mismatched_or_missing_attempt_evidence_rejected(failed_job, changed):
    job, source, evidence = failed_job
    final = job["sides"]["A"]["attempts"][-1]
    if changed == "sid":
        final["session_id"] = "wrong-session"
    elif changed == "cwd":
        trace(Path(final["transcript_path"]), evidence, final["session_id"], job["prompt"], job["codex_model"])
    elif changed == "prompt":
        job["prompt"] = "different task"
    elif changed == "model":
        job["codex_model"] = "different-model"
    elif changed == "missing_trace":
        Path(final["transcript_path"]).unlink()
    else:
        Path(job["sides"]["A"]["attempts"][0]["stream_path"]).unlink()
    with pytest.raises(RuntimeError):
        capture_job(failed_job)
    assert not (evidence / "failure-snapshots").exists()


def test_incomplete_session_requires_failed_side_and_authoritative_snapshot(failed_job):
    job, _, evidence = failed_job
    expected = copy.deepcopy(job)
    job["sides"]["A"]["status"] = "done"
    with pytest.raises(RuntimeError, match="authoritative"):
        capture.capture_failed_side(job, "A", evidence, expected_job=expected)
    with pytest.raises(RuntimeError, match="terminal failed"):
        capture.capture_failed_side(job, "A", evidence, expected_job=job)


def test_symlink_source_and_evidence_rejected(failed_job, tmp_path):
    job, source, _ = failed_job
    link = source / "linked-source.py"
    try:
        link.symlink_to(source / "product.py")
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    with pytest.raises(RuntimeError, match="symlink"):
        capture_job(failed_job)
    link.unlink()
    final = job["sides"]["A"]["attempts"][-1]
    path = Path(final["transcript_path"])
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(RuntimeError, match="symlink"):
        capture_job(failed_job)


def test_git_attributes_cannot_rewrite_frozen_bytes(failed_job):
    _, source, _ = failed_job
    (source / ".gitattributes").write_text("*.txt text eol=lf\n", encoding="utf-8")
    (source / "crlf.txt").write_bytes(b"actual\r\nfailed\r\n")
    result = capture_job(failed_job)
    snapshot = Path(result["patch"]["workspace"])
    blob = subprocess.check_output(["git", "show", "HEAD:crlf.txt"], cwd=snapshot)
    assert blob == (source / "crlf.txt").read_bytes()
    assert capture_job(failed_job) == result


def test_public_live_overlay_does_not_change_persisted_side_hash(failed_job):
    side = failed_job[0]["sides"]["A"]
    assert capture.side_sha256(side) == capture.side_sha256({**side, "live": False})
    assert capture.side_sha256(side) != capture.side_sha256({**side, "exit_code": 0})
