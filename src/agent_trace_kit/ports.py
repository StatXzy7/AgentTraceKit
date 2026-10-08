"""Loopback port occupancy helpers for GSB product verification.

Windows machines commonly already have something on :8080 (Steam's CEF remote
debugger is the one that burned three GSB reviews: the page title is
"Inspectable WebContents" and has Steam links). Opening that address after
EADDRINUSE is not a product failure.

The policy here follows three well-known open-source patterns:

- Vite / webpack-dev-server ``detect-port``: if the advertised port is taken,
  increment; never pretend the occupant is "our" server.
- Playwright ``webServer.reuseExistingServer: false``: a health-check GET must
  succeed on a port *we* just bound, not on whatever was already listening.
- Windows ``SO_EXCLUSIVEADDRUSE`` (MSDN): ``SO_REUSEADDR`` on Win32 lets two
  processes bind the same port, which is how a product and Steam can appear to
  share 8080. Exclusive bind refuses that.
"""
from __future__ import annotations

import functools
import http.server
import json
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


COMMON_DEV_PORTS = (8080, 8000, 3000, 5173, 4173, 5000, 5500, 8888, 9000, 4200, 8081)

# Occupants that are never the coding-agent product. Steam's CEF debugger is
# the one GSB reviewers kept opening as if it were the task web app.
FOREIGN_PROCESS_NAMES = {
    "steam.exe", "steamwebhelper.exe", "gameoverlayui.exe", "steamservice.exe",
    "discord.exe", "spotify.exe", "slack.exe", "telegram.exe",
}

PRODUCT_PROCESS_NAMES = {
    "node.exe", "node", "python.exe", "python", "pythonw.exe", "py.exe",
    "php.exe", "ruby.exe", "java.exe", "deno.exe", "bun.exe", "npm.cmd",
}

# Body/title fingerprints of Chromium Embedded Framework remote-debugging pages
# (Steam, some Electron apps, Edge/Chrome inspect landing pages).
CEF_MARKERS = (
    "inspectable webcontents",
    "chrome-devtools-frontend",
    "chrome-devtools://",
    "remote debugging",
    "cef-debug",
    "steamcommunity.com",
    "store.steampowered.com",
    "steam://",
)

_PORT_IN_URL = re.compile(
    r"(?:127\.0\.0\.1|localhost|0\.0\.0\.0):(\d{2,5})\b", re.I)
_HTTP_SERVER_PORT = re.compile(r"http\.server\s+(\d{2,5})\b")
_FLAG_PORT = re.compile(r"(?:--port|-p)(?:\s+|=)(\d{2,5})\b")
_ENV_PORT = re.compile(r"\bPORT\s*=\s*(\d{2,5})\b")
_START_LINE = re.compile(
    r"^\s*(?:\$\s*)?(python(?:3|\.exe)?\s+-m\s+http\.server\b.*|"
    r"npx\s+serve\b.*|npm\s+(?:start|run\s+dev)\b.*|"
    r"node\s+\S+.*|py(?:thon)?\s+\S+.*server.*)$",
    re.I | re.M,
)


def _ntohs_port(raw: int) -> int:
    """TCP table ports are stored in network byte order (DWORD, high 16 unused)."""
    return socket.ntohs(raw & 0xFFFF)


def _pid_name(pid: int) -> str:
    if pid <= 0:
        return ""
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        ok = k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        if not ok:
            return ""
        return Path(buf.value).name.lower()
    finally:
        k32.CloseHandle(handle)


