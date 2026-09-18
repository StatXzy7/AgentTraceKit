"""Built-in screen recording for product-demo videos.

Uses ffmpeg gdigrab (Windows, primary monitor) so no third-party recorder is
needed. A hard ``-t 90`` cap enforces the spec's 90-second limit even if the
browser/server goes away; graceful 'q' shutdown finalises the mp4 properly.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path

from .checklist import video_duration_seconds
from .desk_store import DeskStore

MAX_SECONDS = 90
DEFAULT_FPS = 15


class Recorder:
    def __init__(self, store: DeskStore):
        self.store = store
        self._recs: dict[str, dict] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(job_id: str, side: str) -> str:
        return f"{job_id}/{side}"

    def _video_path(self, job_id: str, side: str) -> Path:
        return self.store.evidence_dir(job_id) / f"{side.lower()}-video.mp4"

    def start(self, job_id: str, side: str, *, fps: int = DEFAULT_FPS, max_seconds: int = MAX_SECONDS) -> dict:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("未找到 ffmpeg，请先安装或改用「选择…」绑定已有录屏文件")
        key = self._key(job_id, side)
        with self._lock:
            cur = self._recs.get(key)
            if cur and cur["proc"].poll() is None:
                raise RuntimeError(f"{side} 侧已在录屏中")

        out = self._video_path(job_id, side)
        logf = (self.store.evidence_dir(job_id) / f"{side.lower()}-video.log").open("w", encoding="utf-8")
        if out.exists():
            out.unlink()
        cmd = [
            ffmpeg, "-y", "-f", "gdigrab", "-framerate", str(fps), "-i", "desktop",
            "-t", str(max_seconds),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out),
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        rec = {"proc": proc, "path": out, "started": time.time(), "max": max_seconds, "logf": logf}
        with self._lock:
            self._recs[key] = rec

        def _watch():
            proc.wait()
            try:
                logf.close()
            except OSError:
                pass
            self._save(job_id, side, out)

        threading.Thread(target=_watch, daemon=True).start()
        return {"recording": True, "path": str(out), "max_seconds": max_seconds}

    def _save(self, job_id: str, side: str, out: Path) -> dict:
        with self._lock:
            self._recs.pop(self._key(job_id, side), None)
        info = {
            "recording": False,
            "path": str(out) if out.exists() else "",
            "size": out.stat().st_size if out.exists() else 0,
            "duration": video_duration_seconds(out) if out.exists() else None,
        }
        if info["size"] > 0:
            try:
                self.store.update_side(job_id, side, {"video_local": str(out)})
            except (FileNotFoundError, OSError):
                pass  # job record missing (e.g. synthetic call); file is kept anyway
        return info

    def stop(self, job_id: str, side: str) -> dict:
        key = self._key(job_id, side)
        with self._lock:
            rec = self._recs.get(key)
        out = self._video_path(job_id, side)
        if rec and rec["proc"].poll() is None:
            try:
                rec["proc"].stdin.write(b"q\n")
                rec["proc"].stdin.flush()
                rec["proc"].wait(timeout=15)  # graceful: ffmpeg writes the moov atom
            except (subprocess.TimeoutExpired, OSError):
                rec["proc"].terminate()
                try:
                    rec["proc"].wait(timeout=10)
                except subprocess.TimeoutExpired:
                    rec["proc"].kill()
                    rec["proc"].wait(timeout=10)
            # wait for the watcher to finalise
            for _ in range(30):
                with self._lock:
                    if key not in self._recs:
                        break
                time.sleep(0.2)
        return self._save(job_id, side, out)

    def status(self, job_id: str, side: str) -> dict:
        key = self._key(job_id, side)
        out = self._video_path(job_id, side)
        with self._lock:
            rec = self._recs.get(key)
        if rec and rec["proc"].poll() is None:
            return {
                "recording": True,
                "elapsed": int(time.time() - rec["started"]),
                "max": rec["max"],
                "path": str(out),
            }
        return {
            "recording": False,
            "path": str(out) if out.exists() else "",
            "size": out.stat().st_size if out.exists() else 0,
            "duration": video_duration_seconds(out) if out.exists() else None,
        }

    def stop_all(self) -> None:
        with self._lock:
            keys = list(self._recs.keys())
        for key in keys:
            job_id, side = key.split("/", 1)
            try:
                self.stop(job_id, side)
            except Exception:
                pass
