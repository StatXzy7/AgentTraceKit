"""Workspace preparation and git automation for A/B pair runs.

- copy one prepared baseline repository into two isolated workspaces
- create distinct branches, commit and push automatically
- verify 40-char SHAs, remote reachability and baseline ancestry
- locate the Claude Code session jsonl that ran inside a given workspace
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import time
import urllib.parse
from pathlib import Path

SHA40 = re.compile(r"^[0-9a-f]{40}$")


def robust_rmtree(path: str | Path, *, retries: int = 3) -> None:
    """Delete a directory tree on Windows, surviving read-only files and AV locks.

    Git pack files under .git/objects are marked read-only, which makes plain
    shutil.rmtree fail with WinError 5; antivirus scans right after a child
    process is killed can also hold transient locks, hence the short retry loop.
    """

    def _on_exc(func, p, _exc_info):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass

    target = Path(path)
    for attempt in range(retries):
        try:
            shutil.rmtree(target, onexc=_on_exc)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(0.5 * (attempt + 1))

# Vendor-required 1M context triplet. Injected into each side workspace's
# local settings so annotation runs are pinned to 1M without touching the
# user's global or cc-switch configuration. Model tiers are intentionally NOT
# overridden here: the gateway accepts the [1M] suffix on the main/opus/sonnet
# tiers but rejects it on the haiku tier (used for background title calls), so
# the proven global tier mapping is inherited as-is.
PINNED_MODEL = "auto_model/urm[1M]"
CONTEXT_ENV_1M = {
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
    "DISABLE_COMPACT": "1",
    "ANTHROPIC_BETAS": "context-1m-2025-08-07",
}
CONTEXT_SETTINGS_RELPATH = ".claude/settings.local.json"


def write_context_settings(workspace: str | Path) -> Path:
    """Merge the 1M/model-pin env into the workspace's local settings file."""
    ws_dir = Path(workspace)
    settings_path = ws_dir / CONTEXT_SETTINGS_RELPATH
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    env = dict(data.get("env") or {})
    # Remove model-pin keys from earlier versions (the gateway rejects [1M] on
    # the haiku tier); proven global tier mapping is inherited instead.
    for legacy_key in (
        "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        env.pop(legacy_key, None)
    env.update(CONTEXT_ENV_1M)
    data["env"] = env
    settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings_path


def _exclude_local_settings(workspace: str | Path) -> None:
    """Make sure .claude/settings.local.json is never committed (local exclude)."""
    exclude = Path(workspace) / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text(encoding="utf-8", errors="replace").splitlines() if exclude.exists() else []
    entries = {ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")}
    for rule in (".claude/settings.local.json",):
        if rule not in entries:
            lines.append(rule)
    exclude.write_text("\n".join(lines) + "\n", encoding="utf-8")
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


def canonical_remote(url: str) -> str:
    """Normalise an https/ssh/scp git remote to ``host/owner/repo`` (lowercase).

    So an HTTPS URL from ``gh`` and a user's existing SSH origin for the same
    repository compare equal. Returns "" when the shape is unrecognised.
    """
    s = (url or "").strip().removesuffix(".git").rstrip("/")
    if not s:
        return ""
    if "://" in s:
        parsed = urllib.parse.urlsplit(s)
        host = parsed.hostname or ""
        path = parsed.path.lstrip("/")
    elif "@" in s and ":" in s:  # scp-like: git@github.com:owner/repo
        user_host, _, path = s.partition(":")
        host = user_host.rsplit("@", 1)[-1]
        path = path.lstrip("/")
    else:
        return ""
    return f"{host.lower()}/{path.lower().strip('/')}"


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


def ensure_remote_contains(workspace: str | Path, sha: str, branch: str, *, force: bool = False) -> None:
    """Push branch until the remote contains the exact SHA (fast-forward by default).

    ``force`` is used for a side retry: the workspace was freshly re-copied from
    the baseline, so its history diverged from the previous product on the
    branch; the branch is tool-owned for this job/side and safe to overwrite.
    """
    if not force and sha_pushed(workspace, sha, branch):
        return
    ref = f"HEAD:refs/heads/{branch}"
    if force:
        git(["push", "--force", "origin", ref], workspace, timeout=300)
    elif _remote_branch_exists(workspace, branch):
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


# ---------- one-click baseline provisioning (local folder + GitHub repo) ----------

FALLBACK_GIT_IDENTITY = ("AgentTraceKit Desk", "agenttracekit@users.noreply.github.com")


def ensure_local_git_identity(workspace: str | Path) -> None:
    """Set a repo-local committer identity for any missing global piece.

    Global config is never touched; this is a fallback so ``git commit`` works on
    machines where the post-install identity step was skipped (a global email
    without a name, or vice versa, still leaves commit unable to run).
    """
    name, email = FALLBACK_GIT_IDENTITY
    out_email, _, code_email = git(["config", "user.email"], workspace, check=False)
    out_name, _, code_name = git(["config", "user.name"], workspace, check=False)
    if not (code_email == 0 and out_email.strip()):
        git(["config", "user.email", email], workspace)
    if not (code_name == 0 and out_name.strip()):
        git(["config", "user.name", name], workspace)


def provision_baseline_folder(
    parent_dir: str | Path,
    name: str,
    remote: str,
    default_branch: str,
    description: str,
) -> dict[str, object]:
    """Make *parent_dir/name* a local checkout of *remote*, seeding it when empty.

    Three starting states are handled:

    - neither local folder nor remote commits exist (freshly created GitHub
      repo): clone the empty repo, add README/.gitignore, make the initial
      commit on the default branch and push it
    - the remote already has commits (reused/existing repo) but no local
      folder: clone and use it as-is, never injecting a README commit
    - a local repo folder already exists: verify origin matches, fetch; seed
      only when it has zero commits

    User files are never overwritten and pushes are never forced.
    """
    from .ghutil import DEFAULT_GITIGNORE, INITIAL_COMMIT_MSG, render_readme, validate_repo_name

    parent = Path(parent_dir).resolve()
    repo_name = name.strip()
    if not repo_name:
        raise GitError("仓库名不能为空")
    if problem := validate_repo_name(repo_name):
        raise GitError(problem)
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / repo_name
    # Defense in depth: the resolved folder must live directly under parent
    # (blocks any traversal even if name validation were ever loosened).
    if target.resolve().parent != parent:
        raise GitError(f"仓库名越界，拒绝在 {target} 建仓")
    if target.is_symlink():
        raise GitError(f"目标路径是符号链接，拒绝跟随：{target}")
    if target.exists() and not target.is_dir():
        raise GitError(f"目标已存在且是一个文件（不会改动）：{target}")
    if target.is_dir() and any(target.iterdir()) and not (target / ".git").is_dir():
        raise GitError(f"目录已存在且非 git 仓库（不会改动其中文件）：{target}")

    if not (target / ".git").is_dir():
        # Works for both a normal repo and a brand-new empty GitHub repo
        # (clone succeeds; HEAD just points at an unborn branch).
        git(["clone", remote, str(target)], parent, timeout=300)

    origin = remote_url(target)
    if not origin:
        git(["remote", "add", "origin", remote.removesuffix(".git") + ".git"], target)
    elif canonical_remote(origin) != canonical_remote(remote):
        raise GitError(f"目录已有指向其他远端的 origin（{origin}），拒绝接管：{target}")
    git(["fetch", "origin", "--quiet"], target, timeout=120, check=False)

    branch = current_branch(target) or default_branch or "main"
    sha = head_sha(target)
    if not sha:
        # Clone can leave HEAD unborn when the remote HEAD points at a branch
        # that does not exist (fresh bare, or a renamed default branch). If the
        # remote actually has branches, track the default one (or the first)
        # instead of mistaking the repo for empty.
        out, _, code = git(["ls-remote", "--heads", "origin"], target, timeout=120, check=False)
        if code != 0:
            raise GitError("无法从远端读取分支列表（网络或鉴权失败），已停止以免误初始化")
        heads: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                heads[parts[1][len("refs/heads/"):]] = parts[0]
        if heads:
            pick = default_branch if default_branch in heads else next(iter(heads))
            git(["checkout", "-B", pick, f"origin/{pick}"], target)
            sha = head_sha(target)
            branch = current_branch(target) or pick
    seeded = False
    if not sha:
        # Genuinely empty repo (zero remote branches). Refuse to sweep up any
        # pre-existing user files into a (possibly public) initial push: only
        # seed when the work tree contains nothing but .git.
        tracked = git(["status", "--porcelain", "--untracked-files=all"], target, check=False)[0]
        strangers = [ln for ln in tracked.splitlines() if ln.strip()]
        if strangers:
            raise GitError(
                f"该仓库零提交但工作区已有文件，拒绝自动提交/推送（避免泄露）：{target}；"
                "请先自行提交这些文件，或清空目录后重试。"
            )
        # Pin HEAD to the requested default branch so the commit lands on main.
        branch = default_branch or "main"
        git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], target)
        if not (target / "README.md").exists():
            (target / "README.md").write_text(render_readme(repo_name, description), encoding="utf-8")
        if not (target / ".gitignore").exists():
            (target / ".gitignore").write_text(DEFAULT_GITIGNORE, encoding="utf-8")
        ensure_local_git_identity(target)
        # Commit ONLY the two managed files (never add -A of user content).
        git(["add", "--", "README.md", ".gitignore"], target)
        git(["commit", "-m", INITIAL_COMMIT_MSG], target)
        sha = head_sha(target)
        if not is_sha40(sha):
            raise GitError("初始提交后无法读取 40 位 SHA")
        git(["push", "-u", "origin", f"HEAD:refs/heads/{branch}"], target, timeout=300)
        seeded = True
        if not _remote_branch_exists(target, branch):
            raise GitError(f"push 后远端仍找不到分支 {branch}")
    return {
        "path": str(target),
        "sha": sha,
        "branch": branch,
        "url": commit_permalink(target, sha),
        "remote": remote_url(target),
        "seeded": seeded,
    }


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
    write_context_settings(dst)
    _exclude_local_settings(dst)
    return {"workspace": str(dst), "branch": branch, "head": head_sha(dst)}


