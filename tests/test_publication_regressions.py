import json

import pytest

from agent_trace_kit import local_review_worker as worker
from agent_trace_kit import workspace as ws
from agent_trace_kit.checklist import run_checklist
from agent_trace_kit.runner import PairRunner


def test_local_baseline_stays_frozen_after_baseline_advances(tmp_path):
    source = tmp_path / "baseline"
    source.mkdir()
    ws.git(["init"], source)
    ws.git(["config", "user.name", "Synthetic Test"], source)
    ws.git(["config", "user.email", "test@example.invalid"], source)
    (source / "answer.txt").write_text("frozen", encoding="utf-8")
    ws.git(["add", "."], source)
    ws.git(["commit", "-m", "frozen baseline"], source)
    frozen = ws.head_sha(source)
    (source / "answer.txt").write_text("later baseline", encoding="utf-8")
    ws.git(["commit", "-am", "later baseline"], source)
    (source / "untracked.txt").write_text("later file", encoding="utf-8")
    (source / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (source / "ignored.txt").write_text("later ignored file", encoding="utf-8")
    nested = source / "later-nested-repo"
    nested.mkdir()
    ws.git(["init"], nested)
    (nested / "later.txt").write_text("later nested repository", encoding="utf-8")
    runner = object.__new__(PairRunner)
    job = {"baseline_repo": str(source), "baseline_sha": frozen, "source_mode": "local", "agent": "codex"}
    for name in ("a", "b", "retry-a"):
        side = tmp_path / name
        info = runner._prepare_workspace(job, side, name)
        assert info["head"] == frozen
        assert (side / "answer.txt").read_text(encoding="utf-8") == "frozen"
        assert not (side / "untracked.txt").exists()
        assert not (side / "ignored.txt").exists()
        assert not (side / "later-nested-repo").exists()
    assert (source / "untracked.txt").exists()
    assert (source / "ignored.txt").exists()
    assert (nested / "later.txt").exists()
    assert (source / "answer.txt").read_text(encoding="utf-8") == "later baseline"


@pytest.mark.parametrize("mode", ["local", "new_github"])
def test_local_and_new_baseline_mismatch_blocks_checklist(tmp_path, mode):
    job = {"source_mode": mode, "baseline_sha": "a" * 40,
           "sides": {name: {"workspace": str(tmp_path), "initial_sha": "b" * 40} for name in ("A", "B")}}
    checks = {row["id"]: row for row in run_checklist(job)["items"]}
    for name in ("A", "B"):
        assert not checks[f"{name}_initial_sha"]["ok"]
        assert checks[f"{name}_initial_sha"]["blocking"]


def test_linked_worktree_is_refused_without_touching_original(tmp_path):
    source, linked = tmp_path / "source", tmp_path / "linked"
    source.mkdir()
    ws.git(["init"], source)
    ws.git(["config", "user.name", "Synthetic Test"], source)
    ws.git(["config", "user.email", "test@example.invalid"], source)
    (source / "answer.txt").write_text("original", encoding="utf-8")
    ws.git(["add", "."], source)
    ws.git(["commit", "-m", "original"], source)
    frozen = ws.head_sha(source)
    ws.git(["worktree", "add", "-b", "linked-original", str(linked)], source)
    with pytest.raises(ws.GitError, match="独立 Git clone"):
        ws.prepare_side_workspace(linked, tmp_path / "side", "side-a", baseline_sha=frozen)
    assert ws.git(["branch", "--show-current"], linked)[0].strip() == "linked-original"
    assert ws.head_sha(linked) == frozen
    assert ws.git(["status", "--porcelain"], linked)[0] == ""


def test_local_gitlink_is_refused_before_creating_side(tmp_path):
    source = tmp_path / "baseline"
    source.mkdir()
    ws.git(["init"], source)
    ws.git(["config", "user.name", "Synthetic Test"], source)
    ws.git(["config", "user.email", "test@example.invalid"], source)
    ws.git(["commit", "--allow-empty", "-m", "root"], source)
    ws.git(["update-index", "--add", "--cacheinfo", f"160000,{ws.head_sha(source)},lib"], source)
    ws.git(["commit", "-m", "gitlink"], source)
    frozen = ws.head_sha(source)
    with pytest.raises(ws.GitError, match="暂不支持子模块"):
        ws.prepare_side_workspace(source, tmp_path / "side", "side-a", baseline_sha=frozen)
    assert not (tmp_path / "side").exists()
    assert ws.head_sha(source) == frozen


def test_worker_without_private_configuration_cannot_contact_ssh(monkeypatch):
    monkeypatch.setattr(worker, "JOBS", set())
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: pytest.fail("unexpected SSH"))
    with pytest.raises(RuntimeError, match="configuration"):
        worker.remote("print('unreachable')")


@pytest.mark.parametrize("key,value", [("ssh_host", "-oProxyCommand=bad"),
    ("remote_python", "/bin/python;touch-bad"), ("remote_batch", "/srv/../private"),
    ("job_ids", ["pair-1111111111"] * 20)])
def test_private_worker_config_rejects_unsafe_targets(configured_review_worker, key, value):
    config = json.loads(configured_review_worker.read_text(encoding="utf-8"))
    config[key] = value
    configured_review_worker.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError):
        worker.configure_worker(configured_review_worker)