def _tcp_table_windows(family: int) -> list[dict]:
    """MIB_TCP[6]TABLE_OWNER_PID via GetExtendedTcpTable."""
    import ctypes
    from ctypes import wintypes

    TCP_TABLE_OWNER_PID_LISTENER = 3
    AF_INET6 = 23

    class _Row4(ctypes.Structure):
        _fields_ = [
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    class _Row6(ctypes.Structure):
        _fields_ = [
            ("ucLocalAddr", ctypes.c_ubyte * 16),
            ("dwLocalScopeId", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("ucRemoteAddr", ctypes.c_ubyte * 16),
            ("dwRemoteScopeId", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwState", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    row_cls = _Row6 if family == AF_INET6 else _Row4
    iphlpapi = ctypes.WinDLL("iphlpapi.dll", use_last_error=True)
    iphlpapi.GetExtendedTcpTable.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        wintypes.ULONG, wintypes.DWORD, wintypes.DWORD]
    iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD

    size = wintypes.DWORD(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), True, family,
                                 TCP_TABLE_OWNER_PID_LISTENER, 0)
    buf = ctypes.create_string_buffer(size.value)
    err = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), True, family,
                                       TCP_TABLE_OWNER_PID_LISTENER, 0)
    if err:
        raise OSError(err, "GetExtendedTcpTable failed")

    count = ctypes.c_ulong.from_buffer_copy(buf, 0).value
    offset = ctypes.sizeof(ctypes.c_ulong)
    rows = []
    for i in range(count):
        row = row_cls.from_buffer_copy(buf, offset + i * ctypes.sizeof(row_cls))
        if family == AF_INET6:
            addr = socket.inet_ntop(socket.AF_INET6, bytes(row.ucLocalAddr))
        else:
            addr = socket.inet_ntoa(row.dwLocalAddr.to_bytes(4, "little"))
        port = _ntohs_port(row.dwLocalPort)
        pid = int(row.dwOwningPid)
        rows.append({
            "addr": addr, "port": port, "pid": pid,
            "name": _pid_name(pid),
        })
    return rows


def _list_listeners_windows() -> list[dict]:
    """IPv4 + IPv6 listeners via GetExtendedTcpTable (no netstat parsing)."""
    rows = _tcp_table_windows(socket.AF_INET)
    try:
        rows.extend(_tcp_table_windows(socket.AF_INET6))
    except OSError:
        pass
    return rows


def parse_netstat_windows(text: str) -> list[dict]:
    """Parse ``netstat -ano -p TCP`` LISTENING rows (fallback / tests)."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() not in ("LISTENING", "LISTEN"):
            continue
        local = parts[1]
        if ":" not in local:
            continue
        host, _, port_s = local.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_s)
            pid = int(parts[-1])
        except ValueError:
            continue
        rows.append({"addr": host, "port": port, "pid": pid, "name": ""})
    return rows


def list_listeners() -> list[dict]:
    """Return TCP listeners. Best-effort; empty list when the OS call fails."""
    groups: list[list[dict]] = []
    try:
        if sys.platform == "win32":
            try:
                groups.append(_list_listeners_windows())
            except OSError:
                pass
            # Keep merging hidden netstat so IPv6-only / dual-stack listeners
            # that the IPv4 table misses still show up. CREATE_NO_WINDOW stops
            # the console flash operators called "PowerShell".
            try:
                from .procmon import run_hidden
                p = run_hidden(
                    ["netstat", "-ano"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=15,
                )
                extra = parse_netstat_windows(p.stdout)
                for row in extra:
                    if not row.get("name"):
                        row["name"] = _pid_name(int(row["pid"]))
                groups.append(extra)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return _merge_listeners(*groups)
        from .procmon import run_hidden
        p = run_hidden(
            ["ss", "-lntpH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
        if p.returncode == 0 and p.stdout:
            groups.append(_parse_ss(p.stdout))
        return _merge_listeners(*groups)
    except (OSError, subprocess.TimeoutExpired):
        return _merge_listeners(*groups)


def _merge_listeners(*groups: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for rows in groups:
        for row in rows:
            key = (str(row.get("addr") or ""), int(row.get("port") or 0), int(row.get("pid") or 0))
            if key in seen or key[1] <= 0:
                continue
            seen.add(key)
            out.append(row)
    return out


def _parse_ss(text: str) -> list[dict]:
    """Parse ``ss -lntH`` rows: ``LISTEN 0 4096 127.0.0.1:8080 0.0.0.0:*``."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        if ":" not in local:
            continue
        host, _, port_s = local.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_s)
        except ValueError:
            continue
        owner = re.search(r'\("([^"]+)",pid=(\d+)', line)
        rows.append({"addr": host or "0.0.0.0", "port": port,
                     "pid": int(owner.group(2)) if owner else 0,
                     "name": owner.group(1) if owner else ""})
    return rows


