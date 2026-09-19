"""GitHub automation for provisioning empty baseline repos via the ``gh`` CLI.

The annotator only supplies a project name and a README blurb; this module
checks name validity, detects whether the remote already exists and creates it
when missing. ``gh`` must be installed and authenticated (``gh auth status``);
the desk surfaces a clear Chinese error otherwise.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

# GitHub repo names: alphanumerics, hyphens, underscores, periods; max 100 chars.
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# Windows folder names that are reserved (baseline repo name == local folder).
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
DEFAULT_GITIGNORE = "# Python\n__pycache__/\n*.py[cod]\n.venv/\nvenv/\n.env\n" \
                    "# Node\nnode_modules/\n" \
                    "# OS / editor\n.DS_Store\nThumbs.db\n.idea/\n.vscode/\n"
INITIAL_COMMIT_MSG = "chore: initialize baseline repository"


class GitHubError(RuntimeError):
    """A GitHub/gh CLI failure (not authenticated, network, rejected create)."""


class RepoNameError(ValueError):
    """The requested repository/folder name is invalid."""


def validate_repo_name(name: str) -> str:
    """Return an error message for an invalid GitHub/Windows folder name, "" when OK."""
    s = (name or "").strip()
    if not s:
        return "仓库名不能为空"
    if len(s) > 100:
        return "仓库名最长 100 个字符"
    if not REPO_NAME_RE.match(s):
        return "仓库名只能包含字母、数字、连字符 -、下划线 _ 和点 ."
    if s.startswith("-"):
        # Never let a name be mistaken for a CLI flag if it ever reaches git/gh
        # as a bare positional argument.
        return "仓库名不能以连字符 - 开头"
    if s in {".", ".."} or s.endswith(".") or s.endswith(" "):
        return "仓库名不能以点结尾或为 . / .."
    if ".." in s:
        return "仓库名不能包含连续的点"
    # Windows reserves the base name before the FIRST dot (con.txt, nul.md ...).
    if s.split(".", 1)[0].lower() in _WINDOWS_RESERVED:
        return f"「{s}」是 Windows 保留名称，不能用作文件夹名"
    return ""


def render_readme(repo_name: str, description: str) -> str:
    """Render the initial README.md. A blank blurb yields a minimal placeholder."""
    desc = (description or "").strip()
    title = repo_name.strip()
    if not desc:
        desc = "Pair-wise 评测基线仓库（初始化占位，题目初始代码将在此提交）。"
    return f"# {title}\n\n{desc}\n"


@dataclass(frozen=True)
class RemoteRepo:
    name: str
    owner: str
    url: str            # browser URL, no .git suffix
    default_branch: str
    visibility: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class GhRunner(Protocol):
    def viewer_login(self) -> str: ...
    def repo_view(self, owner: str, name: str) -> RemoteRepo | None: ...
    def repo_create(self, owner: str, name: str, description: str, private: bool) -> RemoteRepo: ...


class CliGh:
    """``gh`` CLI backed implementation. Timeouts are generous (network calls)."""

    def __init__(self, *, timeout: int = 60):
        self._timeout = timeout

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["gh", *args], text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self._timeout,
            )
        except FileNotFoundError as exc:
            raise GitHubError("未找到 gh 命令，请先安装 GitHub CLI（https://cli.github.com）") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubError(f"gh {' '.join(args)} 超时（网络不通？）") from exc

    def viewer_login(self) -> str:
        p = self._run(["api", "user", "--jq", ".login"])
        if p.returncode != 0 or not p.stdout.strip():
            raise GitHubError(
                "GitHub CLI 未登录或 token 无效，请在终端执行 `gh auth login` "
                f"（需要 repo 权限）。gh 返回：{p.stderr.strip() or p.stdout.strip()}"
            )
        return p.stdout.strip()

    def repo_view(self, owner: str, name: str) -> RemoteRepo | None:
        """Return the repo when it exists remotely, None on a definitive 404."""
        p = self._run([
            "repo", "view", f"{owner}/{name}",
            "--json", "name,owner,url,defaultBranchRef,visibility",
        ])
        if p.returncode == 0:
            try:
                data = json.loads(p.stdout)
            except json.JSONDecodeError as exc:
                raise GitHubError(f"gh 返回了无法解析的 JSON：{p.stdout[:200]}") from exc
            owner_obj = data.get("owner") or {}
            branch = (data.get("defaultBranchRef") or {}).get("name") or "main"
            return RemoteRepo(
                name=data.get("name", name),
                owner=owner_obj.get("login", owner),
                url=(data.get("url") or f"https://github.com/{owner}/{name}").removesuffix(".git"),
                default_branch=branch,
                visibility=str(data.get("visibility", "")).upper() or "UNKNOWN",
            )
        err = (p.stderr + p.stdout).lower()
        # Only gh's own repository-resolution failures mean "does not exist".
        # Bare "host not found"/"no such host" proxy noise must NOT be treated
        # as 404 (that would wrongly attempt to recreate an existing repo).
        not_found = "could not resolve to a repository" in err or (
            "404" in err and "repository" in err
        )
        if not_found:
            return None
        raise GitHubError(f"检查远端仓库失败：{p.stderr.strip() or p.stdout.strip()}")

    def repo_create(self, owner: str, name: str, description: str, private: bool) -> RemoteRepo:
        # gh (incl. 2.x) does NOT support --json on `repo create`; create, then
        # read the canonical metadata back with `repo view`.
        args = ["repo", "create", f"{owner}/{name}", "--private" if private else "--public"]
        if description.strip():
            args += ["--description", description.strip()[:350]]
        p = self._run(args)
        if p.returncode != 0:
            raise GitHubError(f"创建 GitHub 仓库失败：{p.stderr.strip() or p.stdout.strip()}")
        repo = self.repo_view(owner, name)
        if repo is None:
            # Create reported success but the repo is not visible yet; build a
            # minimal record from known values rather than failing the whole run.
            return RemoteRepo(
                name=name, owner=owner, url=f"https://github.com/{owner}/{name}",
                default_branch="main", visibility="PRIVATE" if private else "PUBLIC",
            )
        return repo


def ensure_remote_repo(
    name: str,
    description: str,
    *,
    private: bool,
    gh: GhRunner | None = None,
    owner: str = "",
) -> tuple[RemoteRepo, bool]:
    """Validate *name*; return (repo, created?). Existing repos are reused, never recreated."""
    problem = validate_repo_name(name)
    if problem:
        raise RepoNameError(problem)
    cli = gh or CliGh()
    login = cli.viewer_login()
    if owner and owner.lower() != login.lower():
        # Do not create repos under arbitrary accounts/orgs, and do not clone a
        # third party's repo as the trusted baseline, just because a name was
        # typed. Leave the setting blank to use the authenticated account.
        raise GitHubError(
            f"目标账号 {owner} 与当前 gh 登录账号 {login} 不一致；"
            "请清空后台设置里的「GitHub 归属账号」或改成当前账号"
        )
    existing = cli.repo_view(login, name)
    if existing is not None:
        return existing, False
    try:
        return cli.repo_create(login, name, description, private), True
    except GitHubError as exc:
        # A concurrent job (double-click, batch) may have just created it:
        # re-check once and reuse instead of surfacing "name already exists".
        if "already exists" in str(exc).lower() or "name already been taken" in str(exc).lower():
            raced = cli.repo_view(login, name)
            if raced is not None:
                return raced, False
        raise


def remote_repo_from_view(view: dict[str, Any], fallback_name: str, fallback_owner: str) -> RemoteRepo:
    """Build a RemoteRepo from a ``gh repo view --json`` payload (helper/tests)."""
    owner_obj = view.get("owner") or {}
    branch = (view.get("defaultBranchRef") or {}).get("name") or "main"
    owner = owner_obj.get("login", fallback_owner)
    return RemoteRepo(
        name=view.get("name", fallback_name),
        owner=owner,
        url=(view.get("url") or f"https://github.com/{owner}/{fallback_name}").removesuffix(".git"),
        default_branch=branch,
        visibility=str(view.get("visibility", "")).upper() or "UNKNOWN",
    )
