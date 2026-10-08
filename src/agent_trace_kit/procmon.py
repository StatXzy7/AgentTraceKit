"""Process-tree monitoring on Windows (ctypes) and Linux (/proc).

Used by the side-run watchdog to tell a genuinely busy claude process tree
(model streaming output, an install/test child running) from a gateway stream
stall: the SSE connection goes silent, no transcript is written and CPU/IO
counters stop moving. stdout is unreliable here because Python block-buffers
the redirected pipe, so liveness is decided from OS counters and transcript
file mtime instead.
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any, Sequence

# CPU is reported in 100ns units; IO transfer counters are in bytes.
CPU_EPSILON_100NS = 10_000_000        # 1.0s of root-process CPU per poll window
TOOL_CPU_EPSILON_100NS = 3_000_000    # 0.3s from freshly spawned tool children
IO_EPSILON_BYTES = 64 * 1024          # 64 KiB of read/write traffic

# Long-lived MCP/helper children (context7, codex runtimes) emit heartbeat CPU
# even while the main request stalls; they must not count as request activity.
IDLE_HELPER_NAMES = {"node.exe", "python.exe"}


class _PROCESS_TIMES(ctypes.Structure):
    _fields_ = [
        ("CreateTime", wintypes.FILETIME),
        ("ExitTime", wintypes.FILETIME),
        ("KernelTime", wintypes.FILETIME),
        ("UserTime", wintypes.FILETIME),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


def _ft(ft: wintypes.FILETIME) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def snapshot() -> dict[int, dict]:
    """pid -> {ppid, cpu_100ns, io_bytes} for every process, best effort."""
    if sys.platform != "win32":
        return _snapshot_linux()
    psapi = ctypes.WinDLL("psapi.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

    capacity = 1024
    for _ in range(6):
        arr_t = wintypes.DWORD * capacity
        pids = arr_t()
        needed = wintypes.DWORD()
        ok = psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed))
        if not ok:
            return {}
        if needed.value <= ctypes.sizeof(pids):
            break
        capacity *= 2
    count = needed.value // ctypes.sizeof(wintypes.DWORD)

    out: dict[int, dict] = {}
    for i in range(count):
        pid = pids[i]
        if pid == 0:
            continue
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            times = _PROCESS_TIMES()
            io = _IO_COUNTERS()
            ppid = wintypes.DWORD()
            ok_t = kernel32.GetProcessTimes(
                h, ctypes.byref(times.CreateTime), ctypes.byref(times.ExitTime),
                ctypes.byref(times.KernelTime), ctypes.byref(times.UserTime))
            ok_io = kernel32.GetProcessIoCounters(h, ctypes.byref(io))
            # parent pid via BasicInfo is fiddly; derive ancestry separately
            if ok_t and ok_io:
                out[pid] = {
                    "ppid": 0, "name": "",
                    "cpu": _ft(times.KernelTime) + _ft(times.UserTime),
                    "io": io.ReadTransferCount + io.WriteTransferCount,
                }
        except OSError:
            pass
        finally:
            kernel32.CloseHandle(h)
    # parent PIDs come from the toolhelp snapshot (single extra call)
    _fill_ppids(out)
    return out


def _snapshot_linux() -> dict[int, dict]:
    result = {}
    ticks = os.sysconf("SC_CLK_TCK")
    for path in Path("/proc").glob("[0-9]*"):
        try:
            raw = (path / "stat").read_text(encoding="utf-8")
            end = raw.rindex(")")
            fields = raw[end + 2:].split()
            item = {
                "ppid": int(fields[1]), "name": raw[raw.index("(") + 1:end],
                "cpu": (int(fields[11]) + int(fields[12])) * 10_000_000 // ticks,
                "io": 0, "start_time": fields[19], "state": fields[0],
            }
            try:
                counters = dict(line.split(":", 1) for line in
                                (path / "io").read_text(encoding="utf-8").splitlines())
                item["io"] = int(counters.get("rchar", 0)) + int(counters.get("wchar", 0))
            except (OSError, ValueError):
                pass  # Other users' IO counters may be unreadable.
            result[int(path.name)] = item
        except (OSError, ValueError, IndexError):
            continue  # The process can exit during a snapshot.
    return result


def _fill_ppids(info: dict[int, dict]) -> None:
    TH32CS_SNAPPROCESS = 0x00000002

    class _ENTRY(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    k32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return
    try:
        entry = _ENTRY()
        entry.dwSize = ctypes.sizeof(_ENTRY)
        if not k32.Process32First(snap, ctypes.byref(entry)):
            return
        while True:
            pid, ppid = entry.th32ProcessID, entry.th32ParentProcessID
            if pid in info:
                info[pid]["ppid"] = ppid
                try:
                    info[pid]["name"] = entry.szExeFile.decode("mbcs", "ignore").lower()
                except (UnicodeDecodeError, AttributeError):
                    info[pid]["name"] = ""
            if not k32.Process32Next(snap, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snap)


def descendants(root_pid: int, snap: dict[int, dict]) -> set[int]:
    """All live pids in root's tree (root included), robust to PID reuse churn."""
    out = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, info in snap.items():
            if pid not in out and info["ppid"] in out:
                out.add(pid)
                changed = True
    return {p for p in out if p in snap}


