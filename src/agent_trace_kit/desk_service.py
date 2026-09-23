"""Detach Pair Desk from the console so it outlives the PowerShell window.

The HTTP worker is spawned by a supervisor that restarts it on crash. The
operator stops it only via ``desk stop`` or the control-window 退出 button.
This is not a Windows Service: screen recording and file dialogs need the
interactive desktop.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .desk_store import DEFAULT_HOME

DEFAULT_PORT = 8765
# Consecutive failed HTTP probes while the worker PID is still alive.
HTTP_HEALTH_FAILS = 30
_WIN_DETACH = 0
if sys.platform == "win32":
    _WIN_DETACH = (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )


def service_paths(home: str | Path | None = None) -> dict[str, Path]:
    root = Path(home) if home else DEFAULT_HOME
    root.mkdir(parents=True, exist_ok=True)
    return {
        "home": root,
        "stop": root / "desk.stop",
        "supervisor_pid": root / "desk.supervisor.pid",
        "worker_pid": root / "desk.worker.pid",
        "app_pid": root / "desk.app.pid",
        "log": root / "desk-service.log",
    }


def desk_url(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{int(port)}/"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def read_pid(path: Path) -> int:
    try:
        return int((path.read_text(encoding="utf-8") or "0").strip() or "0")
    except (OSError, ValueError):
        return 0


def write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(pid)), encoding="utf-8")


def probe_desk(port: int = DEFAULT_PORT, timeout: float = 0.6) -> bool:
    try:
        urllib.request.urlopen(desk_url(port), timeout=timeout)
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def is_running(port: int = DEFAULT_PORT, home: str | Path | None = None) -> dict:
    paths = service_paths(home)
    supervisor = read_pid(paths["supervisor_pid"])
    worker = read_pid(paths["worker_pid"])
    http_up = probe_desk(port)
    return {
        "running": http_up or pid_alive(supervisor) or pid_alive(worker),
        "http": http_up,
        "url": desk_url(port),
        "supervisor_pid": supervisor if pid_alive(supervisor) else 0,
        "worker_pid": worker if pid_alive(worker) else 0,
        "port": int(port),
    }


def _log(paths: dict[str, Path], message: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}\n"
    try:
        with paths["log"].open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def pythonw_executable() -> str:
    """Prefer pythonw.exe: no console flash, and ``Get-Process python`` misses us."""
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.is_file():
            return str(candidate)
    return str(exe)


def _popen_detached(args: list[str], paths: dict[str, Path]) -> subprocess.Popen:
    logf = paths["log"].open("a", encoding="utf-8")
    kwargs: dict = {
        "args": args,
        "stdin": subprocess.DEVNULL,
        "stdout": logf,
        "stderr": subprocess.STDOUT,
        "cwd": os.getcwd(),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _WIN_DETACH
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(**kwargs)


def request_stop(home: str | Path | None = None) -> Path:
    paths = service_paths(home)
    paths["stop"].write_text("1", encoding="utf-8")
    return paths["stop"]


def clear_stop(home: str | Path | None = None) -> None:
    path = service_paths(home)["stop"]
    try:
        path.unlink()
    except OSError:
        pass


def _terminate_pid(pid: int) -> None:
    if not pid_alive(pid):
        return
    if sys.platform == "win32":
        try:
            from .procmon import run_hidden
            run_hidden(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass


def _port_pid(port: int) -> int:
    try:
        from . import ports as ports_mod
        for row in ports_mod.list_listeners():
            if int(row.get("port") or 0) == int(port):
                return int(row.get("pid") or 0)
    except Exception:
        return 0
    return 0


def stop_desk(port: int = DEFAULT_PORT, home: str | Path | None = None) -> dict:
    """Ask the supervisor to exit, then force-reap leftover pids."""
    paths = service_paths(home)
    request_stop(home)
    for key in ("app_pid", "worker_pid", "supervisor_pid"):
        _terminate_pid(read_pid(paths[key]))
    listener = _port_pid(port)
    if listener and listener not in (
        read_pid(paths["worker_pid"]), read_pid(paths["supervisor_pid"]),
    ):
        _terminate_pid(listener)
    deadline = time.time() + 8
    while time.time() < deadline and probe_desk(port, timeout=0.3):
        time.sleep(0.2)
    for key in ("supervisor_pid", "worker_pid", "app_pid"):
        try:
            paths[key].unlink()
        except OSError:
            pass
    return {"stopped": not probe_desk(port, timeout=0.3), "url": desk_url(port)}


def _reap_proc(proc) -> None:
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def supervise(port: int = DEFAULT_PORT, home: str | Path | None = None,
              *, spawn=None, sleep_fn=time.sleep, forever: bool = True,
              probe=None, health_fails: int = HTTP_HEALTH_FAILS) -> int:
    """Restart the HTTP worker until a stop file appears.

    ``spawn`` / ``forever`` / ``probe`` / ``health_fails`` are seams for tests.
    A worker that stays alive but stops answering HTTP is treated as dead.
    """
    paths = service_paths(home)
    write_pid(paths["supervisor_pid"], os.getpid())
    spawn = spawn or _spawn_worker
    probe = probe or (lambda: probe_desk(port))
    backoff = 1.0
    while True:
        if paths["stop"].is_file():
            _log(paths, "supervisor: stop requested")
            return 0
        try:
            proc = spawn(port, paths)
        except Exception as exc:
            _log(paths, f"supervisor: spawn failed: {exc}")
            if not forever:
                raise
            if paths["stop"].is_file():
                return 0
            sleep_fn(backoff)
            backoff = min(30.0, backoff * 2)
            continue
        write_pid(paths["worker_pid"], getattr(proc, "pid", 0) or 0)
        _log(paths, f"supervisor: worker pid={getattr(proc, 'pid', '?')} started")
        unhealthy = 0
        seen_up = False
        backoff = 1.0
        while proc.poll() is None:
            if paths["stop"].is_file():
                _reap_proc(proc)
                _log(paths, "supervisor: stop requested, worker reaped")
                return 0
            sleep_fn(1)
            if probe():
                seen_up = True
                unhealthy = 0
            elif seen_up:
                unhealthy += 1
            if seen_up and unhealthy >= max(1, int(health_fails)):
                _log(paths, f"supervisor: worker HTTP unresponsive for {unhealthy}s, restarting")
                _reap_proc(proc)
                break
        if paths["stop"].is_file():
            return 0
        if proc.poll() is None:
            code = "health"
        else:
            code = proc.returncode
            _log(paths, f"supervisor: worker exited {code}, restart in {backoff:.0f}s")
        if not forever:
            return int(code or 0) if isinstance(code, int) else 1
        sleep_fn(backoff)
        backoff = min(30.0, backoff * 2)


def _spawn_worker(port: int, paths: dict[str, Path]) -> subprocess.Popen:
    args = [pythonw_executable(), "-m", "agent_trace_kit.desk", "--worker",
            "--port", str(int(port)), "--no-browser"]
    return _popen_detached(args, paths)


def start_supervisor(port: int = DEFAULT_PORT, home: str | Path | None = None,
                     *, popen=None) -> int:
    paths = service_paths(home)
    clear_stop(home)
    args = [pythonw_executable(), "-m", "agent_trace_kit.desk", "--supervise",
            "--port", str(int(port))]
    launcher = popen or _popen_detached
    proc = launcher(args, paths)
    write_pid(paths["supervisor_pid"], proc.pid or 0)
    return int(proc.pid or 0)


def start_app_window(port: int = DEFAULT_PORT, home: str | Path | None = None,
                     *, popen=None) -> int:
    paths = service_paths(home)
    existing = read_pid(paths["app_pid"])
    if pid_alive(existing):
        return existing
    args = [pythonw_executable(), "-m", "agent_trace_kit.desk", "--app",
            "--port", str(int(port))]
    launcher = popen or _popen_detached
    proc = launcher(args, paths)
    write_pid(paths["app_pid"], proc.pid or 0)
    return int(proc.pid or 0)


def wait_until_up(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe_desk(port, timeout=0.4):
            return True
        time.sleep(0.25)
    return probe_desk(port, timeout=0.4)


def start_desk(port: int = DEFAULT_PORT, home: str | Path | None = None,
               *, open_browser: bool = True, open_app: bool = True) -> dict:
    """Ensure the detached desk is up. Idempotent if already running."""
    import webbrowser

    status = is_running(port, home)
    already = bool(status["http"])
    if not already:
        if status.get("supervisor_pid") or status.get("worker_pid"):
            wait_until_up(port, timeout=60.0)
        else:
            start_supervisor(port, home)
            wait_until_up(port)
        status = is_running(port, home)
    if open_app:
        start_app_window(port, home)
    if open_browser:
        try:
            webbrowser.open(desk_url(port))
        except Exception:
            pass
    return {
        **status,
        "url": desk_url(port),
        "already": already,
    }
