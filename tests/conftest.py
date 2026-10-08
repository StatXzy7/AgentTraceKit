import json

import pytest


@pytest.fixture
def configured_review_worker(tmp_path, monkeypatch):
    """Synthetic operator authority, restored after each local worker test."""
    from agent_trace_kit import local_review_worker as worker

    for name in ("REMOTE_BATCH", "REMOTE_EVIDENCE", "MODEL", "GENERATION_MODEL", "AUTHORIZATION",
                 "SSH_HOST", "REMOTE_PYTHON", "JOBS", "RECOVERY_AUTHORIZATIONS"):
        monkeypatch.setattr(worker, name, getattr(worker, name))
    config = {"schema": 1, "model": "gpt-6.1-sol", "generation_model": "auto_model/urm",
              "authorization_id": "synthetic-review", "ssh_host": "example-atk",
              "remote_batch": "/srv/atk/batch", "remote_evidence": "/srv/atk/evidence",
              "remote_python": "/srv/atk/venv/bin/python",
              "job_ids": ["pair-1111111111", "pair-2222222222"] + [f"pair-{i:010x}" for i in range(1, 19)]}
    path = tmp_path / "worker.local.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    worker.configure_worker(path)
    return path
