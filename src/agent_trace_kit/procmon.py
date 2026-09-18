"""Windows process-tree activity monitoring via ctypes (no third-party deps).

Used by the side-run watchdog to tell a genuinely busy claude process tree
(model streaming output, an install/test child running) from a gateway stream
stall: the SSE connection goes silent, no transcript is written and CPU/IO
counters stop moving. stdout is unreliable here because Python block-buffers
the redirected pipe, so liveness is decided from OS counters and transcript
file mtime instead.
"""
from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes
from pathlib import Path

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


def kill_tree(pid: int) -> None:
    """Kill the process and every descendant (taskkill /T /F)."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass


def newest_jsonl_mtime(projects_dir: Path, encoded_cwd: str) -> float:
    """Newest mtime of session transcripts belonging to this workspace."""
    newest = 0.0
    if not projects_dir.is_dir():
        return newest
    needle = encoded_cwd.lower()
    for d in projects_dir.iterdir():
        if d.is_dir() and needle in d.name.lower():
            for p in d.glob("*.jsonl"):
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if mtime > newest:
                    newest = mtime
    return newest
