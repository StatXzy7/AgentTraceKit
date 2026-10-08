"""Freeze a terminal failed worktree without changing its generation evidence.

This is an explicit posthoc capture, not successful completion or a CLI retry.
The caller owns store concurrency checks and any separately authorized upload.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

from . import engines


PROVENANCE = "posthoc-failed-worktree-freeze"
EXCLUDED_DIRECTORIES = frozenset({
    ".git", "target", "dist", "build", "node_modules", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".cache",
})


def side_sha256(side: dict) -> str:
    persisted = {key: value for key, value in side.items() if key != "live"}
    return hashlib.sha256(json.dumps(persisted, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _json_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _plain_path(path: str | Path) -> Path:
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise RuntimeError("Failure capture rejects symlinks and junctions")
    return path.resolve()


def _git(workspace: Path, *args: str, input: str | None = None) -> str:
    # Prevent inherited Git plumbing variables, hooks and signing from affecting
    # either the source repository or this mechanical local snapshot.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "commit.gpgsign=false",
         "-c", "core.longpaths=true",
         "-c", "user.name=AgentTraceKit failed capture", "-c", "user.email=failed-capture@localhost",
         *args], cwd=workspace, env=env, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="strict", check=False, timeout=180,
    )
    if result.returncode:
        # Git stderr may contain a configured credential-bearing remote URL.
        raise RuntimeError(f"Failure capture Git command failed: {args[0]} (exit {result.returncode})")
    return result.stdout.strip()


def _inventory(source: Path, snapshots: Path) -> tuple[dict, list[str]]:
    files, excluded = {}, []
    if snapshots.is_relative_to(source):
        excluded.append(snapshots.relative_to(source).as_posix() + "/")
    for current, directories, names in os.walk(source, followlinks=False):
        current = Path(current)
        retained = []
        for name in sorted(directories):
            path = current / name
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise RuntimeError("Failure capture rejects symlink directories")
            relative = path.relative_to(source).as_posix()
            if name in EXCLUDED_DIRECTORIES or path.resolve() == snapshots:
                excluded.append(relative + "/")
            else:
                if not path.resolve().is_relative_to(source):
                    raise RuntimeError("Failure capture path escaped the source workspace")
                retained.append(name)
        directories[:] = retained
        for name in sorted(names):
            path = current / name
            relative = path.relative_to(source).as_posix()
            if path.is_symlink():
                raise RuntimeError("Failure capture rejects symlink files")
            info = path.lstat()
            if name == ".git":
                excluded.append(relative)
                continue
            if not stat.S_ISREG(info.st_mode) or not path.resolve().is_relative_to(source):
                raise RuntimeError("Failure capture requires regular files inside the workspace")
            files[relative] = {"sha256": _hash(path), "size": info.st_size,
                               "executable": bool(info.st_mode & stat.S_IXUSR)}
    return dict(sorted(files.items())), sorted(set(excluded))


def _trace_evidence(job: dict, side: dict, source: Path, evidence: Path) -> tuple[list[dict], dict]:
    attempts = side.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise RuntimeError("Failed side has no attempt ledger")
    archived = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise RuntimeError("Invalid failed attempt ledger")
        row = {"metadata": copy.deepcopy(attempt), "files": {}}
        for field in ("stream_path", "transcript_path"):
            value = attempt.get(field)
            if not value:
                row["files"][field] = {"path": "", "sha256": "", "size": 0, "present": False}
                continue
            path = _plain_path(value)
            if not path.is_relative_to(evidence) or not path.is_file():
                raise RuntimeError("Attempt evidence is missing or outside this job evidence directory")
            row["files"][field] = {"path": str(path), "sha256": _hash(path),
                                    "size": path.stat().st_size, "present": True}
        archived.append(row)
    final = attempts[-1]
    sid = final.get("session_id")
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        raise RuntimeError("Final failed attempt has no exact session ID")
    path = archived[-1]["files"]["transcript_path"]
    if not path["present"]:
        raise RuntimeError("Final failed attempt has no archived transcript")
    trace = Path(path["path"])
    with trace.open(encoding="utf-8") as handle:
        first = json.loads(handle.readline())
    if (not isinstance(first, dict) or first.get("type") != "session_meta"
            or not isinstance(first.get("payload"), dict)
            or first["payload"].get("id") != sid):
        raise RuntimeError("Final attempt SID does not match transcript session_meta")
    cwd = first["payload"].get("cwd")
    if not isinstance(cwd, str) or not cwd or _plain_path(cwd) != source:
        raise RuntimeError("Final attempt transcript cwd does not match the source workspace")
    parsed = engines.codex_evidence(trace, prompt=job["prompt"],
                                    allow_recovered_turns=bool(side.get("completion_recovery")))
    if (parsed.get("session_id") != sid or parsed.get("reason") in {"unreadable", "malformed_json"}
            or engines.normalized_prompt(job["prompt"]) not in parsed.get("users", [])
            or parsed.get("models") != [job["codex_model"]]):
        raise RuntimeError("Final failed transcript prompt/model/session binding does not match the job")
    return archived, {"attempt": final.get("attempt"), "status": final.get("status"),
                      "session_id": sid, "transcript_path": str(trace), "sha256": path["sha256"],
                      "interruption_reason": parsed.get("reason"), "complete": False,
                      "transcript_complete": parsed.get("reason") is None}


def _result(receipt: dict, path: Path) -> dict:
    capture = {"origin": "posthoc-failed-worktree", "completed": False,
               "provenance": PROVENANCE, "complete": False, "receipt_path": str(path),
               "receipt_sha256": _hash(path), "source_head_sha": receipt["source_head_sha"],
               "source_workspace": receipt["source_workspace"],
               "source_manifest_sha256": receipt["source_manifest_sha256"],
               "snapshot_branch": receipt["snapshot_branch"], "last_attempt": receipt["last_attempt"]}
    patch = {"workspace": receipt["snapshot_workspace"], "head_sha": receipt["snapshot_head_sha"],
             "head_url": "", "pushed": False, "session_id": receipt["last_attempt"]["session_id"],
             "jsonl_local": receipt["last_attempt"]["transcript_path"], "failure_capture": capture}
    return {"patch": patch, "receipt": receipt, "receipt_path": str(path)}


def _verify_snapshot(snapshot: Path, snapshots: Path, manifest: dict) -> None:
    if _inventory(snapshot, snapshots)[0] != manifest:
        raise RuntimeError("Frozen failed capture does not match its verified manifest")
    committed = {}
    for entry in _git(snapshot, "ls-tree", "-r", "-z", "HEAD").split("\0"):
        if not entry:
            continue
        metadata, relative = entry.split("\t", 1)
        mode, kind, blob = metadata.split(" ")
        if kind != "blob":
            raise RuntimeError("Failed capture contains a non-file Git entry")
        committed[relative] = (mode, blob)
    expected = {relative: ("100755" if info["executable"] else "100644",
                           _git(snapshot, "hash-object", "--no-filters", "--", relative))
                for relative, info in manifest.items()}
    if committed != expected:
        raise RuntimeError("Failed capture commit bytes differ from the source manifest")


def _receipt(job: dict, side_name: str, side: dict, source: Path, initial: str,
             source_head: str, before: dict, excluded: list[str], archived: list[dict],
             final: dict, identity: str, snapshot: Path, branch: str, frozen_head: str) -> dict:
    evidence_files = [{"path": info["path"], "sha256": info["sha256"],
                       "kind": "stream" if field == "stream_path" else "transcript",
                       "attempt": row["metadata"].get("attempt")}
                      for row in archived for field, info in row["files"].items() if info["present"]]
    return {"schema": 1, "identity": identity, "provenance": PROVENANCE,
            "complete": False, "job": job["id"], "side": side_name,
            "prompt_sha256": hashlib.sha256(job["prompt"].encode("utf-8")).hexdigest(),
            "model": job["codex_model"], "original_side": side,
            "original_side_sha256": side_sha256(side), "source_workspace": str(source),
            "source_branch": side.get("branch", ""), "initial_head_sha": initial,
            "source_head_sha": source_head, "source_inventory": before,
            "source_manifest_sha256": _json_hash(before), "source_pre_post_equal": True,
            "excluded_directories": excluded, "exclusion_policy": sorted(EXCLUDED_DIRECTORIES),
            "attempts": archived, "evidence_files": evidence_files, "last_attempt": final,
            "snapshot_workspace": str(snapshot), "snapshot_branch": branch,
            "snapshot_head_sha": frozen_head, "pushed": False}


def _verify_source(job: dict, original: dict, side: dict, source: Path, evidence: Path,
                   snapshots: Path, source_head: str, before: dict, excluded: list[str],
                   archived: list[dict], final: dict) -> None:
    after, after_excluded = _inventory(source, snapshots)
    after_attempts, after_final = _trace_evidence(original, side, source, evidence)
    if (after != before or after_excluded != excluded or _git(source, "rev-parse", "HEAD") != source_head
            or after_attempts != archived or after_final != final or job != original):
        raise RuntimeError("Original source or attempt evidence changed during failed capture")


def capture_failed_side(job: dict, side_name: str, evidence_dir: str | Path, *,
                        expected_job: dict | None = None) -> dict:
    """Return a frozen-artifact patch; never update the caller's job or source.

    ``expected_job`` is the authoritative pre-operation snapshot. The caller must
    also compare that snapshot under its store lock before applying the patch.
    """
    if expected_job is None or job != expected_job:
        raise RuntimeError("Failure capture requires an unchanged authoritative job snapshot")
    if side_name not in {"A", "B"} or engines.job_engine(job) != "codex":
        raise RuntimeError("Failure capture requires a Codex A/B side")
    original_job = copy.deepcopy(job)
    side = original_job["sides"][side_name]
    if (side.get("status") != "failed" or side.get("live") or job.get("archived")
            or job.get("review", {}).get("locked_at")):
        raise RuntimeError("Only a quiescent, unlocked terminal failed side can be captured")
    if any(value.get("live") or value.get("status") not in {"done", "failed"}
           for value in job["sides"].values()):
        raise RuntimeError("Both sides must be terminal before failed capture")
    if not isinstance(job.get("prompt"), str) or not job["prompt"].strip() or not job.get("codex_model"):
        raise RuntimeError("Failed capture requires the frozen prompt and model")
    if not isinstance(side.get("workspace"), str) or not side["workspace"]:
        raise RuntimeError("Failed capture requires an explicit source workspace")
    source = _plain_path(side["workspace"])
    evidence = _plain_path(evidence_dir)
    if not source.is_dir() or not evidence.is_dir():
        raise RuntimeError("Failed capture source/evidence directory does not exist")
    if evidence.is_relative_to(source):
        raise RuntimeError("Failure capture evidence must be outside the original workspace")
    snapshots = evidence / "failure-snapshots"
    _plain_path(snapshots)
    source_head = _git(source, "rev-parse", "HEAD")
    initial = side.get("initial_sha") or job.get("baseline_sha")
    if not re.fullmatch(r"[a-f0-9]{40}", source_head) or not re.fullmatch(r"[a-f0-9]{40}", str(initial)):
        raise RuntimeError("Failed capture requires verified source and initial HEAD")
    if _git(source, "rev-parse", f"{initial}^{{commit}}") != initial:
        raise RuntimeError("Initial history is absent from the failed workspace")
    _git(source, "merge-base", "--is-ancestor", initial, source_head)
    before, excluded = _inventory(source, snapshots)
    archived, final = _trace_evidence(original_job, side, source, evidence)
    identity = _json_hash({"job": job["id"], "side": side_name, "original_side": side,
                           "prompt": job["prompt"], "model": job["codex_model"],
                           "source_head": source_head, "source_inventory": before, "attempts": archived})[:24]
    directory = snapshots / identity
    receipt_path = directory / "posthoc-failed-capture.json"
    snapshot = directory / "workspace"
    branch = f"codex/failed-capture/{job['id']}-{side_name.lower()}-{identity[:12]}"
    if not re.fullmatch(r"[A-Za-z0-9_/-]+", branch):
        raise RuntimeError("Unsafe failed capture job ID")
    if directory.exists():
        _plain_path(directory)
        _plain_path(receipt_path)
        _plain_path(snapshot)
        if not receipt_path.is_file():
            raise RuntimeError("Incomplete prior failed capture retained; manual inspection required")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        frozen_head = _git(snapshot, "rev-parse", "HEAD")
        expected = _receipt(original_job, side_name, side, source, initial, source_head, before,
                            excluded, archived, final, identity, snapshot, branch, frozen_head)
        if receipt != expected:
            raise RuntimeError("Existing failed capture receipt does not match current evidence")
        _verify_snapshot(snapshot, snapshots, before)
        _verify_source(job, original_job, side, source, evidence, snapshots, source_head,
                       before, excluded, archived, final)
        return _result(receipt, receipt_path)
    snapshots.mkdir(exist_ok=True)
    directory.mkdir(exist_ok=False)
    _git(directory, "clone", "--local", "--no-hardlinks", "--no-checkout", "--dissociate",
         "--", str(source), str(snapshot))
    _plain_path(snapshot)
    _git(snapshot, "remote", "remove", "origin")
    _git(snapshot, "config", "core.longpaths", "true")
    _git(snapshot, "update-ref", f"refs/heads/{branch}", source_head)
    _git(snapshot, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    _git(snapshot, "read-tree", "--empty")
    for relative in before:
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    if _inventory(snapshot, snapshots)[0] != before:
        raise RuntimeError("Failed capture copy does not match the source manifest")
    # Explicit raw blobs bypass .gitattributes clean filters, text normalization
    # and ignored-file rules: the frozen commit must contain the actual bytes.
    index = []
    for relative, info in before.items():
        blob = _git(snapshot, "hash-object", "-w", "--no-filters", "--", relative)
        mode = "100755" if info["executable"] else "100644"
        index.append(f"{mode} {blob}\t{relative}\0")
    if index:
        _git(snapshot, "update-index", "-z", "--index-info", input="".join(index))
    _git(snapshot, "commit", "--allow-empty", "-m", f"Posthoc failed worktree capture {job['id']} {side_name}")
    frozen_head = _git(snapshot, "rev-parse", "HEAD")
    _verify_source(job, original_job, side, source, evidence, snapshots, source_head,
                   before, excluded, archived, final)
    _verify_snapshot(snapshot, snapshots, before)
    receipt = _receipt(original_job, side_name, side, source, initial, source_head, before,
                       excluded, archived, final, identity, snapshot, branch, frozen_head)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return _result(receipt, receipt_path)


def prepare_capture(store, job: dict, side_name: str) -> dict:
    """Convenience adapter; the store is used only to locate job evidence."""
    return capture_failed_side(job, side_name, store.evidence_dir(job["id"]), expected_job=job)
