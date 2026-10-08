import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/linux/resume_failed_capture_upgrade.py"
spec = importlib.util.spec_from_file_location("capture_upgrade", SCRIPT)
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


def job(identity="pair-test", status="done"):
    return {"id": identity, "agent": "codex", "codex_model": "auto_model/urm",
            "sides": {n: {"status": status, "live": False} for n in ("A", "B")}}


def capability():
    return {"enabled": True, "active_jobs": [], "busy_jobs": [], "recording_jobs": [],
            "preview_keys": [], "preview_pending_keys": []}


@pytest.mark.parametrize("status", ["pending", "preparing", "running", "collecting"])
def test_upgrade_never_restarts_busy_other_batch(tmp_path, status):
    assert not upgrade.idle_inventory([job(), job("older-pair", status)], tmp_path, {"pair-test"})


def test_upgrade_includes_archived_live_registry_recording_and_preview(tmp_path):
    current = job()
    other = job("old")
    other["archived"] = True
    other["sides"]["A"]["live"] = True
    assert not upgrade.idle_inventory([current, other], tmp_path, {"pair-test"})
    other["sides"]["A"]["live"] = False
    assert upgrade.idle_inventory([current, other], tmp_path, {"pair-test"})
    for key, state in (("demo", {"status": "queued"}), ("preview", {"running": True})):
        other["sides"]["A"][key] = state
        assert not upgrade.idle_inventory([current, other], tmp_path, {"pair-test"})
        other["sides"]["A"].pop(key)
    (tmp_path / "running").mkdir()
    (tmp_path / "running" / "stale.json").write_text("{}", encoding="utf-8")
    assert not upgrade.idle_inventory([current], tmp_path, {"pair-test"})


def test_upgrade_accepts_terminal_failure_but_requires_batch_inventory_and_model(tmp_path):
    current = job(status="failed")
    assert upgrade.idle_inventory([current], tmp_path, {"pair-test"})
    assert not upgrade.idle_inventory([current], tmp_path, {"missing"})
    assert not upgrade.idle_inventory([current, current], tmp_path, {"pair-test"})
    current["codex_model"] = "other"
    assert not upgrade.idle_inventory([current], tmp_path, {"pair-test"})


def test_upgrade_requires_reviewed_unchanged_source(tmp_path):
    source = tmp_path / "repair.py"
    source.write_text("pass\n", encoding="utf-8")
    manifest = {"reviewed": True, "files": {"repair.py": upgrade.sha256(source)}}
    upgrade.verify_code(tmp_path, manifest)
    with pytest.raises(RuntimeError, match="reviewed"):
        upgrade.verify_code(tmp_path, {**manifest, "reviewed": False})
    source.write_text("changed\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="changed"):
        upgrade.verify_code(tmp_path, manifest)


def setup_batch(tmp_path):
    batch = tmp_path / "batch"
    code = tmp_path / "code"
    batch.mkdir()
    code.mkdir()
    source = code / "repair.py"
    source.write_text("pass\n", encoding="utf-8")
    cards = [{"github_repo": f"repo-{i}", "prompt": "冻结题目"} for i in range(20)]
    digest = hashlib.sha256(json.dumps(cards, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    ids = [f"pair-{i:010x}" for i in range(20)]
    upgrade.atomic_json(batch / "submission-receipt.json", {"batch_sha256": digest, "entries": {
        card["github_repo"]: {"status": "queued", "job_id": identity} for card, identity in zip(cards, ids)}})
    (batch / "create-requests.json").write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    upgrade.atomic_json(batch / "failed-capture-upgrade-manifest.json", {
        "reviewed": True, "files": {"repair.py": upgrade.sha256(source)}, "batch_sha256": digest, "batch_ids": sorted(ids)})
    return batch, code, [job(identity) for identity in ids]


def test_watcher_never_runs_service_commands_while_generation_busy(tmp_path, monkeypatch):
    batch, code, jobs = setup_batch(tmp_path)
    jobs[-1]["sides"]["B"]["status"] = "running"
    class Api:
        def __init__(self, *a, **kw): pass
        def call(self, *a, **kw): return {"jobs": jobs}
    monkeypatch.setattr(upgrade, "DeskApi", Api)
    monkeypatch.setattr(upgrade.subprocess, "run", lambda *a, **kw: pytest.fail("Cannot restart active generation"))
    def end_poll(_): raise RuntimeError("test ends after one safe poll")
    monkeypatch.setattr(upgrade.time, "sleep", end_poll)
    with pytest.raises(RuntimeError, match="test ends"):
        upgrade.run(batch, tmp_path / "desk", code)
    assert upgrade.read_json(batch / "failed-capture-upgrade-state.json")["phase"] == "waiting_quiescence"


def test_fresh_busy_read_prevents_restart_after_two_idle_samples(tmp_path, monkeypatch):
    batch, code, jobs = setup_batch(tmp_path)
    class Api:
        count = 0
        def __init__(self, *a, **kw): pass
        def call(self, path, body):
            if path != "/api/jobs":
                return capability()
            self.count += 1
            if self.count == 3:
                jobs[-1]["sides"]["B"]["live"] = True
            return {"jobs": jobs}
    monkeypatch.setattr(upgrade, "DeskApi", Api)
    monkeypatch.setattr(upgrade.time, "sleep", lambda _: None)
    monkeypatch.setattr(upgrade.subprocess, "run", lambda *a, **kw: pytest.fail("Fresh busy read must block restart"))
    with pytest.raises(RuntimeError, match="became busy"):
        upgrade.run(batch, tmp_path / "desk", code)


def test_unknown_activation_reuses_loaded_capability_without_second_desk_restart(tmp_path, monkeypatch):
    batch, code, jobs = setup_batch(tmp_path)
    upgrade.atomic_json(batch / "failed-capture-upgrade-state.json", {"phase": "activating_reviewed_repair"})
    class Api:
        def __init__(self, *a, **kw): pass
        def call(self, path, body):
            return {"jobs": jobs} if path == "/api/jobs" else capability()
    calls = []
    monkeypatch.setattr(upgrade, "DeskApi", Api)
    monkeypatch.setattr(upgrade.time, "sleep", lambda _: None)
    monkeypatch.setattr(upgrade.subprocess, "run", lambda argv, **kw: calls.append(argv))
    upgrade.run(batch, tmp_path / "desk", code)
    assert calls == [["systemctl", "stop", "agenttracekit-algorithm20.service"],
                     ["systemctl", "start", "agenttracekit-algorithm20.service"]]
    assert upgrade.read_json(batch / "failed-capture-upgrade-state.json")["controller_resumed"] is True


@pytest.mark.parametrize("activity", ["recording", "preview"])
def test_old_desk_media_uses_real_readonly_status_not_job_overlay(activity):
    class OldApi:
        def call(self, path, body):
            if path == "/api/failed_capture_status":
                raise RuntimeError("old desk has no endpoint")
            if path == "/api/ports_overview":
                return {"previews": [{"key": "pair-test/A", "running": True}] if activity == "preview" else []}
            if path == "/api/rec_status":
                return {"recording": activity == "recording" and body["side"] == "B"}
            raise AssertionError(path)
    assert not upgrade.runtime_idle(OldApi(), [job()])


@pytest.mark.parametrize("key", ["active_jobs", "busy_jobs", "recording_jobs", "preview_keys", "preview_pending_keys"])
def test_loaded_desk_media_and_capture_activity_always_blocks_upgrade(key):
    status = capability()
    status[key] = ["pair-test"]
    class Api:
        def call(self, path, body): return status
    assert not upgrade.runtime_idle(Api(), [job()])
