from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_trace_kit import demo, recorder
from agent_trace_kit.demo_artifacts import byte_range, contained_file, build_bundle
from agent_trace_kit.desk_store import DeskStore


def test_recipe_static_and_vite(tmp_path):
    (tmp_path / "index.html").write_text("中文演示", encoding="utf-8")
    assert demo.recipe_for(tmp_path)["source"] == "static-html"
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "*"}}), encoding="utf-8")
    result = demo.recipe_for(tmp_path)
    assert "--strictPort" in result["start"]
    assert "{port}" in result["start"]
    assert not (tmp_path / "node_modules").exists()


@pytest.mark.parametrize("patch", [{"start": "npm run dev"}, {"path": "//foreign.example/"},
                                   {"steps": [{"action": "eval", "code": "x"}]}, {"kind": "unknown"}])
def test_recipe_refuses_ambiguous_inputs(tmp_path, patch):
    recipe = {"kind": "web", "start": ["python", "app.py"], **patch}
    (tmp_path / "atk-demo.json").write_text(json.dumps(recipe), encoding="utf-8")
    with pytest.raises(ValueError):
        demo.recipe_for(tmp_path)


def test_unknown_product_is_not_claimed_as_demonstrated(tmp_path):
    (tmp_path / "README.md").write_text("A CLI product", encoding="utf-8")
    with pytest.raises(ValueError, match="入口"):
        demo.recipe_for(tmp_path)


def test_linux_recording_serializes_display_and_uses_x11(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(recorder.shutil, "which", lambda _: "/bin/ffmpeg")
    commands = []
    class Proc:
        def poll(self): return None
    monkeypatch.setattr(recorder.subprocess, "Popen", lambda cmd, **kw: (commands.append(cmd) or Proc()))
    monkeypatch.setattr(recorder.threading.Thread, "start", lambda _: None)
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "demo"})
    rec = recorder.Recorder(store)
    rec.start(job["id"], "A", fps=10, max_seconds=600)
    assert "x11grab" in commands[0] and ":99" in commands[0]
    assert commands[0][commands[0].index("-t") + 1] == "89"
    assert commands[0][-1].endswith(".recording.mp4")
    with pytest.raises(RuntimeError, match="另一侧"):
        rec.start(job["id"], "B")
    for value in rec._recs.values():
        value["logf"].close()


def test_failed_recording_does_not_replace_prior_evidence(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "demo"})
    rec = recorder.Recorder(store)
    out = rec._video_path(job["id"], "A")
    out.write_bytes(b"previous-good-video")
    pending = out.with_name("failed.recording.mp4")
    pending.write_bytes(b"incomplete")
    store.update_side(job["id"], "A", {"video_local": str(out)})
    rec._recs[rec._key(job["id"], "A")] = {"pending": pending}
    monkeypatch.setattr(recorder, "video_duration_seconds", lambda p: 3 if p == out else None)
    info = rec._save(job["id"], "A", out, 1)
    assert info["error"]
    assert out.read_bytes() == b"previous-good-video"
    assert store.get_job(job["id"])["sides"]["A"]["video_local"] == str(out)


@pytest.mark.parametrize("header,expected", [("bytes=0-9", (0, 9)), ("bytes=95-", (95, 99)),
                                             ("bytes=-10", (90, 99)), ("bytes=0-500", (0, 99))])
def test_video_ranges(header, expected):
    assert byte_range(header, 100) == expected


@pytest.mark.parametrize("header", ["bytes=100-", "bytes=3-1", "bytes=-0", "bytes=", "bytes=0-1,4-5"])
def test_video_ranges_reject_invalid(header):
    with pytest.raises(ValueError):
        byte_range(header, 100)


def test_download_rejects_file_outside_job(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    secret = tmp_path / "secrets.env"
    secret.write_text("private", encoding="utf-8")
    with pytest.raises(ValueError):
        contained_file(str(secret), evidence)


def test_bundle_has_evidence_hashes_and_no_history(tmp_path):
    import zipfile
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "中文任务"})
    evidence = store.evidence_dir(job["id"])
    (evidence / "a-video.mp4").write_bytes(b"video")
    (evidence / "a-video.recording.mp4").write_bytes(b"partial")
    out = tmp_path / "results.zip"
    build_bundle(store, job, out)
    with zipfile.ZipFile(out) as z:
        assert "中文任务" in z.read("job.json").decode("utf-8")
        assert "evidence/a-video.recording.mp4" not in z.namelist()
        manifest = json.loads(z.read("manifest.json"))
        assert len(manifest) == 1 and len(manifest[0]["sha256"]) == 64


def test_archived_or_locked_job_cannot_auto_demo(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.sys, "platform", "linux")
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "demo"})
    store.update_side(job["id"], "A", {"status": "done"})
    store.set_archived(job["id"], True)
    manager = demo.DemoManager(store, recorder.Recorder(store))
    with pytest.raises(RuntimeError, match="归档"):
        manager.request(job["id"], "A")


def test_artifact_key_changes_with_new_attempt():
    first = {"head_sha": "a", "session_id": "b", "finished_at": "c"}
    assert demo.artifact_key(first) != demo.artifact_key({**first, "finished_at": "d"})


def test_interrupted_demo_is_failed_without_rewriting_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.sys, "platform", "linux")
    monkeypatch.setattr(demo.threading.Thread, "start", lambda _: None)
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "demo"})
    store.update_side(job["id"], "A", {"demo": {"status": "running", "artifact_key": "original"}, "video_local": "prior.mp4"})
    manager = demo.DemoManager(store, recorder.Recorder(store))
    manager.start()
    side = store.get_job(job["id"])["sides"]["A"]
    assert side["demo"]["status"] == "failed"
    assert side["demo"]["artifact_key"] == "original"
    assert side["video_local"] == "prior.mp4"


def test_next_pair_waits_for_demo_but_failed_demo_does_not_stall_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.sys, "platform", "linux")
    store = DeskStore(tmp_path / "desk")
    store.save_settings({"linux_auto_demo": True})
    job = store.create_job({"prompt": "demo"})
    for name in ("A", "B"):
        store.update_side(job["id"], name, {"status": "done"})
    manager = demo.DemoManager(store, recorder.Recorder(store))
    assert not manager.pair_may_start()
    for name in ("A", "B"):
        value = store.get_job(job["id"])["sides"][name]
        store.update_side(job["id"], name, {"demo": {"status": "failed", "artifact_key": demo.artifact_key(value)}})
    assert manager.pair_may_start()


def test_json_recipe_validation_is_a_finite_check():
    from agent_trace_kit.runner import is_long_running_start
    assert not is_long_running_start("python -m json.tool atk-demo.json")
    assert is_long_running_start("python -m http.server 8765")