def finalize_side(workspace: str | Path, branch: str, message: str, *, force: bool = False) -> dict[str, str]:
    """Auto commit all model changes and push; return 40-char sha + permalink."""
    sha = commit_all(workspace, message)
    if not is_sha40(sha):
        raise GitError("产物提交后无法读取 40 位 SHA")
    ensure_remote_contains(workspace, sha, branch, force=force)
    return {"sha": sha, "branch": branch, "url": commit_permalink(workspace, sha)}


# ---------- Claude Code session discovery ----------

def _encode_cwd(path: str | Path) -> str:
    """Replicate Claude Code projects directory encoding.

    D:\\myprojects\\x  ->  D--myprojects-x ; C:\\Users\\a -> C--Users-a
    """
    resolved = str(Path(path).resolve())
    return re.sub(r"[^A-Za-z0-9]", "-", resolved)


def _is_api_error_record(rec: dict) -> bool:
    """A synthetic gateway-error assistant record (CLI-generated, not model output).

    Claude Code marks these explicitly with ``isApiErrorMessage``; the model tag
    is ``<synthetic>``. A genuine model answer that merely quotes the words
    "API Error" must never count (the old text-prefix heuristic false-positived
    on tasks that document HTTP errors).
    """
    if rec.get("isApiErrorMessage"):
        return True
    msg = rec.get("message")
    if isinstance(msg, dict) and msg.get("model") == "<synthetic>":
        content = msg.get("content")
        if isinstance(content, list):
            text = "".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ).strip()
            if text.startswith("API Error"):
                return True
    return False


