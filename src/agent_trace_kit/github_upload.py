"""Upload generated pair evidence to a local clone and push its branch."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _run(args: list[str], cwd: Path, timeout: int = 120) -> str:
    p = subprocess.run(args, cwd=cwd, text=True, encoding="utf-8", errors="replace",
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"{' '.join(args)} failed ({p.returncode}): {p.stdout[-4000:]}")
    return p.stdout.strip()


def upload_bundle(repo: str | Path, source: str | Path, destination: str = "data/pairs", message: str = "Add pair evaluation evidence", push: bool = True) -> dict:
    """Copy evidence into a Git clone, commit it and optionally push.

    Authentication is delegated to the user's configured Git credential helper
    or SSH agent. No token is read, printed or written by this function.
    """
    root = Path(repo).resolve(); src = Path(source).resolve()
    if not (root / ".git").exists(): raise ValueError("目标目录不是 Git 仓库")
    if not src.exists(): raise ValueError("待上传目录或文件不存在")
    target = (root / destination).resolve()
    if not target.is_relative_to(root):
        raise ValueError("上传子目录必须位于目标 Git 仓库内")
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    items = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file()]
    for item in items:
        rel = item.name if src.is_file() else str(item.relative_to(src)).replace("\\", "/")
        dest = target / rel; dest.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(item, dest); copied.append(str(dest.relative_to(root)).replace("\\", "/"))
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root)
    if staged.returncode != 0:
        raise ValueError("目标仓库已有 staged 改动，请先提交或清理后再上传")
    _run(["git", "add", "--", *copied], root)
    status = _run(["git", "status", "--short", "--", *copied], root)
    if not status: return {"ok": True, "changed": False, "pushed": False, "files": copied}
    _run(["git", "commit", "--only", "-m", message, "--", *copied], root)
    if push: _run(["git", "push"], root)
    return {"ok": True, "changed": True, "pushed": bool(push), "files": copied}
