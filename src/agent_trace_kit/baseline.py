"""Explicit Pair baseline sources and immutable Git commit checkouts."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import workspace as ws

SOURCE_MODES = ("new_github", "github_commit", "local")
_COMMIT = re.compile(r"[0-9a-fA-F]{7,40}")
_SUFFIX = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}")
TASK_SUFFIXES = {
    "Feature迭代": "feature",
    "Bug修复": "fix",
    "代码重构": "refactor",
    "代码理解": "analysis",
    "工程化": "engineering",
    "代码测试": "test",
}


def github_source(value: str) -> dict[str, str]:
    """Accept owner/repo, HTTPS/SSH repo URLs, or GitHub commit permalinks."""
    value = str(value or "").strip()
    ssh = value.startswith("git@github.com:")
    if ssh:
        parts = value.removeprefix("git@github.com:").split("/")
    elif value.startswith("https://"):
        parsed = urlsplit(value)
        if (
            parsed.hostname != "github.com"
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            raise ValueError("请输入不含凭据的 github.com 仓库或 commit 链接")
        parts = parsed.path.strip("/").split("/")
    else:
        parts = value.split("/")
    if len(parts) not in (2, 4) or (len(parts) == 4 and parts[2] != "commit"):
        raise ValueError("仓库格式应为 owner/repo、GitHub 仓库 URL 或 /commit/SHA 链接")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", owner)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo)
        or repo in (".", "..")
    ):
        raise ValueError("GitHub 仓库名称无效")
    commit = parts[3] if len(parts) == 4 else ""
    if commit and not _COMMIT.fullmatch(commit):
        raise ValueError("commit 必须是 7–40 位十六进制 SHA，不能填写分支名")
    name = f"{owner}/{repo}"
    url = f"https://github.com/{name}"
    return {
        "repo": name,
        "url": url,
        "remote": f"git@github.com:{name}.git" if ssh else url + ".git",
        "commit": commit.lower(),
    }


def normalize_source(data: dict, task_type: str) -> dict:
    mode = data.get("source_mode") or (
        "github_commit"
        if any(data.get(k) for k in ("source_repo", "source_commit", "commit_url"))
        else "local" if data.get("baseline_repo") else "new_github"
    )
    if mode not in SOURCE_MODES:
        raise ValueError("基线来源必须是自动建仓、已有 GitHub commit 或本地仓库")
    if data.get("source_mode") and mode in ("new_github", "local"):
        if any(data.get(k) for k in ("source_repo", "source_commit", "commit_url")):
            raise ValueError("所选基线来源与 GitHub commit 字段冲突")
        required, incompatible = (
            ("github_repo", "baseline_repo")
            if mode == "new_github"
            else ("baseline_repo", "github_repo")
        )
        if not str(data.get(required) or "").strip():
            raise ValueError(
                "请填写新仓库名" if mode == "new_github" else "请填写已有本地仓库路径"
            )
        if data.get(incompatible):
            raise ValueError("所选基线来源与仓库字段冲突，请只填写当前来源对应的仓库")
    suffix = str(data.get("branch_suffix") or "").strip()
    if not suffix and mode == "github_commit":
        suffix = TASK_SUFFIXES.get(task_type, "upgrade")
    if suffix and not _SUFFIX.fullmatch(suffix):
        raise ValueError("分支后缀限 1–48 位英文字母、数字、-、_，且须以字母或数字开头")
    result: dict = {"source_mode": mode, "branch_suffix": suffix}
    if mode != "github_commit":
        return result
    repo_input = str(data.get("source_repo") or "").strip()
    commit_input = str(
        data.get("commit_url") or data.get("source_commit") or ""
    ).strip()
    repo = github_source(repo_input) if repo_input else None
    if commit_input.startswith("https://"):
        link = github_source(commit_input)
        if not link["commit"]:
            raise ValueError("请填写带 /commit/SHA 的链接，或另填 commit SHA")
        if repo and repo["repo"].lower() != link["repo"].lower():
            raise ValueError("commit 链接与填写的仓库不一致")
        repo = repo or link
        commit_input = link["commit"]
    if repo is None:
        raise ValueError("请填写已有 GitHub 仓库或完整 commit 链接")
    if repo["commit"] and commit_input and repo["commit"] != commit_input.lower():
        raise ValueError("仓库链接中的 commit 与填写的 SHA 不一致")
    commit = commit_input or repo["commit"]
    if not _COMMIT.fullmatch(commit):
        raise ValueError("请指定 7–40 位 commit SHA；已有仓库模式不能使用浮动分支起点")
    result.update(
        source_repo=repo["remote"],
        source_commit=commit.lower(),
        source_url=repo["url"],
        github_repo=repo["repo"],
        github_url=repo["url"],
        github_created=False,
        baseline_repo="",
    )
    return result


def _resolve_commit(repo: Path, commit: str) -> str:
    if not _COMMIT.fullmatch(commit):
        raise ValueError("无效的 commit SHA")
    sha, _, code = ws.git(
        ["rev-parse", "--verify", f"{commit}^{{commit}}"], repo, check=False
    )
    if code or not ws.is_sha40(sha):
        # A full SHA may refer to a PR commit not present in normal branch refs.
        # GitHub must provide it from this origin; never fall back to HEAD.
        if len(commit) == 40:
            ws.git(["fetch", "origin", commit], repo, timeout=300)
            sha, _, code = ws.git(
                ["rev-parse", "--verify", f"{commit}^{{commit}}"], repo, check=False
            )
        if code or not ws.is_sha40(sha):
            raise ws.GitError(
                "在指定仓库中找不到该 commit（短 SHA 也可能不唯一）；请核对完整 SHA"
            )
    if not sha.lower().startswith(commit.lower()):
        raise ws.GitError("解析出的提交与指定 SHA 不一致，请使用完整 commit SHA")
    return sha


def prepare_commit_baseline(remote: str, commit: str, dest: Path) -> dict[str, str]:
    """Create/reuse a task-owned cache; fetch only, never push or rewrite origin."""
    dest = dest.resolve()
    if dest.exists():
        if ws.remote_url(dest) != remote:
            raise ws.GitError(f"基线缓存的 origin 与指定仓库不一致：{dest}")
        sha = _resolve_commit(dest, commit)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix=".baseline-", dir=dest.parent)).resolve()
        try:
            clone = temp / "repo"
            ws.git(
                ["clone", "--no-hardlinks", "--no-checkout", "--", remote, str(clone)],
                temp,
                timeout=300,
            )
            sha = _resolve_commit(clone, commit)
            ws.git(["checkout", "--detach", sha], clone)
            clone.rename(dest)
        finally:
            # Only this helper's fresh temporary directory is removed.
            if temp.parent == dest.parent and temp.exists():
                ws.robust_rmtree(temp)
    return {
        "path": str(dest),
        "sha": sha,
        "branch": "",
        "remote": remote,
        "url": ws.commit_permalink(dest, sha),
    }


def prepare_commit_side(
    baseline: str, dest: str | Path, branch: str, sha: str, *, agent: str = "claude"
) -> dict[str, str]:
    """Materialize tracked contents at the frozen SHA, excluding cache dirt."""
    source, target = Path(baseline).resolve(), Path(dest).resolve()
    if not ws.is_sha40(sha):
        raise ws.GitError("指定 commit 模式缺少冻结的完整 SHA")
    if target.exists():
        raise ws.GitError(f"目标工作区已存在：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".side-", dir=target.parent)).resolve()
    try:
        clone = temp / "repo"
        ws.git(
            ["clone", "--no-hardlinks", "--no-checkout", "--", str(source), str(clone)],
            temp,
            timeout=300,
        )
        ws.git(["remote", "set-url", "origin", ws.remote_url(source)], clone)
        ws.git(["checkout", "-b", branch, sha], clone)
        if (clone / ".gitmodules").exists():
            ws.git(["submodule", "update", "--init", "--recursive"], clone, timeout=300)
        ws.ensure_local_git_identity(clone)
        if agent == "claude":
            ws.write_context_settings(clone)
            ws._exclude_local_settings(clone)
        clone.rename(target)
    finally:
        if temp.parent == target.parent and temp.exists():
            ws.robust_rmtree(temp)
    return {"workspace": str(target), "branch": branch, "head": ws.head_sha(target)}
