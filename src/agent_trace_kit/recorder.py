"""Built-in screen recording for genuine product-demo videos.

Two capture targets are supported, both via ffmpeg (no third-party recorder):

- ``window=""`` (default): full primary monitor (``gdigrab desktop``) — captures
  terminal + browser and every switch between them, which is what a real
  end-to-end verification looks like.
- ``window="<title>"``: one OS window (``gdigrab title=...``), e.g. just the
  browser or just the terminal.

The recording is the operator's real product run: they start it, demonstrate
the product from a clean state, and stop when the run ends. An auto-stop cap
(default 89s, just under a strict 90s boundary check) exists so a forgotten
recording cannot run unbounded — a demo is only as long as the genuine run,
even a few seconds is fine. Manual stop and binding an externally recorded
mp4 remain available.
"""
from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from .checklist import video_duration_seconds
from .desk_store import DeskStore

DEFAULT_FPS = 15
# Just under the strict 90s downstream boundary so the auto-capped file is
# never exactly on it.
DEFAULT_MAX_SECONDS = 89


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

    @staticmethod
    def list_windows() -> list[dict]:
        """Visible top-level window titles via user32 (no PowerShell console).

        Best-effort: returns [] when the enumeration fails (e.g. headless CI);
        full-screen capture still works in that case.
        """
        if sys.platform != "win32":
            return []
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        is_visible = user32.IsWindowVisible
        is_visible.argtypes = [wintypes.HWND]
        is_visible.restype = wintypes.BOOL
        text_len = user32.GetWindowTextLengthW
        text_len.argtypes = [wintypes.HWND]
        text_len.restype = ctypes.c_int
        get_text = user32.GetWindowTextW
        get_text.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        get_text.restype = ctypes.c_int
        enum_windows = user32.EnumWindows
        wnd_enum = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        enum_windows.argtypes = [wnd_enum, wintypes.LPARAM]
        enum_windows.restype = wintypes.BOOL

        titles: list[dict] = []
        seen: set[str] = set()

        def _cb(hwnd, _lparam):
            try:
                if not is_visible(hwnd):
                    return True
                n = text_len(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                get_text(hwnd, buf, n + 1)
                t = buf.value.strip()
                if t and t not in seen:
                    seen.add(t)
                    titles.append({"title": t})
            except Exception:
                return True
            return True

        cb = wnd_enum(_cb)
        enum_windows(cb, 0)
        return titles

    def start(self, job_id: str, side: str, *, window: str = "",
              max_seconds: int = DEFAULT_MAX_SECONDS, fps: int = DEFAULT_FPS) -> dict:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("未找到 ffmpeg，请先安装或改用「选择…」绑定已有录屏文件")
        if max_seconds <= 0:
            max_seconds = int(self.store.settings().get("video_max_seconds", DEFAULT_MAX_SECONDS)) or DEFAULT_MAX_SECONDS
        if fps <= 0:
            fps = int(self.store.settings().get("video_fps", DEFAULT_FPS)) or DEFAULT_FPS
        key = self._key(job_id, side)
        with self._lock:
            cur = self._recs.get(key)
            if cur and cur["proc"].poll() is None:
                raise RuntimeError(f"{side} 侧已在录屏中")

        out = self._video_path(job_id, side)
        logf = (self.store.evidence_dir(job_id) / f"{side.lower()}-video.log").open("w", encoding="utf-8")
        if out.exists():
            out.unlink()
        target = f"title={window}" if window else "desktop"
        cmd = [
            ffmpeg, "-y", "-f", "gdigrab", "-framerate", str(fps),
            "-i", target,
            # Hard cap: stop the genuine demo recording at the limit even if the
            # operator forgets to press stop. "-t" makes ffmpeg exit cleanly, so
            # the mp4 moov atom is still finalised.
            "-t", str(max(1, int(max_seconds))),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out),
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        rec = {
            "proc": proc, "path": out, "started": time.time(), "logf": logf,
            "window": window, "max_seconds": max_seconds, "auto_stopped": False,
        }
        with self._lock:
            self._recs[key] = rec

        def _watch():
            proc.wait()
            # ffmpeg reaching its -t cap exits on its own; mark that so the UI
            # can say "auto-stopped at the cap" rather than implying a manual stop.
            with self._lock:
                r = self._recs.get(key)
                if r and time.time() - r["started"] >= max_seconds - 1:
                    r["auto_stopped"] = True
            try:
                logf.close()
            except OSError:
                pass
            self._save(job_id, side, out)

        threading.Thread(target=_watch, daemon=True).start()
        return {"recording": True, "path": str(out), "window": window, "max_seconds": max_seconds}

    def _save(self, job_id: str, side: str, out: Path) -> dict:
        with self._lock:
            rec = self._recs.pop(self._key(job_id, side), None)
        auto = bool(rec and rec.get("auto_stopped"))
        info = {
            "recording": False,
            "path": str(out) if out.exists() else "",
            "size": out.stat().st_size if out.exists() else 0,
            "duration": video_duration_seconds(out) if out.exists() else None,
            "auto_stopped": auto,
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
                stdin = rec["proc"].stdin
                if stdin is None:
                    raise ValueError("ffmpeg stdin closed")
                stdin.write(b"q\n")
                stdin.flush()
                rec["proc"].wait(timeout=15)  # graceful: ffmpeg writes the moov atom
            except (subprocess.TimeoutExpired, OSError, ValueError):
                rec["proc"].terminate()
                try:
                    rec["proc"].wait(timeout=10)
                except subprocess.TimeoutExpired:
                    rec["proc"].kill()
                    try:
                        rec["proc"].wait(timeout=10)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
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
                "max_seconds": rec.get("max_seconds", 0),
                "window": rec.get("window", ""),
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