def _exclusive_socket(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Win32: SO_REUSEADDR (Python HTTPServer default) lets two binds succeed on
    # the same port. Exclusive-address-use is the documented way to refuse that.
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:
        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    sock.bind((host, port))
    return sock


def claim_port(preferred: int | None = None, host: str = "127.0.0.1",
               *, skip: set[int] | None = None, tries: int = 80) -> tuple[socket.socket, int]:
    """Bind an exclusive socket. Caller owns it until close.

    Vite detect-port: try preferred, then +1, skipping occupied/reserved
    ports. Holding the socket closes the bind-then-rebind race that let
    Steam's SO_REUSEADDR grab 8080 between probe and listen.
    """
    blocked = {p for p in (skip or set()) if isinstance(p, int) and p > 0}
    if preferred and 0 < preferred < 65536:
        for port in range(preferred, min(65536, preferred + tries)):
            if port in blocked:
                continue
            try:
                return _exclusive_socket(host, port), port
            except OSError:
                continue
    sock = _exclusive_socket(host, 0)
    port = int(sock.getsockname()[1])
    if port in blocked:
        sock.close()
        raise RuntimeError("操作系统分配的临时端口落在保留集合里，请重试")
    return sock, port


def port_available(port: int, host: str = "127.0.0.1") -> bool:
    try:
        sock = _exclusive_socket(host, port)
    except OSError:
        return False
    sock.close()
    return True


def find_free_port(preferred: int | None = None, host: str = "127.0.0.1",
                   *, skip: set[int] | None = None, tries: int = 80) -> int:
    """Suggest a free port (socket is released). Preview uses ``claim_port``."""
    sock, port = claim_port(preferred, host, skip=skip, tries=tries)
    sock.close()
    return port


def classify_process_name(name: str) -> str:
    n = (name or "").lower()
    base = Path(n).name
    if base in FOREIGN_PROCESS_NAMES:
        return "foreign"
    if base in PRODUCT_PROCESS_NAMES:
        return "product"
    return "other"


def classify_http_body(body: str, *, headers: dict | None = None) -> str:
    """Classify a GET response: cef_debugger / product / empty / unknown."""
    text = body or ""
    blob = text.lower()
    if headers:
        blob += "\n" + "\n".join(f"{k}:{v}" for k, v in headers.items()).lower()
    if any(marker in blob for marker in CEF_MARKERS):
        return "cef_debugger"
    stripped = text.strip()
    if not stripped:
        return "empty"
    if "<html" in blob or "<!doctype" in blob or "<body" in blob:
        return "product"
    return "unknown"


def assert_loopback_http_url(url: str) -> urllib.parse.SplitResult:
    """Refuse userinfo / non-loopback / missing port (SSRF guard)."""
    if not isinstance(url, str) or not url.strip():
        raise RuntimeError("缺少探测地址")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme != "http":
        raise RuntimeError("只允许 http://127.0.0.1:端口/")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("探测地址不能带用户名")
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("只允许探测本机 loopback 地址（http://127.0.0.1:端口/）")
    if parsed.port is None or not (1 <= parsed.port <= 65535):
        raise RuntimeError("探测地址必须带 1–65535 的端口")
    return parsed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ARG002
        return None


def _probe_opener() -> urllib.request.OpenerDirector:
    # No system/HTTP proxy, and never follow a 302 off box.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def probe_http(url: str, timeout: float = 1.2) -> dict:
    """GET ``url`` and classify it. Never follows into a foreign occupant silently."""
    result = {"url": url, "ok": False, "status": 0, "kind": "unreachable",
              "title": "", "snippet": "", "error": ""}
    try:
        assert_loopback_http_url(url)
    except RuntimeError as exc:
        result["error"] = str(exc)
        return result
    try:
        req = urllib.request.Request(url, method="GET", headers={
            "User-Agent": "AgentTraceKit-port-probe/1",
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.1",
        })
        with _probe_opener().open(req, timeout=timeout) as resp:
            raw = resp.read(65536)
            status = getattr(resp, "status", 200) or 200
            ctype = resp.headers.get("Content-Type", "")
            headers = {"content-type": ctype}
    except urllib.error.HTTPError as exc:
        raw = exc.read(65536) if exc.fp else b""
        status = exc.code
        headers = {"content-type": exc.headers.get("Content-Type", "") if exc.headers else ""}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        result["error"] = str(exc)
        return result
    try:
        body = raw.decode("utf-8", errors="replace")
    except Exception:
        body = ""
    kind = classify_http_body(body, headers=headers)
    title_m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
    result.update({
        "ok": 200 <= status < 300 and kind != "cef_debugger",
        "status": status, "kind": kind, "title": title[:200],
        "snippet": re.sub(r"\s+", " ", body)[:240],
    })
    return result


def extract_ports_from_text(text: str) -> list[int]:
    """Advertised localhost ports from README/package scripts. Deduped, stable."""
    found: list[int] = []
    seen: set[int] = set()
    for rx in (_PORT_IN_URL, _HTTP_SERVER_PORT, _FLAG_PORT, _ENV_PORT):
        for m in rx.finditer(text or ""):
            port = int(m.group(1))
            if 1 <= port <= 65535 and port not in seen:
                # Years and HTTP status codes show up in docs; keep typical
                # bind ports and anything next to a listen/server token.
                if port in COMMON_DEV_PORTS or 1024 <= port <= 9999:
                    seen.add(port)
                    found.append(port)
    return found


def extract_start_commands(text: str) -> list[str]:
    cmds: list[str] = []
    seen: set[str] = set()
    for m in _START_LINE.finditer(text or ""):
        cmd = re.sub(r"^\s*\$\s*", "", m.group(0)).strip()
        if cmd and cmd.lower() not in seen:
            seen.add(cmd.lower())
            cmds.append(cmd)
    return cmds


def rewrite_start_command(command: str, new_port: int) -> str:
    """Replace an advertised bind port so the operator can paste a free one."""
    cmd = command.strip()
    if not cmd:
        return f"python -m http.server {new_port} --bind 127.0.0.1"

    def _sub_port(match: re.Match) -> str:
        prefix = match.group(0)[: -len(match.group(1))]
        return f"{prefix}{new_port}"

    replaced = _HTTP_SERVER_PORT.sub(_sub_port, cmd, count=1)
    if replaced != cmd:
        if "--bind" not in replaced:
            replaced += " --bind 127.0.0.1"
        return replaced
    replaced = _FLAG_PORT.sub(_sub_port, cmd, count=1)
    if replaced != cmd:
        return replaced
    replaced = _PORT_IN_URL.sub(lambda m: m.group(0).rsplit(":", 1)[0] + f":{new_port}", cmd, count=1)
    if replaced != cmd:
        return replaced
    if re.search(r"http\.server\b", cmd) and not re.search(r"\b\d{2,5}\b", cmd):
        return f"{cmd} {new_port} --bind 127.0.0.1"
    if "PORT=" in cmd.upper():
        return re.sub(r"PORT\s*=\s*\d+", f"PORT={new_port}", cmd, count=1, flags=re.I)
    if re.match(r"(npm|npx|node)\b", cmd, re.I):
        return f"set PORT={new_port}&& {cmd}" if sys.platform == "win32" else f"PORT={new_port} {cmd}"
    return f"{cmd}  (请把端口改成 {new_port}，不要复用已被占用的端口)"


def workspace_uses_es_module(workspace: str | Path) -> bool:
    """``file://`` cannot load ``<script type=module>``; GSB hit this too."""
    root = Path(workspace)
    for name in ("index.html", "public/index.html", "src/index.html"):
        p = root / name
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"<script[^>]+type\s*=\s*['\"]module['\"]", text, re.I):
                return True
    return False


