"""Real Linux process lifecycle checks; never signal an unrelated process."""
import os
import subprocess
import sys
import time
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from agent_trace_kit import ports, procmon
from agent_trace_kit.desk_store import DeskStore
from agent_trace_kit.desk import _ExclusiveServer


def test_ss_owns_ipv4_and_ipv6_listener():
    rows = ports._parse_ss('LISTEN 0 128 127.0.0.1:8765 0.0.0.0:* users:(("python",pid=123,fd=7))\n'
                           'LISTEN 0 128 [::1]:9000 [::]:*\n')
    assert rows[0] == {"addr": "127.0.0.1", "port": 8765, "pid": 123, "name": "python"}
    assert rows[1]["port"] == 9000


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX socket reuse")
def test_immediate_restart_does_not_share_live_listener():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
        def log_message(self, *_):
            pass
    first = _ExclusiveServer(('127.0.0.1', 0), Handler)
    address = first.server_address
    thread = threading.Thread(target=first.handle_request)
    thread.start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{address[1]}', timeout=5):
            pass
        with pytest.raises(OSError):
            _ExclusiveServer(address, Handler)
    finally:
        thread.join(timeout=5)
        first.server_close()
    second = _ExclusiveServer(address, Handler)
    second.server_close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc lifecycle")
def test_linux_snapshot_and_job_os(tmp_path):
    own = procmon.snapshot()[os.getpid()]
    assert own["ppid"] == os.getppid()
    assert own["cpu"] >= 0 and own["start_time"]
    store = DeskStore(tmp_path)
    job = store.create_job({"prompt": "测试 Linux", "baseline_repo": str(tmp_path)})
    assert job["os_name"] == "Linux"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process groups")
@pytest.mark.parametrize("leader_exits", [False, True])
def test_kill_group_reaps_children_and_preserves_neighbor(tmp_path, leader_exits):
    pid_file = tmp_path / "child.pid"
    code = ("import subprocess,sys,time; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
            f"Path({str(pid_file)!r}).write_text(str(p.pid), encoding='utf-8'); "
            + ("" if leader_exits else "time.sleep(120)"))
    neighbor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                **procmon.hidden_console_kwargs())
    leader = subprocess.Popen([sys.executable, "-c", code], **procmon.hidden_console_kwargs())
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child = int(pid_file.read_text(encoding="utf-8"))
        if leader_exits:
            leader.wait(timeout=5)
        procmon.kill_tree(leader.pid)
        leader.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = procmon.snapshot().get(child)
            if not item or item["state"] == "Z":
                break
            time.sleep(0.05)
        else:
            pytest.fail("child survived process-group cleanup")
        assert neighbor.poll() is None
    finally:
        procmon.kill_tree(leader.pid)
        procmon.kill_tree(neighbor.pid)
        leader.wait(timeout=5)
        neighbor.wait(timeout=5)
