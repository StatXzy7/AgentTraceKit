"""Activate a reviewed failure-capture repair after all Desk work is idle.

This maintenance watcher never restarts active generation or recording work.
It reads the complete inventory, including jobs outside the supervised batch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

from agent_trace_kit.batch_pipeline import DeskApi, atomic_json, read_json, receipt_jobs, sha256
from agent_trace_kit.desk_store import utc_now

BUSY = {"pending", "running", "preparing", "collecting"}


def idle_inventory(jobs: list[dict], home: Path, batch_ids: set[str]) -> bool:
    if not isinstance(jobs, list) or not jobs or len({j.get("id") for j in jobs}) != len(jobs):
        return False
    selected = {j["id"]: j for j in jobs if j.get("id") in batch_ids}
    if set(selected) != batch_ids:
        return False
    for job in jobs:
        sides = job.get("sides")
        if not isinstance(sides, dict) or set(sides) != {"A", "B"}:
            return False
        for side in sides.values():
            if side.get("live") or side.get("status") in BUSY:
                return False
            if side.get("demo", {}).get("status") in {"queued", "running"} or side.get("preview", {}).get("running"):
                return False
        if job.get("id") in batch_ids and (job.get("agent") != "codex" or job.get("codex_model") != "auto_model/urm"
                or any(s.get("status") not in {"done", "failed"} for s in sides.values())):
            return False
    # Even a stale registry requires investigation; this watcher never reaps it.
    return not any((home / "running").glob("*.json"))


def verify_code(root: Path, manifest: dict) -> None:
    files = manifest.get("files")
    if not isinstance(files, dict) or not files or manifest.get("reviewed") is not True:
        raise RuntimeError("A reviewed source manifest is required")
    for relative, digest in files.items():
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or sha256(path) != digest:
            raise RuntimeError("Reviewed repair source changed")


def runtime_idle(api: DeskApi, jobs: list[dict]) -> bool:
    """Read real recorder/preview state, absent from public job objects."""
    try:
        capability = api.call("/api/failed_capture_status", {})
    except RuntimeError:
        capability = None
    if capability is not None:
        return (capability.get("enabled") is True and all(capability.get(key) == [] for key in
                ("active_jobs", "busy_jobs", "recording_jobs", "preview_keys", "preview_pending_keys")))
    overview = api.call("/api/ports_overview", {})
    previews = overview.get("previews")
    if not isinstance(previews, list) or any(item.get("running") is not False for item in previews):
        return False
    for job in jobs:
        for side in ("A", "B"):
            if api.call("/api/rec_status", {"job": job["id"], "side": side}).get("recording") is not False:
                return False
    return True


def run(batch: Path, home: Path, code: Path, *, poll: int = 30) -> None:
    manifest = read_json(batch / "failed-capture-upgrade-manifest.json")
    verify_code(code, manifest)
    requests = json.loads((batch / "create-requests.json").read_text(encoding="utf-8"))
    expected = hashlib.sha256(json.dumps(requests, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    receipt = read_json(batch / "submission-receipt.json")
    if receipt.get("batch_sha256") != expected or manifest.get("batch_sha256") != expected:
        raise RuntimeError("Repair is not bound to this batch")
    ids = set(receipt_jobs(receipt))
    if len(ids) != 20 or manifest.get("batch_ids") != sorted(ids):
        raise RuntimeError("Repair batch identity mismatch")
    state_path = batch / "failed-capture-upgrade-state.json"
    state = read_json(state_path) if state_path.exists() else {"schema": 1, "phase": "waiting_quiescence"}
    api = DeskApi("http://127.0.0.1:8765", timeout=15)
    if state.get("phase") == "complete":
        return
    quiet = 0
    while True:
        verify_code(code, manifest)
        try:
            jobs = api.call("/api/jobs", {}).get("jobs", [])
            idle = idle_inventory(jobs, home, ids) and runtime_idle(api, jobs)
        except RuntimeError:
            idle = False
        quiet = quiet + 1 if idle else 0
        state.update(phase="waiting_quiescence", checked_at=utc_now(), consecutive_idle_checks=quiet)
        atomic_json(state_path, state)
        if quiet >= 2:
            break
        time.sleep(poll)
    # A final fresh read narrows the gap before service activation. No queued,
    # running, preparing, collecting, recording or preview process is accepted.
    jobs = api.call("/api/jobs", {}).get("jobs", [])
    if not (idle_inventory(jobs, home, ids) and runtime_idle(api, jobs)):
        raise RuntimeError("Desk became busy before activation; no restart performed")
    state.update(phase="activating_reviewed_repair", activated_at=utc_now())
    atomic_json(state_path, state)
    subprocess.run(["systemctl", "stop", "agenttracekit-algorithm20.service"], check=True, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        capability = api.call("/api/failed_capture_status", {})
        loaded = capability.get("enabled") is True
    except RuntimeError:
        loaded = False
    if not loaded:
        subprocess.run(["systemctl", "restart", "agenttracekit.service"], check=True, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(24):
        try:
            jobs = api.call("/api/jobs", {}).get("jobs", [])
            capability = api.call("/api/failed_capture_status", {})
            if (idle_inventory(jobs, home, ids) and capability.get("enabled") is True
                    and all(capability.get(key) == [] for key in
                            ("active_jobs", "busy_jobs", "recording_jobs", "preview_keys", "preview_pending_keys"))):
                break
        except RuntimeError:
            pass
        time.sleep(5)
    else:
        raise RuntimeError("Restarted Desk readiness not confirmed; controller remains stopped")
    verify_code(code, manifest)
    subprocess.run(["systemctl", "start", "agenttracekit-algorithm20.service"], check=True, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    state.update(phase="complete", finished_at=utc_now(), controller_resumed=True)
    atomic_json(state_path, state)
    print("Reviewed failure capture activated at global quiescence; batch controller resumed", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--desk-home", type=Path, required=True)
    parser.add_argument("--code-root", type=Path, required=True)
    args = parser.parse_args()
    run(args.batch_dir.resolve(), args.desk_home.resolve(), args.code_root.resolve())
