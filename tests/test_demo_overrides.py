from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_trace_kit import demo, recorder
from agent_trace_kit.desk_store import DeskStore


@pytest.fixture
def finished_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.sys, "platform", "linux")
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "演示外部配方"})
    for side in ("A", "B"):
        store.update_side(job["id"], side, {"status": "done", "head_sha": "frozen-head",
                                          "session_id": f"session-{side}", "finished_at": "2026-09-30"})
    return store, store.get_job(job["id"]), demo.DemoManager(store, recorder.Recorder(store))


def terminal_recipe():
    return {"kind": "terminal", "start": ["{python}", ".atk-review/probe.py"]}


def test_override_is_bound_hashed_and_written_only_to_copy(finished_pair, tmp_path):
    store, job, manager = finished_pair
    source = tmp_path / "frozen-workspace"
    source.mkdir()
    (source / "README.md").write_text("原始中文产物", encoding="utf-8")
    work = tmp_path / "disposable-copy"
    work.mkdir()
    recipe = terminal_recipe()
    files = {".atk-review/probe.py": "print('独立演示')\n"}
    info = manager.request(job["id"], "A", recipe=recipe,
                           expected_artifact_key=demo.artifact_key(job["sides"]["A"]), extra_files=files)
    assert info["status"] == "queued"
    assert len(info["recipe_override"]["sha256"]) == 64
    assert not (source / ".atk-review").exists()
    assert (source / "README.md").read_text(encoding="utf-8") == "原始中文产物"
    # The persisted envelope is detached from the caller's mutable data.
    recipe["start"][0] = "changed"
    files[".atk-review/probe.py"] = "changed"
    result, evidence = manager._recipe_for_run(store.get_job(job["id"]), "A", info, work)
    assert result["start"][0] == "{python}"
    assert (work / ".atk-review/probe.py").read_text(encoding="utf-8") == "print('独立演示')\n"
    assert evidence["source"] == "operator-authorized-external"
    assert evidence["artifact_key"] == demo.artifact_key(job["sides"]["A"])


def test_override_requires_current_artifact_and_no_partial_queue(finished_pair):
    store, job, manager = finished_pair
    with pytest.raises(ValueError, match="expected_artifact_key"):
        manager.request(job["id"], "A", recipe=terminal_recipe())
    with pytest.raises(ValueError, match="已经改变"):
        manager.request(job["id"], "A", recipe=terminal_recipe(), expected_artifact_key="stale")
    assert not store.get_job(job["id"])["sides"]["A"].get("demo")
    assert not (store.evidence_dir(job["id"]) / "demo-overrides").exists()


def test_override_refuses_changed_artifact_at_run(finished_pair, tmp_path):
    store, job, manager = finished_pair
    info = manager.request(job["id"], "A", recipe=terminal_recipe(),
                           expected_artifact_key=demo.artifact_key(job["sides"]["A"]))
    store.update_side(job["id"], "A", {"session_id": "new-attempt"})
    with pytest.raises(ValueError, match="已经改变"):
        manager._recipe_for_run(store.get_job(job["id"]), "A", info, tmp_path)


def test_override_refuses_tampered_evidence(finished_pair, tmp_path):
    store, job, manager = finished_pair
    info = manager.request(job["id"], "A", recipe=terminal_recipe(),
                           expected_artifact_key=demo.artifact_key(job["sides"]["A"]))
    path = store.evidence_dir(job["id"]) / info["recipe_override"]["path"]
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["recipe"]["start"] = ["changed"]
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256"):
        manager._recipe_for_run(job, "A", info, tmp_path)


@pytest.mark.parametrize("name", ["outside.py", "/.atk-review/probe.py", ".atk-review/../source.py",
                                  ".atk-review//probe.py", ".atk-review/./probe.py",
                                  ".atk-review\\probe.py", ".atk-review/C:probe.py"])
def test_extra_files_refuses_noncanonical_or_escaping_paths(finished_pair, name):
    _, job, manager = finished_pair
    with pytest.raises(ValueError, match="相对路径"):
        manager.request(job["id"], "A", recipe=terminal_recipe(),
                        expected_artifact_key=demo.artifact_key(job["sides"]["A"]),
                        extra_files={name: "print('test')"})


def test_extra_files_refuses_overwrite_without_partial_writes(tmp_path):
    (tmp_path / ".atk-review").mkdir()
    original = tmp_path / ".atk-review/existing.py"
    original.write_text("原来的文件", encoding="utf-8")
    with pytest.raises(ValueError, match="覆盖"):
        demo._write_extra_files(tmp_path, {".atk-review/new.py": "new", ".atk-review/existing.py": "changed"})
    assert original.read_text(encoding="utf-8") == "原来的文件"
    assert not (tmp_path / ".atk-review/new.py").exists()


def test_extra_files_refuses_symlink_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    work = tmp_path / "copy"
    work.mkdir()
    try:
        (work / ".atk-review").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is not permitted on this host")
    with pytest.raises(ValueError, match="逃逸|符号链接"):
        demo._write_extra_files(work, {".atk-review/probe.py": "bad"})
    assert not (outside / "probe.py").exists()


def test_extra_files_is_finite_utf8_and_requires_recipe(finished_pair):
    _, job, manager = finished_pair
    with pytest.raises(ValueError, match="一起提交"):
        manager.request(job["id"], "A", extra_files={".atk-review/probe.py": "test"})
    with pytest.raises(ValueError, match="64 KB"):
        demo._extra_files({".atk-review/probe.py": "中" * 22000})
    with pytest.raises(ValueError, match="UTF-8"):
        demo._extra_files({".atk-review/probe.py": "\ud800"})


def test_default_request_still_autodetects_original_product(finished_pair, tmp_path):
    _, job, manager = finished_pair
    (tmp_path / "index.html").write_text("原始页面", encoding="utf-8")
    info = manager.request(job["id"], "A")
    recipe, evidence = manager._recipe_for_run(job, "A", info, tmp_path)
    assert recipe["source"] == "static-html"
    assert evidence["source"] == "committed-artifact-or-autodetection"


def test_run_one_archive_failure_preserves_frozen_artifact_and_reports_failure(finished_pair, tmp_path, monkeypatch):
    store, job, manager = finished_pair
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    (source / "product.txt").write_text("冻结中文", encoding="utf-8")
    subprocess.run(["git", "add", "product.txt"], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "test artifact"], cwd=source, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True, encoding="utf-8").strip()
    store.update_side(job["id"], "A", {"workspace": str(source), "head_sha": sha})
    current = store.get_job(job["id"])
    recipe = {**terminal_recipe(), "setup": [["test-setup"]]}
    manager.request(job["id"], "A", recipe=recipe,
                    expected_artifact_key=demo.artifact_key(current["sides"]["A"]),
                    extra_files={".atk-review/probe.py": "print('演示真实副本')\n"})
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(demo, "find_free_port", lambda **_: 12345)
    def failing_setup(command, cwd, log, timeout, env):
        assert cwd != source
        assert (cwd / "product.txt").read_text(encoding="utf-8") == "冻结中文"
        assert "演示真实副本" in (cwd / ".atk-review/probe.py").read_text(encoding="utf-8")
        raise RuntimeError("observed setup failure")
    monkeypatch.setattr(manager, "_command", failing_setup)
    report = manager.run_one(job["id"], "A")
    assert report["status"] == "failed"
    assert "observed setup failure" in report["error"]
    assert report["human_reviewed"] is False
    assert report["recipe_evidence"]["artifact"]["head_sha"] == sha
    assert not (source / ".atk-review").exists()
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=source).strip() == b""