def parse_workspace_hints(workspace: str | Path) -> dict:
    root = Path(workspace)
    texts: list[str] = []
    for rel in ("README.md", "README.txt", "readme.md", "package.json"):
        p = root / rel
        if not p.is_file():
            continue
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if p.name == "package.json":
            try:
                pkg = json.loads(raw)
                scripts = pkg.get("scripts") or {}
                raw = "\n".join(f"{k} {v}" for k, v in scripts.items() if isinstance(v, str))
            except json.JSONDecodeError:
                pass
        texts.append(raw)
    blob = "\n".join(texts)
    ports = extract_ports_from_text(blob)
    commands = extract_start_commands(blob)
    if not commands:
        if (root / "index.html").is_file() or (root / "public" / "index.html").is_file():
            port = ports[0] if ports else 8080
            commands = [f"python -m http.server {port} --bind 127.0.0.1"]
    return {
        "workspace": str(root),
        "advertised_ports": ports,
        "start_commands": commands,
        "has_es_module": workspace_uses_es_module(root),
        "has_index_html": (root / "index.html").is_file()
        or (root / "public" / "index.html").is_file(),
    }


def _listener_warning(row: dict, probe: dict | None, desk_port: int | None,
                      owned_ports: set[int] | None = None,
                      owned_pids: set[int] | None = None) -> dict | None:
    port = int(row["port"])
    name = (row.get("name") or "").lower()
    kind_proc = classify_process_name(name)
    pid = int(row.get("pid") or 0)
    owned_ports = owned_ports or set()
    owned_pids = owned_pids or set()
    addr = row.get("addr") or "127.0.0.1"
    if addr in ("0.0.0.0", "::", "::1"):
        addr = "127.0.0.1"
    url = f"http://{addr}:{port}/"
    if desk_port and port == desk_port:
        return None
    if port in owned_ports or (pid and pid in owned_pids):
        return {
            "port": port, "kind": "ours", "process": name or f"pid {pid}",
            "pid": pid, "url": url, "title": (probe or {}).get("title") or "",
            "message": (
                f"{url} 是交付台自己的静态预览（同一 python.exe pid {pid}），"
                "不是 Steam，也不是上一轮残留。验收请用下方 README 命令另开产物端口。"
            ),
        }
    cef = probe and probe.get("kind") == "cef_debugger"
    if kind_proc == "foreign" or cef:
        title = (probe or {}).get("title") or "Inspectable WebContents"
        return {
            "port": port,
            "kind": "foreign_cef" if cef or "steam" in name else "foreign",
            "process": name or f"pid {row.get('pid')}",
            "pid": row.get("pid"),
            "url": url,
            "title": title,
            "message": (
                f"{url} 被 {name or '未知进程'} 占用"
                + (f"（页面标题「{title}」）" if title else "")
                + "。这不是本题产物。终端若出现 EADDRINUSE，请换空闲端口，"
                "禁止打开这个已被占用的地址，也不要用 file:// 打开 index.html。"
            ),
        }
    if port in COMMON_DEV_PORTS and kind_proc == "product":
        return {
            "port": port,
            "kind": "product_leftover",
            "process": name,
            "pid": row.get("pid"),
            "url": url,
            "title": (probe or {}).get("title") or "",
            "message": (
                f"{url} 已被 {name} (pid {row.get('pid')}) 监听。"
                "不要复用这个已有服务（Playwright reuseExistingServer=false）："
                "它可能是上一轮残留，也可能不是本题工作区。请换空闲端口重新启动。"
            ),
        }
    if port in COMMON_DEV_PORTS:
        return {
            "port": port,
            "kind": "occupied",
            "process": name or f"pid {row.get('pid')}",
            "pid": row.get("pid"),
            "url": url,
            "title": (probe or {}).get("title") or "",
            "message": (
                f"{url} 已被 {name or '其他进程'} 占用。"
                "验收时请按 README 改用空闲端口，不要打开被占用地址。"
            ),
        }
    return None