def tree_busy(prev: dict[int, dict], cur: dict[int, dict], pids: set[int], root_pid: int) -> bool:
    for pid in pids:
        a, b = prev.get(pid), cur.get(pid)
        if not (a and b):
            continue
        cpu_delta = b["cpu"] - a["cpu"]
        if pid == root_pid and cpu_delta >= CPU_EPSILON_100NS:
            return True
        if b.get("name") in IDLE_HELPER_NAMES:
            continue
        if cpu_delta >= TOOL_CPU_EPSILON_100NS:
            return True
        if b["io"] - a["io"] >= IO_EPSILON_BYTES:
            return True
    return False


def hidden_creationflags() -> int:
    """CREATE_NO_WINDOW for a leaf tool (netstat, taskkill) that spawns no shell.

    Do not use this for ``claude`` or ``shell=True`` checks. A process started
    with no console forces each console grandchild — Claude Code's per-command
    ``powershell.exe`` — to allocate its own visible window.
    """
    if sys.platform != "win32":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def hidden_console_kwargs() -> dict[str, Any]:
    """Popen kwargs: one hidden console, inherited by console grandchildren.

    ``CREATE_NEW_CONSOLE`` plus ``SW_HIDE`` gives the child a console that
    already starts hidden. ``powershell.exe`` / ``cmd.exe`` spawned underneath
    attach to it instead of opening a desktop window. Redirected stdout/stderr
    are unchanged, so logs and the stall watchdog still see the same pipes.
    """
    if sys.platform != "win32":
        # Give each CLI/check its own process group for timeout/abort cleanup.
        return {"start_new_session": True}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= int(subprocess.STARTF_USESHOWWINDOW)
    startupinfo.wShowWindow = int(subprocess.SW_HIDE)
    return {
        "startupinfo": startupinfo,
        "creationflags": int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)),
    }


def run_hidden(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """subprocess.run without flashing a console window on Windows."""
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", hidden_creationflags())
    return subprocess.run(args, **kwargs)


def kill_tree(pid: int) -> None:
    """Reap the owned process group on POSIX, or taskkill /T /F on Windows."""
    if pid <= 1 or pid == os.getpid():
        return
    if sys.platform != "win32":
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            # A completed session leader may leave children in its group.
            group = pid
        if group == pid and group != os.getpgrp():
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            # Never signal the caller's/shared process group.
            before = snapshot()
            for child in sorted(descendants(pid, before), reverse=True):
                current = snapshot().get(child)
                if current and current.get("start_time") == before[child].get("start_time"):
                    try:
                        os.kill(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        return
    try:
        run_hidden(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        try:
            run_hidden(["taskkill", "/F", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass


def newest_jsonl_mtime(projects_dir: Path, encoded_cwd: str) -> float:
    """Newest mtime of session transcripts belonging to this workspace."""
    from .workspace import native_path
    newest = 0.0
    if not projects_dir.is_dir():
        return newest
    needle = encoded_cwd.lower()
    for d in projects_dir.iterdir():
        d = native_path(d)
        if d.is_dir() and needle in d.name.lower():
            for p in d.glob("*.jsonl"):
                p = native_path(p)
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if mtime > newest:
                    newest = mtime
    return newest
