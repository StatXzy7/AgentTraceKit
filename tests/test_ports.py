"""Port occupancy helpers: GSB must never treat Steam CEF :8080 as the product."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent_trace_kit import ports as pt


CEF_HTML = """<!doctype html><html><head>
<title>Inspectable WebContents</title></head>
<body><h1>Inspectable WebContents</h1>
<a href="https://store.steampowered.com/">Steam</a>
<script src="chrome-devtools-frontend/inspector.js"></script>
</body></html>"""

PRODUCT_HTML = """<!doctype html><html><head>
<title>Parametric LP Lab</title></head>
<body><h1>二维参数线性规划</h1><canvas id="plot"></canvas>
<script type="module" src="app.js"></script>
</body></html>"""


def test_classify_cef_body_is_not_product():
    assert pt.classify_http_body(CEF_HTML) == "cef_debugger"
    assert pt.classify_http_body(PRODUCT_HTML) == "product"
    assert pt.classify_http_body("") == "empty"
    assert pt.classify_http_body("hello") == "unknown"


def test_list_listeners_merges_hidden_netstat_on_windows(monkeypatch):
    monkeypatch.setattr(pt, "_list_listeners_windows", lambda: [
        {"addr": "127.0.0.1", "port": 8765, "pid": 1, "name": "python.exe"},
    ])
    calls = []

    def fake_hidden(args, **kw):
        calls.append(list(args))
        class P:
            returncode = 0
            stdout = (
                "  Proto  Local Address          Foreign Address        State           PID\n"
                "  TCP    [::1]:3000             [::]:0                 LISTENING       42\n"
            )
        return P()

    import agent_trace_kit.procmon as procmon
    monkeypatch.setattr(procmon, "run_hidden", fake_hidden)
    monkeypatch.setattr(pt.sys, "platform", "win32")
    monkeypatch.setattr(pt, "_pid_name", lambda pid: "node.exe")
    rows = pt.list_listeners()
    assert calls and calls[0][:2] == ["netstat", "-ano"]
    ports = {r["port"] for r in rows}
    assert 8765 in ports
    assert 3000 in ports


def test_parse_netstat_listening_rows():
    sample = """
  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:8080         0.0.0.0:0              LISTENING       4242
  TCP    0.0.0.0:8765           0.0.0.0:0              LISTENING       99
  TCP    127.0.0.1:8080         127.0.0.1:51234        ESTABLISHED     4242
