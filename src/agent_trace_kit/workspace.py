"""Workspace preparation and git automation for A/B pair runs.

- copy one prepared baseline repository into two isolated workspaces
- create distinct branches, commit and push automatically
- verify 40-char SHAs, remote reachability and baseline ancestry
- locate the Claude Code session jsonl that ran inside a given workspace
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

SHA40 = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_COPY_EXCLUDES = {
    ".git",  # re-initialised per side so workspaces share no object store state
}


class GitError(RuntimeError):
    pass


def git(args: list[str], cwd: str | Path, *, timeout: int = 120, check: bool = True) -> tuple[str, str, int]:
    try:
        p = subprocess.run(
            ["git", *args], cwd=str(cwd), text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args)} @ {cwd}: {exc}") from exc
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({p.returncode}): {p.stdout}{p.stderr}".strip())
    return p.stdout.strip(), p.stderr.strip(), p.returncode


def is_sha40(value: str) -> bool:
    return bool(SHA40.match(value or ""))


def head_sha(workspace: str | Path) -> str:
    out, _, code = git(["rev-parse", "HEAD"], workspace, check=False)
    return out if code == 0 and is_sha40(out) else ""


def current_branch(workspace: str | Path) -> str:
    out, _, code = git(["rev-parse", "--abbrev-ref", "HEAD"], workspace, check=False)
    return out if code == 0 else ""


def remote_url(workspace: str | Path) -> str:
    out, _, code = git(["config", "--get", "remote.origin.url"], workspace, check=False)
    return out if code == 0 else ""


def commit_permalink(workspace: str | Path, sha: str) -> str:
    url = remote_url(workspace)
    if not url or not is_sha40(sha):
        return ""
    path = re.sub(r"^https?://", "https://", url)
    if path.endswith(".git"):
        path = path[:-4]
    if "@" in path and "://" not in path:  # git@github.com:org/repo
        path = "https://" + path.replace(":", "/").removeprefix("git@")
    return f"{path}/commit/{sha}"


def is_ancestor(workspace: str | Path, ancestor_sha: str, descendant_sha: str) -> bool:
    _, _, code = git(
        ["merge-base", "--is-ancestor", ancestor_sha, descendant_sha], workspace, check=False,
    )
    return code == 0


def sha_pushed(workspace: str | Path, sha: str, branch: str) -> bool:
    """True when the exact SHA is reachable on origin/<branch>."""
    git(["fetch", "origin", "--quiet"], workspace, timeout=60, check=False)
    out, _, code = git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], workspace, check=False)
    if code != 0 or not is_sha40(out):
        return False
    return out == sha or is_ancestor(workspace, sha, out)


def ensure_remote_contains(workspace: str | Path, sha: str, branch: str) -> None:
    """Push branch until the remote contains the exact SHA (fast-forward only)."""
    if sha_pushed(workspace, sha, branch):
        return
    ref = f"HEAD:refs/heads/{branch}"
    if _remote_branch_exists(workspace, branch):
        git(["push", "origin", ref], workspace, timeout=300)
    else:
        git(["push", "-u", "origin", ref], workspace, timeout=300)
    if not sha_pushed(workspace, sha, branch):
        raise GitError(f"push 后远端仍不包含 {sha[:12]}（{branch}）")


def _remote_branch_exists(workspace: str | Path, branch: str) -> bool:
    _, _, code = git(["ls-remote", "--exit-code", "--heads", "origin", branch], workspace, timeout=60, check=False)
    return code == 0


def has_uncommitted(workspace: str | Path) -> bool:
    """True when TRACKED content has staged/unstaged changes.

    Untracked files (node_modules, venv, build caches) do not block snapshot:
    the baseline commit only needs tracked source; untracked dependency trees
    are intentionally carried into the copied side workspaces.
    """
    out, _, _ = git(["status", "--porcelain", "--untracked-files=no"], workspace, check=False)
    return bool(out.strip())


def commit_all(workspace: str | Path, message: str) -> str:
    """Stage everything (including new files) and commit; returns HEAD sha."""
    git(["add", "-A"], workspace)
    p = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=str(workspace),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if p.returncode == 0:
        return head_sha(workspace)  # nothing staged: model made no changes
    git(["commit", "-m", message], workspace)
    return head_sha(workspace)


def snapshot_baseline(workspace: str | Path) -> dict[str, str]:
    """Commit any pending baseline work and push it so both sides share a reachable start."""
    sha = head_sha(workspace)
    if not sha:
        raise GitError("基线工作区还没有任何提交")
    branch = current_branch(workspace)
    ensure_remote_contains(workspace, sha, branch)
    return {"sha": sha, "branch": branch, "url": commit_permalink(workspace, sha), "remote": remote_url(workspace)}


def prepare_side_workspace(
    baseline_repo: str | Path,
    dest: str | Path,
    branch: str,
    copy_excludes: str = "",
) -> dict[str, str]:
    """Copy the prepared repo directory (including .git history) to an isolated side workspace."""
    src = Path(baseline_repo).resolve()
    dst = Path(dest).resolve()
    if not (src / ".git").exists():
        raise GitError(f"基线目录不是 git 仓库：{src}")
    if dst.exists() and any(dst.iterdir()):
        raise GitError(f"目标目录已存在且非空：{dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    excludes = set(DEFAULT_COPY_EXCLUDES)
    excludes.update(x.strip() for x in (copy_excludes or "").replace(";", "\n").splitlines() if x.strip())
    # .git must be copied so baseline history is present; remove it from excludes
    excludes.discard(".git")

    def ignore(dirpath: str, names: list[str]) -> list[str]:
        return [n for n in names if n in excludes]

    shutil.copytree(src, dst, ignore=ignore)
    git(["checkout", "-B", branch], dst)
    return {"workspace": str(dst), "branch": branch, "head": head_sha(dst)}


def finalize_side(workspace: str | Path, branch: str, message: str) -> dict[str, str]:
    """Auto commit all model changes and push; return 40-char sha + permalink."""
    sha = commit_all(workspace, message)
    if not is_sha40(sha):
        raise GitError("产物提交后无法读取 40 位 SHA")
    ensure_remote_contains(workspace, sha, branch)
    return {"sha": sha, "branch": branch, "url": commit_permalink(workspace, sha)}


# ---------- Claude Code session discovery ----------

def _encode_cwd(path: str | Path) -> str:
    """Replicate Claude Code projects directory encoding.

    D:\\myprojects\\x  ->  D--myprojects-x ; C:\\Users\\a -> C--Users-a
    """
    resolved = str(Path(path).resolve())
    return re.sub(r"[^A-Za-z0-9]", "-", resolved)


def find_session_jsonl(workspace: str | Path) -> list[dict[str, str]]:
    """Return sessions that ran inside workspace, newest first.

    Every jsonl under ~/.claude/projects is matched on the ``cwd`` record
    field (authoritative), with the encoded directory name as a fast filter.
    """
    import json
    target = str(Path(workspace).resolve()).lower()
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()
    projects = home / "projects"
    if not projects.exists():
        return []
    encoded = _encode_cwd(workspace).lower()
    out: list[dict[str, str]] = []
    for d in projects.iterdir():
        if not d.is_dir() or encoded not in d.name.lower():
            continue
        for p in d.glob("*.jsonl"):
            session_id = cwd = None
            try:
                with p.open("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        session_id = session_id or rec.get("sessionId") or rec.get("session_id")
                        cwd = cwd or rec.get("cwd")
                        if session_id and cwd:
                            break
            except OSError:
                continue
            if not cwd or str(Path(cwd).resolve()).lower() != target:
                continue
            st = p.stat()
            out.append({
                "path": str(p),
                "session_id": session_id or p.stem,
                "mtime": str(st.st_mtime),
                "size": str(st.st_size),
            })
    out.sort(key=lambda x: float(x["mtime"]), reverse=True)
    return out
