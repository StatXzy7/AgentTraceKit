"""Windows Job Object wrapper that reaps every descendant of a claude attempt.

The agent runs ``claude -p`` in a throwaway workspace and the model frequently
starts local web servers itself (``node src/server.js &``, ``cmd /c start``,
``npm run dev`` …). On Windows a Git-Bash ``&`` detaches the grandchild and,
once the intermediate shell exits, the OS reparents it, so a ``taskkill /T``
from the runner cannot reach it: the terminal tab is gone but the node server
keeps listening on its port — exactly the zombie-port problem.

A Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` solves it structurally:
every process assigned to the job stays in the job for its whole life regardless
of reparenting, detaching, or console closing, and (because the job does not
allow silent breakaway) none of its descendants can opt out. Closing the job
handle — explicitly at the end of an attempt, or automatically by the OS if the
desk crashes — terminates the whole tree atomically.

Falls back gracefully (returns a no-op object) when assignment is impossible
(e.g. already living under an incompatible job); the caller then keeps using the
best-effort ``taskkill /T`` path.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

# --- constants -------------------------------------------------------------

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

JobObjectExtendedLimitInformation = 9

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class KillJob:
    """Owns one Job Object. ``close()`` (or process death) reaps the whole set.

    Use as a context manager. On non-Windows, or if the process cannot be
    assigned, it degrades to a no-op and ``alive`` reports False so callers can
    fall back to taskkill-tree cleanup.
    """

    def __init__(self) -> None:
        self._handle: wintypes.HANDLE | None = None
        self.alive = False
        self.reason = ""
        if sys.platform != "win32":
            self.reason = "non-windows"
            return
        try:
            self._build()
        except OSError as exc:
            self._drop()
            self.reason = f"create-failed: {exc}"

    def _build(self) -> None:
        k32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateJobObject.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        self._k32 = k32

        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle

        info = _ExtendedLimitInformation()
        # No breakaway of any kind: even a `cmd /c start` / Git-Bash `&`
        # grandchild cannot leave the set. KILL_ON_JOB_CLOSE makes the handle
        # the single reaping switch, including on an unclean desk crash.
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(
            handle, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        # Created and configured; assignment happens per process in add_pid.
        self.alive = True

    def add_pid(self, pid: int) -> bool:
        """Assign a live process (and, by inheritance, all its descendants).

        Must be called immediately after the child is created, before it spawns
        children of its own. Returns False (and disables this job) if the OS
        refuses assignment, so the caller can fall back to tree killing.
        """
        if not self.alive or self._handle is None:
            return False
        k32 = self._k32
        hproc = k32.OpenProcess(PROCESS_TERMINATE | PROCESS_SET_QUOTA, False, pid)
        if not hproc:
            self.alive = False
            self.reason = f"open-process-failed: winerr {ctypes.get_last_error()}"
            return False
        try:
            ok = k32.AssignProcessToJobObject(self._handle, hproc)
            if not ok:
                err = ctypes.get_last_error()
                self.alive = False
                self.reason = f"assign-failed: winerr {err}"
                return False
        finally:
            k32.CloseHandle(hproc)
        return True

    def terminate(self, exit_code: int = 1) -> None:
        """Force-reap every process in the job now (stall/timeout/abort)."""
        if not self.alive or self._handle is None:
            return
        try:
            self._k32.TerminateJobObject(self._handle, exit_code)
        except OSError:
            pass

    def _drop(self) -> None:
        if self._handle is not None:
            try:
                self._k32.CloseHandle(self._handle)
            except OSError:
                pass
            self._handle = None
        self.alive = False

    def close(self) -> None:
        """Close the handle; with KILL_ON_JOB_CLOSE this reaps the whole set."""
        if self._handle is not None:
            self._drop()

    def __enter__(self) -> "KillJob":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