def _assistant_content_flags(rec: dict) -> tuple[bool, bool]:
    """Return (has_real_text, has_tool_use) for a non-synthetic-error assistant record."""
    content = rec.get("message", {}).get("content")
    has_text = False
    has_tool_use = False
    if isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and c.get("text", "").strip():
                has_text = True
            elif c.get("type") == "tool_use":
                has_tool_use = True
    elif isinstance(content, str) and content.strip():
        has_text = True
    return has_text, has_tool_use


def transcript_interruption_reason(path: str | Path) -> str | None:
    """Mechanically classify a transcript's tail.

    ``None`` means the single turn looks complete. Otherwise a stable code:

    - ``no_assistant``: no real assistant turn in the file
    - ``api_error``: the turn ended on a synthetic gateway error record
      (``isApiErrorMessage`` / ``<synthetic>`` model, e.g. seed-code 504) with
      no prior closing answer — the turn was cut mid-flight
    - ``dangling_tool_result``: the file ends after the last tool_result with
      no closing answer (silent SSE stall / killed stream / tool call awaiting
      a result that never arrived)
    - ``unreadable``: the file could not be opened

    A synthetic error AFTER a normal closing answer (the CLI's trailing
    auxiliary request 504s after the turn itself finished) is not a cut:
    that run exits non-zero but still produced a complete product.
    """
    try:
        records = []
        with Path(path).open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return "unreadable"

    last_result_pos = -1
    saw_real_assistant = False
    saw_error_record = False
    assistant_records: list[tuple[int, dict]] = []
    for i, rec in enumerate(records):
        rtype = rec.get("type")
        if rtype == "assistant":
            if _is_api_error_record(rec):
                saw_error_record = True
            else:
                saw_real_assistant = True
                assistant_records.append((i, rec))
        elif rtype == "user":
            content = rec.get("message", {}).get("content")
            if isinstance(content, list) and any(
                isinstance(c, dict) and c.get("type") == "tool_result" for c in content
            ):
                last_result_pos = i

    if not saw_real_assistant:
        return "api_error" if saw_error_record else "no_assistant"

    tail = [rec for i, rec in assistant_records if i > last_result_pos]
    if last_result_pos < 0:
        # No tool calls at all: any real assistant turn is a closed Q&A turn.
        return None

    if not tail:
        return "api_error" if saw_error_record else "dangling_tool_result"

    # A trailing synthetic error is benign once a real closer already exists
    # after the last tool_result (auxiliary post-turn request failed upstream).
    last = tail[-1]
    has_text, has_tool_use = _assistant_content_flags(last)
    if has_tool_use:
        # Last assistant turn requested another tool whose result never landed.
        return "dangling_tool_result"
    if has_text:
        return None
    # thinking-only / empty tail: the stream died while the model was working.
    return "dangling_tool_result"


def session_has_assistant(path: str | Path) -> bool:
    """True when the transcript contains a real (non-synthetic-error) assistant turn."""
    reason = transcript_interruption_reason(path)
    return reason not in ("unreadable", "no_assistant")


def session_turn_complete(path: str | Path) -> bool:
    """True when the transcript holds a closed single turn (see transcript_interruption_reason)."""
    # "no_assistant" preserves the previous vacuous-true behaviour; the runner
    # additionally requires session_has_assistant, so an empty transcript is
    # rejected there regardless.
    return transcript_interruption_reason(path) in (None, "no_assistant")


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
