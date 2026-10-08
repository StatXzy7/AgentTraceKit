"""Explicit, administrator-installed permission for a separate grading model."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

REGISTRY_PATH = Path("/etc/agenttracekit/authorized-review-models.json")


def authorized_model(job: dict, model: str, authorization_id: str) -> dict:
    path = REGISTRY_PATH
    if (not isinstance(authorization_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", authorization_id)
            or not path.is_file() or path.is_symlink() or path.resolve() != path.absolute()):
        raise RuntimeError("Separate grading model has no explicit authorization")
    if os.name == "posix" and any(p.stat().st_uid != 0 or p.stat().st_mode & 0o022 for p in (path, path.parent)):
        raise RuntimeError("Grading authorization must be administrator-owned and immutable to the runner")
    raw = path.read_bytes()
    registry = json.loads(raw.decode("utf-8"))
    if not isinstance(registry, dict) or not isinstance(registry.get("authorizations"), dict):
        raise RuntimeError("Malformed separate grading authorization")
    record = registry["authorizations"].get(authorization_id, {})
    if (not isinstance(record, dict) or registry.get("schema") != 1
            or not isinstance(record.get("job_ids"), list)
            or record.get("origin") != "human-authorized-local-evaluation"
            or record.get("model") != model or job.get("id") not in record.get("job_ids", [])
            or record.get("generation_model") != job.get("codex_model") or job.get("agent") != "codex"):
        raise RuntimeError("Separate grading authorization does not match this job and model")
    return {"id": authorization_id, "model": model, "generation_model": record["generation_model"],
            "origin": record["origin"], "registry_sha256": hashlib.sha256(raw).hexdigest()}