def gsb_scan(*, workspaces: dict[str, str] | None = None, desk_port: int | None = None,
             listeners: list[dict] | None = None, probe_fn=None,
             extra_skip: set[int] | None = None,
             owned_ports: set[int] | None = None,
             owned_pids: set[int] | None = None) -> dict:
    """Build the GSB port-assist report for the desk UI.

    ``listeners`` / ``probe_fn`` are seams for tests; production uses the OS
    table and a real HTTP GET of occupied common ports.
    """
    rows = list(listeners) if listeners is not None else list_listeners()
    probe = probe_fn or probe_http
    warnings: list[dict] = []
    probed: dict[int, dict] = {}
    common_occupied = {
        int(r["port"]) for r in rows
        if int(r.get("port") or 0) in COMMON_DEV_PORTS
        and (r.get("addr") in ("127.0.0.1", "0.0.0.0", "::", "::1", "localhost")
             or not r.get("addr"))
    }
    for port in sorted(common_occupied):
        try:
            probed[port] = probe(f"http://127.0.0.1:{port}/")
        except Exception as exc:
            probed[port] = {"ok": False, "kind": "unreachable", "error": str(exc)}
    seen_ports: set[int] = set()
    for row in rows:
        port = int(row.get("port") or 0)
        if port in seen_ports:
            continue
        warn = _listener_warning(
            row, probed.get(port), desk_port,
            owned_ports=owned_ports, owned_pids=owned_pids)
        if warn:
            seen_ports.add(port)
            warnings.append(warn)

    # Always avoid 8080/5173/… even when the TCP table looks empty: a just-stopped
    # desk preview or Steam CEF will sit there again, and GSB must not open it.
    skip = set(common_occupied) | set(COMMON_DEV_PORTS)
    if desk_port:
        skip.add(int(desk_port))
    if extra_skip:
        skip |= {int(p) for p in extra_skip if p}
    advertised: list[int] = []
    sides: dict[str, dict] = {}
    for side, path in (workspaces or {}).items():
        hints = parse_workspace_hints(path) if path and Path(path).is_dir() else {
            "workspace": path or "", "advertised_ports": [], "start_commands": [],
            "has_es_module": False, "has_index_html": False,
        }
        advertised.extend(hints.get("advertised_ports") or [])
        preferred = (hints.get("advertised_ports") or [None])[0]
        try:
            free = find_free_port(preferred, skip=skip)
        except RuntimeError:
            free = find_free_port(skip=skip)
        skip.add(free)
        rewritten = [rewrite_start_command(c, free) for c in (hints.get("start_commands") or [])]
        if not rewritten and hints.get("has_index_html"):
            rewritten = [f"python -m http.server {free} --bind 127.0.0.1"]
        extra = []
        if hints.get("has_es_module"):
            extra.append("该产物使用 ES Module，禁止用 file:// 打开 index.html，必须走本地 HTTP。")
        if preferred and preferred in COMMON_DEV_PORTS:
            extra.append(
                f"README 写的端口 {preferred} 是常见占用位（Steam / 上一题残留），"
                f"已改写为 {free}。不要打开 http://127.0.0.1:{preferred}/。"
            )
        elif preferred and preferred in common_occupied:
            extra.append(
                f"README 写的端口 {preferred} 此刻不可用，已改写为 {free}。"
            )
        sides[side] = {
            **hints,
            "free_port": free,
            "rewritten_commands": rewritten,
            "open_url": f"http://127.0.0.1:{free}/",
            "notes": extra,
        }
    try:
        global_free = find_free_port(advertised[0] if advertised else 8080, skip=skip)
    except RuntimeError:
        global_free = find_free_port(skip=skip)
    return {
        "listeners": [
            {**r, "class": classify_process_name(r.get("name") or "")}
            for r in rows if int(r.get("port") or 0) in COMMON_DEV_PORTS
            or int(r.get("port") or 0) in set(advertised)
        ],
        "warnings": warnings,
        "probes": probed,
        "free_port": global_free,
        "sides": sides,
    }