"""
    rows = pt.parse_netstat_windows(sample)
    assert {(r["port"], r["pid"]) for r in rows} == {(8080, 4242), (8765, 99)}


def test_find_free_port_skips_occupied_and_reserved():
    occupied = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    taken = occupied.server_address[1]
    t = threading.Thread(target=occupied.serve_forever, daemon=True)
    t.start()
    try:
        free = pt.find_free_port(taken, skip={taken})
        assert free != taken
        assert pt.port_available(free)
        # preferred is taken and not skipped: still must not return the bound port
        # after skip, Vite-style increment
        other = pt.find_free_port(taken, skip=set())
        # other may equal taken+1 etc; it must be bindable exclusively
        assert pt.port_available(other)
        assert other != taken or not pt.port_available(taken)
        # taken is occupied, so other must differ
        assert other != taken
    finally:
        occupied.shutdown()
        occupied.server_close()


def test_rewrite_http_server_command_injects_free_port_and_bind():
    cmd = pt.rewrite_start_command("python -m http.server 8080", 18765)
    assert "18765" in cmd
    assert "8080" not in cmd
    assert "--bind 127.0.0.1" in cmd


def test_parse_readme_ports_and_es_module(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "# lab\n\nStart with:\n\n    python -m http.server 8080\n\n"
        "Then open http://127.0.0.1:8080/\n",
        encoding="utf-8",
    )
    (tmp_path / "index.html").write_text(PRODUCT_HTML, encoding="utf-8")
    hints = pt.parse_workspace_hints(tmp_path)
    assert 8080 in hints["advertised_ports"]
    assert any("http.server" in c for c in hints["start_commands"])
    assert hints["has_es_module"] is True
    assert hints["has_index_html"] is True


def test_gsb_scan_flags_steam_cef_and_rewrites_port(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "python -m http.server 8080\nOpen http://127.0.0.1:8080/\n", encoding="utf-8")
    (tmp_path / "index.html").write_text(PRODUCT_HTML, encoding="utf-8")
    listeners = [
        {"addr": "127.0.0.1", "port": 8080, "pid": 4242, "name": "steamwebhelper.exe"},
        {"addr": "127.0.0.1", "port": 8765, "pid": 7, "name": "python.exe"},
    ]

    def fake_probe(url: str, timeout: float = 1.2) -> dict:
        if ":8080" in url:
            return {"url": url, "ok": False, "status": 200, "kind": "cef_debugger",
                    "title": "Inspectable WebContents", "snippet": "Steam", "error": ""}
        return {"url": url, "ok": True, "status": 200, "kind": "product",
                "title": "desk", "snippet": "", "error": ""}

    report = pt.gsb_scan(
        workspaces={"A": str(tmp_path)}, desk_port=8765,
        listeners=listeners, probe_fn=fake_probe,
    )
    kinds = {w["kind"] for w in report["warnings"]}
    assert "foreign_cef" in kinds
    assert any("Inspectable WebContents" in w["message"] for w in report["warnings"])
    # desk's own 8765 is not a GSB warning
    assert all(w["port"] != 8765 for w in report["warnings"])
    side = report["sides"]["A"]
    assert side["free_port"] != 8080
    assert side["free_port"] != 8765
    assert any("18765" in c or str(side["free_port"]) in c for c in side["rewritten_commands"])
    assert "file://" in " ".join(side["notes"]) or side["has_es_module"]


def test_static_preview_binds_exclusive_port_and_serves_workspace(tmp_path: Path):
    (tmp_path / "index.html").write_text(PRODUCT_HTML, encoding="utf-8")
    # occupy a preferred port so preview must increment (Vite detect-port)
    blocker = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    preferred = blocker.server_address[1]
    t = threading.Thread(target=blocker.serve_forever, daemon=True)
    t.start()
    preview = None
    try:
        preview = pt.start_static_preview(tmp_path, preferred=preferred)
        assert preview["port"] != preferred
        probed = pt.probe_http(preview["url"])
        assert probed["kind"] == "product"
        assert probed["ok"] is True
        assert "Parametric LP Lab" in probed["title"]
    finally:
        blocker.shutdown()
        blocker.server_close()
        if preview and preview.get("httpd"):
            preview["httpd"].shutdown()
            preview["httpd"].server_close()


def test_preview_aborts_if_health_check_is_cef(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text(PRODUCT_HTML, encoding="utf-8")

    def lie_cef(url: str, timeout: float = 2.0) -> dict:
        return {"url": url, "ok": False, "status": 200, "kind": "cef_debugger",
                "title": "Inspectable WebContents", "snippet": "", "error": ""}

    monkeypatch.setattr(pt, "probe_http", lie_cef)
    with pytest.raises(RuntimeError, match="CEF"):
        pt.start_static_preview(tmp_path, preferred=None)


def test_probe_http_against_live_cef_page():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = CEF_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = HTTPServer(("127.0.0.1", 0), H)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        got = pt.probe_http(f"http://127.0.0.1:{port}/")
        assert got["kind"] == "cef_debugger"
        assert got["ok"] is False
        assert "Inspectable" in got["title"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_preview_registry_stop_others_keeps_current_job():
    reg = pt.PreviewRegistry()
    dummy = {"url": "http://127.0.0.1:1/", "port": 1, "workspace": "x",
             "httpd": None, "thread": None, "probe": {}}
    reg._items["pair-old/A"] = {**dummy, "port": 8080}
    reg._items["pair-new/A"] = {**dummy, "port": 8123, "url": "http://127.0.0.1:8123/"}
    assert reg.stop_others("pair-new") == ["pair-old/A"]
    assert reg.status("pair-new", "A")["running"] is True
    assert reg.status("pair-old", "A")["running"] is False


def test_reap_skips_keep_pids_and_foreign_names():
    rows = [
        {"addr": "127.0.0.1", "port": 8080, "pid": 11, "name": "python.exe"},
        {"addr": "127.0.0.1", "port": 5173, "pid": 22, "name": "steamwebhelper.exe"},
        {"addr": "127.0.0.1", "port": 3000, "pid": 33, "name": "python.exe"},
    ]
    killed_pids = []

    def fake_run(args, **kw):
        killed_pids.append(int(args[args.index("/PID") + 1]))
        class P:
            returncode = 0
        return P()

    import agent_trace_kit.ports as ports_mod
    import agent_trace_kit.procmon as procmon
    monkey = pytest.MonkeyPatch()
    monkey.setattr(pt.sys, "platform", "win32")
    monkey.setattr(ports_mod, "list_listeners", lambda: rows)
    monkey.setattr(procmon, "run_hidden", fake_run)
    try:
        report = pt.reap_leftover_listeners(keep_pids={11})
    finally:
        monkey.undo()
    assert {x["pid"] for x in report["killed"]} == {33}
    assert 11 not in {x["pid"] for x in report["killed"]}
    assert any(x["pid"] == 22 and x["reason"] == "foreign" for x in report["skipped"])


def test_gsb_scan_never_recommends_common_ports_even_when_idle(tmp_path: Path):
    """README 写 8080 且此刻没人监听时，也不能推荐 8080：上一题预览刚停、Steam 稍后还会占。"""
    (tmp_path / "README.md").write_text(
        "set PORT=8080&& npm start\n# 默认 http://localhost:8080\n", encoding="utf-8")
    (tmp_path / "index.html").write_text(PRODUCT_HTML, encoding="utf-8")
    report = pt.gsb_scan(
        workspaces={"A": str(tmp_path), "B": str(tmp_path)},
        desk_port=8765, listeners=[],
        probe_fn=lambda url, timeout=1.2: {
            "url": url, "ok": True, "status": 200, "kind": "product",
            "title": "", "snippet": "", "error": ""},
    )
    for side in ("A", "B"):
        port = report["sides"][side]["free_port"]
        assert port not in pt.COMMON_DEV_PORTS
        assert port != 8765
        assert "8080" not in (report["sides"][side]["rewritten_commands"] or [""])[0]
        assert str(port) in (report["sides"][side]["open_url"] or "")
        assert any("8080" in n for n in report["sides"][side]["notes"])


def test_preview_registry_list_all_exposes_job_side():
    reg = pt.PreviewRegistry()
    reg._items["pair-old/B"] = {
        "url": "http://127.0.0.1:8080/", "port": 8080, "workspace": r"C:\old\b",
        "httpd": None, "thread": None, "probe": {"ok": True, "kind": "product"},
    }
    rows = reg.list_all()
    assert rows[0]["key"] == "pair-old/B"
    assert rows[0]["port"] == 8080
    assert rows[0]["running"] is True


def test_ab_sides_get_distinct_free_ports(tmp_path: Path):
    for name in ("a", "b"):
        d = tmp_path / name
        d.mkdir()
        (d / "README.md").write_text("python -m http.server 8080\n", encoding="utf-8")
        (d / "index.html").write_text(PRODUCT_HTML, encoding="utf-8")
    report = pt.gsb_scan(
        workspaces={"A": str(tmp_path / "a"), "B": str(tmp_path / "b")},
        desk_port=8765, listeners=[],
        probe_fn=lambda url, timeout=1.2: {
            "url": url, "ok": True, "status": 200, "kind": "product",
            "title": "", "snippet": "", "error": ""},
    )
    assert report["sides"]["A"]["free_port"] != report["sides"]["B"]["free_port"]


def test_probe_rejects_userinfo_ssrf():
    with pytest.raises(RuntimeError, match="用户名|loopback"):
        pt.assert_loopback_http_url("http://127.0.0.1:8080@example.com/")
    got = pt.probe_http("http://127.0.0.1:8080@example.com/")
    assert got["kind"] == "unreachable"
    assert got["ok"] is False


def test_probe_does_not_follow_redirect_off_box():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://example.com/")
            self.end_headers()

    httpd = HTTPServer(("127.0.0.1", 0), H)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        got = pt.probe_http(f"http://127.0.0.1:{port}/")
        assert got["status"] == 302
        assert got["ok"] is False
        assert "example.com" not in (got.get("snippet") or "").lower()
    finally:
        httpd.shutdown()
        httpd.server_close()
