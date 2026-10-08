"""Pinned GitHub sources: validation, actual Git checkouts, retries and Desk API."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_trace_kit import baseline, checklist, engines, ghutil, workspace as ws
from agent_trace_kit.export_tsv import job_row
from agent_trace_kit.desk import DeskServer
from agent_trace_kit.desk_store import DeskStore
from agent_trace_kit.runner import PairRunner


SHA = "a1b2c3d4" * 5


@pytest.mark.parametrize(
    "repo",
    ["org/repo", "https://github.com/org/repo", "https://github.com/org/repo.git"],
)
def test_repository_and_commit_normalization(repo):
    source = baseline.normalize_source(
        {"source_repo": repo, "source_commit": SHA.upper()}, "Feature迭代"
    )
    assert source["source_mode"] == "github_commit"
    assert source["source_repo"] == "https://github.com/org/repo.git"
    assert source["source_commit"] == SHA
    assert source["branch_suffix"] == "feature"
    assert source["github_created"] is False
    assert source["baseline_repo"] == ""


@pytest.mark.parametrize("field", ["source_repo", "source_commit", "commit_url"])
def test_commit_permalink_can_supply_both_fields(field):
    source = baseline.normalize_source(
        {field: f"https://github.com/org/repo/commit/{SHA}"}, "Bug修复"
    )
    assert source["source_commit"] == SHA
    assert source["github_repo"] == "org/repo"
    assert source["branch_suffix"] == "fix"


def test_ssh_and_custom_feature_suffix(tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job(
        {
            "prompt": "添加中文功能",
            "source_repo": "git@github.com:org/repo.git",
            "source_commit": SHA[:9],
            "branch_suffix": "feature-login",
            "agent": "codex",
        }
    )
    assert job["source_repo"] == "git@github.com:org/repo.git"
    assert job["source_commit"] == SHA[:9]
    assert job["agent"] == "codex"
    assert job["sides"]["A"]["branch"] == f"{job['id']}-feature-login-a"
    assert job["sides"]["B"]["branch"] == f"{job['id']}-feature-login-b"
    assert store.get_job(job["id"])["prompt"] == "添加中文功能"


@pytest.mark.parametrize(
    "fields",
    [
        {"source_repo": "org/repo", "source_commit": "main"},
        {"source_repo": "org/repo"},
        {"source_repo": "https://other.example/org/repo", "source_commit": SHA},
        {"source_repo": "https://token@github.com/org/repo", "source_commit": SHA},
        {"source_repo": "https://github.com/org/repo/tree/main", "source_commit": SHA},
        {
            "source_repo": "org/repo",
            "source_commit": f"https://github.com/other/repo/commit/{SHA}",
        },
        {
            "source_repo": f"https://github.com/org/repo/commit/{SHA}",
            "source_commit": "b" * 40,
        },
        {"source_repo": "org/repo", "source_commit": SHA, "branch_suffix": "../main"},
        {"source_repo": "org/repo", "source_commit": SHA, "branch_suffix": "--delete"},
        {"source_mode": "unknown"},
    ],
)
def test_invalid_source_is_rejected_before_persisting(tmp_path, fields):
    store = DeskStore(tmp_path / "desk")
    with pytest.raises(ValueError):
        store.create_job({"prompt": "p", **fields})
    assert store.list_jobs() == []


def test_legacy_new_and_local_jobs_keep_branch_names(tmp_path):
    store = DeskStore(tmp_path / "desk")
    for fields, mode in [
        ({"github_repo": "new-repo"}, "new_github"),
        ({"baseline_repo": str(tmp_path / "local")}, "local"),
    ]:
        job = store.create_job({"prompt": "p", **fields})
        assert job["source_mode"] == mode
        assert job["sides"]["A"]["branch"] == f"{job['id']}-a"


@pytest.mark.parametrize(
    "fields",
    [
        {
            "source_mode": "new_github",
            "github_repo": "new",
            "baseline_repo": "D:/existing",
        },
        {"source_mode": "new_github"},
        {"source_mode": "local"},
        {"source_mode": "local", "baseline_repo": "D:/existing", "github_repo": "new"},
        {"source_mode": "new_github", "github_repo": "new", "source_commit": SHA},
    ],
)
def test_explicit_modes_reject_conflicting_api_fields(tmp_path, fields):
    store = DeskStore(tmp_path / "desk")
    with pytest.raises(ValueError):
        store.create_job({"prompt": "p", **fields})
    assert not store.list_jobs()


@pytest.fixture
def history(tmp_path):
    """A source with an older requested commit and a newer main, no network."""
    remote, source = tmp_path / "remote.git", tmp_path / "source"
    ws.git(["init", "--bare", "-b", "main", str(remote)], tmp_path)
    source.mkdir()
    ws.git(["init", "-b", "main"], source)
    ws.git(["config", "user.name", "Pair Test"], source)
    ws.git(["config", "user.email", "test@example.invalid"], source)
    (source / "app.txt").write_text("初始功能\n", encoding="utf-8")
    (source / ".gitignore").write_text(".env\n", encoding="utf-8")
    ws.git(["add", "-A"], source)
    ws.git(["commit", "-m", "original"], source)
    original = ws.head_sha(source)
    (source / "app.txt").write_text("新的主分支功能\n", encoding="utf-8")
    (source / "later.txt").write_text("later\n", encoding="utf-8")
    ws.git(["add", "-A"], source)
    ws.git(["commit", "-m", "new main"], source)
    latest = ws.head_sha(source)
    ws.git(["remote", "add", "origin", str(remote)], source)
    ws.git(["push", "-u", "origin", "main"], source)
    return {
        "remote": str(remote),
        "source": source,
        "original": original,
        "latest": latest,
    }


def local_import(store, history, *, agent="codex"):
    job = store.create_job(
        {
            "prompt": "升级已有功能",
            "source_repo": "org/repo",
            "source_commit": history["original"][:12],
            "task_type": "Feature迭代",
            "agent": agent,
        }
    )
    # Parsing is tested separately; substitute a local bare origin to exercise real Git
    # without creating/pushing any GitHub repository or invoking a paid model.
    store.update_job(job["id"], {"source_repo": history["remote"]})
    return job["id"]


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_existing_commit_prepare_retry_and_recovery_are_frozen(
    history, tmp_path, monkeypatch, agent
):
    store = DeskStore(tmp_path / "desk")
    jid = local_import(store, history, agent=agent)
    monkeypatch.setattr(engines, "cli_version", lambda *_: "test")
    monkeypatch.setattr(
        ghutil,
        "ensure_remote_repo",
        lambda *_a, **_k: pytest.fail("must not create GitHub repo"),
    )
    runner = PairRunner(store)
    report = runner.prepare(jid)
    assert report["provisioned"] is None
    job = store.get_job(jid)
    original = history["original"]
    assert job["baseline_sha"] == original != history["latest"]
    assert job["github_created"] is False
    sides = {s: Path(job["sides"][s]["workspace"]) for s in ("A", "B")}
    for name, path in sides.items():
        assert ws.head_sha(path) == original
        assert job["sides"][name]["initial_sha"] == original
        assert ws.remote_url(path) == history["remote"]
        assert ws.current_branch(path) == f"{jid}-feature-{name.lower()}"
        assert (path / "app.txt").read_text(encoding="utf-8") == "初始功能\n"
        assert not (path / "later.txt").exists()
        assert not (path / "README.md").exists()
        assert (path / ".claude/settings.local.json").exists() == (agent == "claude")
    # Advance/mutate the cache, including ignored and untracked files. A retry must
    # still materialize the original tracked tree and leave B untouched.
    cache = Path(job["baseline_repo"])
    ws.git(["checkout", "--detach", history["latest"]], cache)
    (cache / "leak.txt").write_text("untracked", encoding="utf-8")
    (cache / ".env").write_text("ignored", encoding="utf-8")
    (sides["A"] / "app.txt").write_text("A dirty", encoding="utf-8")
    (sides["B"] / "keep.txt").write_text("B untouched", encoding="utf-8")
    # A fresh runner simulates process restart; source_commit remains abbreviated.
    recovered = PairRunner(DeskStore(tmp_path / "desk"))
    recovered._fresh_workspace(jid, "A")
    assert ws.head_sha(sides["A"]) == original
    assert (sides["A"] / "app.txt").read_text(encoding="utf-8") == "初始功能\n"
    assert not any(
        (sides["A"] / name).exists() for name in ("later.txt", "leak.txt", ".env")
    )
    assert (sides["B"] / "keep.txt").read_text(encoding="utf-8") == "B untouched"
    assert recovered.prepare(jid)["baseline"]["sha"] == original
    assert (
        ws.git(["rev-parse", "refs/heads/main"], history["remote"])[0]
        == history["latest"]
    )
    assert (
        ws.git(
            ["for-each-ref", "--format=%(refname)", "refs/heads"], history["remote"]
        )[0]
        == "refs/heads/main"
    )
    # Use the normal commit/push path for both products. Only dedicated branches
    # move, and both resulting commits still descend from the selected baseline.
    for name, path in sides.items():
        (path / "feature.txt").write_text(f"{name} 新功能\n", encoding="utf-8")
        branch = job["sides"][name]["branch"]
        product = ws.finalize_side(path, branch, f"Pair {name}")
        assert ws.is_ancestor(path, original, product["sha"])
        assert (
            ws.git(["rev-parse", f"refs/heads/{branch}"], history["remote"])[0]
            == product["sha"]
        )
    assert (
        ws.git(["rev-parse", "refs/heads/main"], history["remote"])[0]
        == history["latest"]
    )


def test_missing_commit_fails_without_head_fallback_or_partial_checkout(
    history, tmp_path
):
    target = tmp_path / "pair" / "baseline"
    with pytest.raises(ws.GitError):
        baseline.prepare_commit_baseline(history["remote"], "f" * 40, target)
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_cache_origin_mismatch_is_rejected(history, tmp_path):
    target = tmp_path / "baseline"
    baseline.prepare_commit_baseline(history["remote"], history["original"], target)
    with pytest.raises(ws.GitError, match="origin"):
        baseline.prepare_commit_baseline(
            "https://github.com/unrelated/repo.git", history["original"], target
        )


def test_hex_named_ref_cannot_override_requested_commit(history, tmp_path):
    source = history["source"]
    ws.git(["branch", "abcdef123", history["latest"]], source)
    with pytest.raises(ws.GitError, match="SHA 不一致"):
        baseline._resolve_commit(source, "abcdef123")


def test_job_create_prepares_import_and_queues_without_provisioning(
    history, tmp_path, monkeypatch
):
    server = DeskServer(DeskStore(tmp_path / "desk"))
    real_prepare = baseline.prepare_commit_baseline
    monkeypatch.setattr(
        baseline,
        "prepare_commit_baseline",
        lambda _remote, commit, dest: real_prepare(history["remote"], commit, dest),
    )
    monkeypatch.setattr(engines, "cli_version", lambda *_: "test")
    monkeypatch.setattr(
        ghutil,
        "ensure_remote_repo",
        lambda *_a, **_k: pytest.fail("must not provision"),
    )
    result = server.job_create(
        {
            "prompt": "p",
            "source_repo": "org/repo",
            "source_commit": history["original"],
            "agent": "codex",
        }
    )
    assert result["prepared"] == [result["job"]]
    assert result["baseline"]["sha"] == history["original"]
    assert result["provisioned"] is None
    assert result["job"] in server.runner._queued


def test_batch_supports_commit_link_and_separate_sha_with_legacy_rows(
    tmp_path, monkeypatch
):
    server = DeskServer(DeskStore(tmp_path / "desk"))
    jobs = []

    def create(data, **_):
        job = server.store.create_job(data)
        jobs.append(job)
        return {"job": job["id"], "prepared": [job["id"]]}

    monkeypatch.setattr(server, "job_create", create)
    rows = [
        f"first\tFeature迭代\t困难\tPython\thttps://github.com/org/repo/commit/{SHA}\tcodex",
        f"second\tBug修复\t困难\tPython\torg/repo\tclaude\t{SHA}\tfix-login",
        "third\t\t\t\tnew-project\tcodex",
        "fourth\t\t\t\tD:/existing/project\tclaude",
        "invalid\t\t\t\thttps://github.com/org/repo\tcodex\tmain",
    ]
    result = server.job_batch({"lines": "\n".join(rows)})
    assert len(result["created"]) == 4
    assert len(result["errors"]) == 1
    assert [j["source_mode"] for j in jobs] == [
        "github_commit",
        "github_commit",
        "new_github",
        "local",
    ]
    assert jobs[0]["source_commit"] == jobs[1]["source_commit"] == SHA
    assert jobs[1]["branch_suffix"] == "fix-login"


def test_checklist_requires_exact_import_start(tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job(
        {"prompt": "p", "source_repo": "org/repo", "source_commit": SHA[:12]}
    )
    job["baseline_sha"] = SHA
    job["baseline_url"] = f"https://github.com/org/repo/commit/{SHA}"
    job["sides"]["A"]["initial_sha"] = SHA
    job["sides"]["B"]["initial_sha"] = "b" * 40
    checks = {c["id"]: c for c in checklist.run_checklist(job)["items"]}
    assert checks["baseline_requested_commit"]["ok"]
    assert checks["A_initial_sha"]["ok"]
    assert not checks["B_initial_sha"]["ok"]
    job["baseline_sha"] = "c" * 40
    checks = {c["id"]: c for c in checklist.run_checklist(job)["items"]}
    assert not checks["baseline_requested_commit"]["ok"]
    row = job_row(job)
    assert len(row) == 26
    assert row[8] == f"https://github.com/org/repo/commit/{SHA}"


def test_import_cannot_delete_existing_repository(tmp_path, monkeypatch):
    server = DeskServer(DeskStore(tmp_path / "desk"))
    job = server.store.create_job(
        {"prompt": "p", "source_repo": "org/repo", "source_commit": SHA}
    )
    monkeypatch.setattr(server, "_gh_login", lambda: "org")
    monkeypatch.setattr(
        ghutil.CliGh,
        "repo_delete",
        lambda *_: pytest.fail("must not delete imported repo"),
    )
    with pytest.raises(RuntimeError, match="不允许删除整个仓库"):
        server._delete_remote(job, "repo")


@pytest.mark.parametrize("entry", ["manual", "recovery"])
def test_partial_preparation_resumes_before_launch(
    history, tmp_path, monkeypatch, entry
):
    store = DeskStore(tmp_path / "desk")
    jid = local_import(store, history)
    server = DeskServer(store)
    monkeypatch.setattr(engines, "cli_version", lambda *_: "test")
    real_side = baseline.prepare_commit_side
    attempts = []

    def fail_b_once(source, dest, branch, sha, **kwargs):
        attempts.append(branch)
        if branch.endswith("-b") and attempts.count(branch) == 1:
            raise ws.GitError("test: checkout interrupted")
        return real_side(source, dest, branch, sha, **kwargs)

    monkeypatch.setattr(baseline, "prepare_commit_side", fail_b_once)
    with pytest.raises(ws.GitError, match="interrupted"):
        server.runner.prepare(jid)
    partial = store.get_job(jid)
    assert partial["baseline_sha"] == history["original"]
    assert partial["baseline_prepared"] is False
    assert partial["status"] == "failed"
    assert partial["sides"]["A"]["workspace"]
    assert not partial["sides"]["B"]["workspace"]
    # No model is invoked: the launch boundary asserts both real checkouts
    # already exist and records completion for this scheduling test.
    launched = []
    runner = PairRunner(DeskStore(tmp_path / "desk"))

    def fake_launch(job_id, side_name):
        job = store.get_job(job_id)
        assert job["baseline_prepared"]
        assert all(
            ws.head_sha(s["workspace"]) == history["original"]
            for s in job["sides"].values()
        )
        launched.append(side_name)
        store.update_side(job_id, side_name, {"status": "done"})

    monkeypatch.setattr(runner, "_run_side_safe", fake_launch)
    server.runner = runner
    if entry == "manual":
        server.job_action({"job": jid, "action": "enqueue"})
    else:
        runner._resume_incomplete_jobs()
    assert jid in runner._queued
    runner._run_job(jid)
    assert sorted(launched) == ["A", "B"]
    assert attempts.count(partial["sides"]["A"]["branch"]) == 1
    assert store.get_job(jid)["baseline_sha"] == history["original"]