class _ExclusiveStaticServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class _QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass


def _stop_httpd(httpd, thread=None, timeout: float = 2.0) -> None:
    """Never call ``HTTPServer.shutdown()`` on the serving thread, and never
    wait forever if ``serve_forever`` has not entered its loop yet."""
    if httpd is None:
        return

    def _shut():
        try:
            httpd.shutdown()
        except Exception:
            pass

    stopper = threading.Thread(target=_shut, daemon=True)
    stopper.start()
    stopper.join(timeout)
    if thread is not None:
        thread.join(timeout)
    try:
        httpd.server_close()
    except OSError:
        pass


def start_static_preview(workspace: str | Path, preferred: int | None = None,
                         *, skip: set[int] | None = None, host: str = "127.0.0.1") -> dict:
    """Serve ``workspace`` on a freshly bound exclusive port. Never reuse.

    Playwright-style: claim the port (keep the socket), listen, then health-check
    *our* URL. If the GET classifies as CEF we abort.
    """
    root = Path(workspace)
    if not root.is_dir():
        raise RuntimeError(f"工作区不存在: {workspace}")
    docroot = root / "public" if (root / "public" / "index.html").is_file() and not (root / "index.html").is_file() else root
    sock, port = claim_port(preferred, host, skip=skip)
    handler = functools.partial(_QuietStaticHandler, directory=str(docroot))
    httpd = _ExclusiveStaticServer((host, port), handler, bind_and_activate=False)
    try:
        try:
            httpd.socket.close()
        except OSError:
            pass
        httpd.socket = sock
        httpd.server_address = sock.getsockname()
        httpd.server_activate()
    except OSError as exc:
        try:
            sock.close()
        except OSError:
            pass
        raise RuntimeError(f"无法在 {host}:{port} 上启动预览（{exc}）；请换端口") from exc
    started = threading.Event()

    def run() -> None:
        started.set()
        httpd.serve_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    if not started.wait(2):
        _stop_httpd(httpd, thread)
        raise RuntimeError("预览线程未能启动")
    bound = httpd.server_address[1]
    url = f"http://{host}:{bound}/"
    probed = {"ok": False, "kind": "unreachable", "error": "timeout", "status": 0, "title": ""}
    deadline = time.time() + 2.5
    while time.time() < deadline:
        probed = probe_http(url, timeout=0.6)
        if probed.get("kind") != "unreachable":
            break
        time.sleep(0.05)
    if probed.get("kind") == "cef_debugger":
        _stop_httpd(httpd, thread)
        raise RuntimeError(
            f"{url} 响应的是 CEF/Steam 调试页，说明端口仍被他人占用；已停止预览，请再扫一次端口"
        )
    if not probed.get("ok") and probed.get("kind") == "unreachable":
        _stop_httpd(httpd, thread)
        raise RuntimeError(f"预览已绑定 {url} 但健康检查失败: {probed.get('error')}")
    return {
        "url": url, "port": bound, "workspace": str(root),
        "httpd": httpd, "thread": thread, "probe": probed,
    }


