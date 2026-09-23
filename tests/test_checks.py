"""Check-command runner: skip GUI/server launchers; timeout must reap children."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_trace_kit import procmon
from agent_trace_kit.runner import is_long_running_start, run_check_commands


@pytest.mark.parametrize(
    "command, expect",
    [
        ("python -m unittest discover -s tests -v", False),
        ("python -m pytest -q", False),
        ("python -m pip install Pillow", False),
        ("python -m tests", False),
        ("python -m box_geometry --selftest", False),
        ("python -m venv .venv", False),
        ("python -m coverage run -m unittest", False),
        ("py -3 -m pytest -q", False),
        ("npm test", False),
        ("npm run build", False),
        ("go test ./...", False),
        ("go vet ./...", False),
        ("cargo test --all-targets", False),
        ("cargo build --release", False),
        ("cargo run -- --help", False),
        ("python -m box_geometry", True),
        ("py -3 -m sensor_align", True),
        ("pythonw -m frame_studio", True),
        ("python -m pytest -q && python -m sensor_align", True),
        ("node server.js --port 0", True),
        ("python -m sensor_align", True),
        ("python -m temporal_join", True),
        ("python -m frame_studio", True),
        ("python -m trace_studio --port 0", True),
        ("python app.py", True),
        ("python src/server.py", True),
        ("python src/app.py", True),
        ("python serve.py", True),
        ("python -m http.server 8080", True),
        ("uvicorn main:app --port 0", True),
        ("uvicorn", True),
        ("gunicorn app:app", True),
        ("daphne app:asgi", True),
        ("hypercorn app:asgi", True),
        ("dotnet run", True),
        ("dotnet runtime --list-runtimes", False),
        ("php -S 127.0.0.1:8080", True),
        ("php -s script.php", False),
        ("python -u src/server.py", True),
        ("python -u -m http.server 8080", True),
        ("dotnet test", False),
        ("npm start", True),
        ("npm start -- --port 0", True),
        ("npm run dev", True),
        ("go run . -port 0", True),
        ("cargo run", True),
    ],
)
def test_classifies_start_commands(command, expect):
    assert is_long_running_start(command) is expect


def test_skips_start_command_without_spawning():
    t0 = time.monotonic()
    results = run_check_commands(["python -m sensor_align"], Path.cwd(), timeout=30)
    assert time.monotonic() - t0 < 2
    assert results == [{
        "command": "python -m sensor_align",
        "exit_code": None,
        "skipped": True,
        "reason": results[0]["reason"],
    }]
    assert "跳过" in results[0]["reason"]


def test_check_command_inherits_hidden_console(monkeypatch, tmp_path):
    """shell=True checks must not use CREATE_NO_WINDOW, or powershell children pop."""
    from agent_trace_kit import runner as rn
    captured: dict = {}

    class Fake:
        pid = 4242
        returncode = 0

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(*_a, **kw):
        captured.update(kw)
        return Fake()

    monkeypatch.setattr(rn.subprocess, "Popen", fake_popen)
    results = run_check_commands(["echo hi"], tmp_path, timeout=5)
    assert results[0]["exit_code"] == 0
    expected = procmon.hidden_console_kwargs()
    assert captured.get("creationflags", 0) == expected.get("creationflags", 0)
    if "startupinfo" in expected:
        si = captured["startupinfo"]
        assert si.dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert si.wShowWindow == subprocess.SW_HIDE
        assert not (captured["creationflags"] & subprocess.CREATE_NO_WINDOW)


def test_finite_command_runs_to_completion(tmp_path):
    marker = tmp_path / "ok.txt"
    cmd = f'{sys.executable} -c "open(r\'{marker}\', \'w\').write(\'ok\')"'
    results = run_check_commands([cmd], tmp_path, timeout=15)
    assert results[0]["exit_code"] == 0
    assert results[0].get("skipped") is not True
    assert marker.read_text(encoding="utf-8") == "ok"


def _pid_alive(pid: int) -> bool:
    """Query-only liveness. Do not use os.kill: on Windows it TerminateProcess."""
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


def test_timeout_reaps_child_that_holds_stdout(tmp_path):
    """Regression: GUI/server children inherit stdout=PIPE; killing only cmd.exe
    left the child alive and communicate() blocked forever."""
    pidfile = tmp_path / "child.pid"
    cmd = (
        f"{sys.executable} -c "
        f"\"import os,time,pathlib; pathlib.Path(r'{pidfile}').write_text(str(os.getpid())); "
        f"time.sleep(120)\""
    )
    t0 = time.monotonic()
    results = run_check_commands([cmd], tmp_path, timeout=2)
    elapsed = time.monotonic() - t0
    assert elapsed < 20, f"timed-out check hung for {elapsed:.1f}s"
    assert results[0]["exit_code"] == 124
    deadline = time.monotonic() + 5
    child_pid = None
    while time.monotonic() < deadline:
        if pidfile.exists():
            child_pid = int(pidfile.read_text(encoding="utf-8").strip() or "0")
            if child_pid and not _pid_alive(child_pid):
                break
        time.sleep(0.1)
    assert child_pid, "child never wrote its pid"
    assert not _pid_alive(child_pid), f"child pid {child_pid} still alive after timeout"


def test_check_skips_launch_when_already_aborted(tmp_path):
    """中止必须在启动检查命令之前生效，否则侧会卡在 600s 的 wait 上。"""
    cmd = f'{sys.executable} -c "import time; time.sleep(120)"'
    t0 = time.monotonic()
    results = run_check_commands(
        [cmd], tmp_path, timeout=30, should_abort=lambda: True,
    )
    assert time.monotonic() - t0 < 2
    assert results[0]["aborted"] is True
    assert results[0]["exit_code"] is None


def test_check_aborts_in_flight(tmp_path):
    """运行中的检查命令必须响应中止，不能等到 timeout 才结束。"""
    abort = {"hit": False}
    marker = tmp_path / "started.txt"
    cmd = (
        f'{sys.executable} -c '
        f'"import time,pathlib; pathlib.Path(r\'{marker}\').write_text(\'1\'); time.sleep(120)"'
    )
    t0 = time.monotonic()

    def should_abort():
        return abort["hit"] or marker.exists()

    # Flip as soon as the child has started (marker exists); the helper above
    # also treats "started" as abort so we don't need a second thread.
    results = run_check_commands(
        [cmd], tmp_path, timeout=30, should_abort=should_abort,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 20, f"in-flight abort hung for {elapsed:.1f}s"
    assert results[0]["aborted"] is True


def test_feed_stdin_timeout_terminates_instead_of_closing_wrapper():
    """Caller must not stdin.close() while the writer holds the IO lock."""
    from agent_trace_kit.runner import _feed_stdin

    class BlockingStdin:
        def __init__(self):
            self.closed_by = []
            self.gate = threading.Event()

        def write(self, _data):
            self.gate.wait(10)

        def flush(self):
            pass

        def close(self):
            self.closed_by.append(threading.current_thread().name)

        def fileno(self):
            raise AssertionError("must not fileno/close from the attempt thread")

    class Proc:
        def __init__(self):
            self.stdin = BlockingStdin()
            self.terminated = False

        def terminate(self):
            self.terminated = True
            self.stdin.gate.set()

    proc = Proc()
    t0 = time.monotonic()
    _feed_stdin(proc, "x" * 80, timeout=0.3)
    assert time.monotonic() - t0 < 3
    assert proc.terminated is True
    assert "atk-stdin" in proc.stdin.closed_by
