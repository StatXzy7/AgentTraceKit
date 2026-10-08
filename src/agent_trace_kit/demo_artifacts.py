"""Serve only a job's evidence, with seekable video and a portable review ZIP."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import threading
import urllib.parse
import zipfile
from pathlib import Path

_BUNDLE_LOCK = threading.Lock()


def contained_file(value: str, root: Path) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("文件不在本任务证据目录内")
    return path


def byte_range(header: str, size: int) -> tuple[int, int]:
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if not match or not size or not any(match.groups()):
        raise ValueError("invalid range")
    left, right = match.groups()
    if not left:
        start, end = max(0, size - int(right)), size - 1
    else:
        start, end = int(left), min(size - 1, int(right)) if right else size - 1
    if start > end or start >= size:
        raise ValueError("invalid range")
    return start, end


def build_bundle(store, job: dict, output: Path) -> None:
    root = store.evidence_dir(job["id"]).resolve()
    files = [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()
             and p.resolve().is_relative_to(root) and not p.name.endswith(".recording.mp4")]
    total = sum(p.stat().st_size for p in files)
    if shutil.disk_usage(output.parent).free < total + 512 * 1024**2:
        raise RuntimeError("磁盘不足以创建结果包；请通过 scp 复制证据目录")
    manifest = []
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("job.json", json.dumps(job, ensure_ascii=False, indent=2))
        z.writestr("README.txt", "服务器自动演示证据包。先阅读 job.json 中 A/B 的 demo 状态，再播放视频和查看报告。\n"
                   "ready 只表示配置的演示步骤完成；GSB 与交付评分由复核者填写。失败日志原样保留。\n")
        for path in files:
            name = "evidence/" + path.relative_to(root).as_posix()
            digest = hashlib.sha256()
            with path.open("rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(block)
            manifest.append({"file": name, "bytes": path.stat().st_size, "sha256": digest.hexdigest()})
            z.write(path, name, compress_type=zipfile.ZIP_STORED if path.suffix == ".mp4" else zipfile.ZIP_DEFLATED)
        z.writestr("manifest.json", json.dumps(manifest, indent=2))


def serve_artifact(handler, server):
    temporary = None
    owns_lock = False
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
        job_id = query.get("job", [""])[0]
        if not re.fullmatch(r"pair-[a-f0-9]{10}", job_id):
            raise ValueError("无效任务 ID")
        job = server.store.get_job(job_id)
        kind = query.get("kind", [""])[0]
        root = server.store.evidence_dir(job_id)
        if kind == "bundle":
            if server.demos.busy(job_id) or any(server.runner.is_side_live(job_id, s) for s in ("A", "B")):
                raise RuntimeError("任务或演示仍在写入证据，请结束后下载")
            owns_lock = _BUNDLE_LOCK.acquire(blocking=False)
            if not owns_lock:
                raise RuntimeError("另一个结果包正在生成或下载，请稍后再试")
            with tempfile.NamedTemporaryFile(prefix="atk-review-", suffix=".zip", dir=server.store.home, delete=False) as f:
                temporary = Path(f.name)
            build_bundle(server.store, job, temporary)
            path, content_type, filename = temporary, "application/zip", f"{job_id}-results.zip"
        else:
            side = query.get("side", [""])[0]
            if side not in ("A", "B"):
                raise ValueError("无效侧名称")
            value = job["sides"][side]
            if kind == "video":
                path = contained_file(value.get("video_local", ""), root)
                content_type, filename = "video/mp4", path.name
            elif kind == "report":
                path = contained_file(value.get("demo", {}).get("report", ""), root)
                content_type, filename = "application/json; charset=utf-8", "report.json"
            else:
                raise ValueError("未知证据类型")
        size = path.stat().st_size
        start, end = 0, size - 1
        ranged = handler.headers.get("Range", "")
        if ranged:
            try:
                start, end = byte_range(ranged, size)
            except ValueError:
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{size}")
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
        handler.send_response(206 if ranged else 200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(end - start + 1))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        if kind == "bundle":
            handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if ranged:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        handler.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining:
                block = f.read(min(1024 * 1024, remaining))
                if not block:
                    break
                handler.wfile.write(block)
                remaining -= len(block)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        handler._send({"error": str(exc)}, 400)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
        if owns_lock:
            _BUNDLE_LOCK.release()