def reap_leftover_listeners(*, keep_pids: set[int] | None = None) -> dict:
    """Kill leftover product servers on common dev ports (not the desk itself).

    Used when GSB switches tasks and a previous ``python -m http.server 8080``
    or in-process preview is still answering with the old page.
    Never touches Steam/CEF (foreign) or ``keep_pids`` (the desk process).
    """
    keep = {int(p) for p in (keep_pids or set()) if p}
    killed: list[dict] = []
    skipped: list[dict] = []
    seen_pids: set[int] = set()
    for row in list_listeners():
        port = int(row.get("port") or 0)
        pid = int(row.get("pid") or 0)
        if port not in COMMON_DEV_PORTS or pid <= 0 or pid in seen_pids:
            continue
        name = (row.get("name") or "").lower()
        if pid in keep:
            skipped.append({"port": port, "pid": pid, "name": name, "reason": "desk"})
            continue
        if classify_process_name(name) != "product":
            skipped.append({"port": port, "pid": pid, "name": name, "reason": "foreign"})
            continue
        seen_pids.add(pid)
        if sys.platform != "win32":
            # Port number/process name alone is not proof of task ownership.
            # Known previews stop through PreviewRegistry; CLI groups through the runner.
            skipped.append({"port": port, "pid": pid, "name": name, "reason": "use-task-abort-or-SSH"})
            continue
        try:
            from .procmon import run_hidden
            run_hidden(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            )
            killed.append({"port": port, "pid": pid, "name": name})
        except (OSError, subprocess.TimeoutExpired) as exc:
            skipped.append({"port": port, "pid": pid, "name": name, "reason": str(exc)})
    return {"killed": killed, "skipped": skipped}


