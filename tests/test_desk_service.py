"""Detached desk service: start is idempotent, stop is explicit, crash restarts."""
from __future__ import annotations

from pathlib import Path

from agent_trace_kit import desk_service as svc


def test_is_running_false_when_nothing_listens(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "probe_desk", lambda *a, **k: False)
    monkeypatch.setattr(svc, "DEFAULT_HOME", tmp_path)
    info = svc.is_running(8765, tmp_path)
    assert info["running"] is False
    assert info["http"] is False


def test_start_desk_skips_spawn_when_http_already_up(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(svc, "probe_desk", lambda *a, **k: True)
    monkeypatch.setattr(svc, "start_supervisor", lambda *a, **k: spawned.append("sup") or 1)
    monkeypatch.setattr(svc, "start_app_window", lambda *a, **k: spawned.append("app") or 2)
    out = svc.start_desk(8765, tmp_path, open_browser=False, open_app=True)
    assert out["already"] is True
    assert "sup" not in spawned
    assert spawned == ["app"]


def test_request_stop_writes_flag(tmp_path):
    path = svc.request_stop(tmp_path)
    assert path.is_file()
    svc.clear_stop(tmp_path)
    assert not path.exists()


def test_supervise_exits_when_stop_file_present(tmp_path):
    svc.request_stop(tmp_path)
    spawned = {"n": 0}

    def spawn(port, paths):
        spawned["n"] += 1
        raise AssertionError("must not spawn after stop")

    assert svc.supervise(8765, tmp_path, spawn=spawn, sleep_fn=lambda *_: None, forever=True) == 0
    assert spawned["n"] == 0


class _FakeProc:
    def __init__(self, pid=99, codes=None):
        self.pid = pid
        self._codes = list(codes or [None, 1])
        self.returncode = None
        self.killed = False

    def poll(self):
        if self._codes:
            self.returncode = self._codes.pop(0)
        return self.returncode

    def terminate(self):
        self.killed = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


def test_supervise_restarts_worker_then_honors_stop(tmp_path):
    procs = [_FakeProc(pid=11, codes=[None, 1]), _FakeProc(pid=22, codes=[None])]
    ticks = {"n": 0}

    def spawn(port, paths):
        return procs.pop(0)

    def sleep(_s):
        ticks["n"] += 1
        if ticks["n"] >= 4:
            svc.request_stop(tmp_path)

    code = svc.supervise(8765, tmp_path, spawn=spawn, sleep_fn=sleep, forever=True)
    assert code == 0
    assert not procs  # both workers used: first crashed, second stopped
    assert (tmp_path / "desk.supervisor.pid").is_file()


def test_supervise_restarts_unresponsive_http_worker(tmp_path):
    first = _FakeProc(pid=11, codes=[None, None, None, None])
    second = _FakeProc(pid=22, codes=[None])
    procs = [first, second]
    spawned = []

    def spawn(port, paths):
        proc = procs.pop(0)
        spawned.append(proc.pid)
        return proc

    probes = {"n": 0}

    def probe():
        probes["n"] += 1
        return probes["n"] == 1

    def sleep(_s):
        if first.killed and len(spawned) >= 2:
            svc.request_stop(tmp_path)

    code = svc.supervise(
        8765, tmp_path, spawn=spawn, sleep_fn=sleep, forever=True,
        probe=probe, health_fails=2,
    )
    assert code == 0
    assert first.killed is True
    assert spawned == [11, 22]


def test_supervise_does_not_reap_before_first_http_ok(tmp_path):
    first = _FakeProc(pid=11, codes=[None] * 8)
    spawned = {"n": 0}
    ticks = {"n": 0}

    def spawn(port, paths):
        spawned["n"] += 1
        return first

    def sleep(_s):
        ticks["n"] += 1
        if ticks["n"] >= 5:
            svc.request_stop(tmp_path)

    code = svc.supervise(
        8765, tmp_path, spawn=spawn, sleep_fn=sleep, forever=True,
        probe=lambda: False, health_fails=2,
    )
    assert code == 0
    assert spawned["n"] == 1


def test_supervise_survives_spawn_error(tmp_path):
    n = {"i": 0}

    def spawn(port, paths):
        n["i"] += 1
        raise OSError("cannot start worker")

    def sleep(_s):
        svc.request_stop(tmp_path)

    assert svc.supervise(8765, tmp_path, spawn=spawn, sleep_fn=sleep, forever=True) == 0
    assert n["i"] == 1


def test_pythonw_prefers_pythonw_next_to_python(tmp_path, monkeypatch):
    py = tmp_path / "python.exe"
    pyw = tmp_path / "pythonw.exe"
    py.write_text("", encoding="utf-8")
    pyw.write_text("", encoding="utf-8")
    monkeypatch.setattr(svc.sys, "executable", str(py))
    assert Path(svc.pythonw_executable()) == pyw


class _PidProc:
    pid = 77


def test_spawn_worker_uses_pythonw(tmp_path, monkeypatch):
    captured = {}

    def fake_popen(args, paths):
        captured["args"] = args
        return _PidProc()

    monkeypatch.setattr(svc, "_popen_detached", fake_popen)
    monkeypatch.setattr(svc, "pythonw_executable", lambda: r"C:\py\pythonw.exe")
    svc._spawn_worker(8765, svc.service_paths(tmp_path))
    assert captured["args"][0].endswith("pythonw.exe")
    assert "--worker" in captured["args"]


def test_start_supervisor_uses_pythonw(tmp_path, monkeypatch):
    captured = {}

    def fake_popen(args, paths):
        captured["args"] = args
        return _PidProc()

    monkeypatch.setattr(svc, "pythonw_executable", lambda: r"C:\py\pythonw.exe")
    svc.start_supervisor(8765, tmp_path, popen=fake_popen)
    assert captured["args"][0].endswith("pythonw.exe")
    assert "--supervise" in captured["args"]