class PreviewRegistry:
    """One static preview per job/side; closed when the desk exits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}
        self._key_locks: dict[str, threading.Lock] = {}

    @staticmethod
    def _key(job_id: str, side: str) -> str:
        return f"{job_id}/{side}"

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def start(self, job_id: str, side: str, workspace: str,
              preferred: int | None = None, skip: set[int] | None = None) -> dict:
        # A leftover preview for the previous pair (e.g. LP lab on :8080)
        # must not outlive the switch to a new task.
        self.stop_all()
        key = self._key(job_id, side)
        with self._key_lock(key):
            info = start_static_preview(workspace, preferred, skip=skip)
            with self._lock:
                self._items[key] = info
            return self._public(key, info)

    def stop(self, key: str) -> dict:
        with self._lock:
            info = self._items.pop(key, None)
        if not info:
            return {"stopped": False}
        _stop_httpd(info.get("httpd"), info.get("thread"))
        return {"stopped": True, "url": info.get("url", "")}

    def stop_side(self, job_id: str, side: str) -> dict:
        return self.stop(self._key(job_id, side))

    def status(self, job_id: str, side: str) -> dict:
        key = self._key(job_id, side)
        with self._lock:
            info = self._items.get(key)
        if not info:
            return {"running": False}
        return self._public(key, info)

    def stop_all(self) -> None:
        with self._lock:
            keys = list(self._items)
        for key in keys:
            self.stop(key)

    def list_all(self) -> list[dict]:
        with self._lock:
            return [self._public(key, info) for key, info in self._items.items()]

    def stop_others(self, job_id: str) -> list[str]:
        """Close in-process previews that belong to a different pair.

        Desk preview is the same python.exe as :8765, so taskkill cannot
        reap it. Switching GSB tasks must drop the previous lab's httpd.
        """
        prefix = f"{job_id}/"
        with self._lock:
            keys = [k for k in self._items if not k.startswith(prefix)]
        stopped = []
        for key in keys:
            if self.stop(key).get("stopped"):
                stopped.append(key)
        return stopped

    @staticmethod
    def _public(key: str, info: dict) -> dict:
        return {
            "running": True, "key": key, "url": info["url"], "port": info["port"],
            "workspace": info.get("workspace", ""),
            "probe": {k: info.get("probe", {}).get(k) for k in ("ok", "kind", "status", "title")},
        }
