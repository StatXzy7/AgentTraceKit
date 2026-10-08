"""Per-attempt Responses relay: retry a rejected request without losing the turn.

Only the loopback client holds the random relay token. The provider credential
stays in this process. No prompt, response body or credential is written to logs.
Once any upstream stream bytes are delivered, replay is forbidden.
"""

from __future__ import annotations

import hmac
import base64
import json
import math
import random
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException, HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, unquote
from urllib.request import (
    HTTPRedirectHandler, HTTPHandler, HTTPSHandler, ProxyHandler, Request,
    build_opener, getproxies, proxy_bypass_environment,
)

TRANSIENT = {408, 429, 500, 502, 503, 504}
MAX_BODY = 32 * 1024 * 1024


def retry_delay(attempt: int, retry_after: str = "") -> float:
    """Progressive independent jitter, never earlier than Retry-After."""
    delay = min(600.0, 30.0 * 2 ** min(attempt - 1, 6))
    delay += random.uniform(0, delay / 4)
    try:
        minimum = float(retry_after)
    except ValueError:
        try:
            stamp = parsedate_to_datetime(retry_after)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            minimum = stamp.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            minimum = 0.0
    return max(delay, minimum if math.isfinite(minimum) else 0.0)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _TrackedConnection:
    def __init__(self, *args, relay, **kwargs):
        self.relay = relay
        self.owner_thread = threading.get_ident()
        self.transport = None
        # Bound connecting/handshaking separately from long model generations.
        kwargs["timeout"] = min(15, relay.request_timeout)
        super().__init__(*args, **kwargs)
        with relay._connections_lock:
            relay._connections.add(self)

    def connect(self):
        super().connect()
        self.transport = self.sock
        self.sock.settimeout(self.relay.request_timeout)
        if self.relay.stopped.is_set():
            self.sock.close()
            raise OSError("Relay stopped")

class _HTTPConnection(_TrackedConnection, HTTPConnection):
    pass


class _HTTPSConnection(_TrackedConnection, HTTPSConnection):
    pass


class _HTTPHandler(HTTPHandler):
    def __init__(self, relay):
        super().__init__()
        self.relay = relay

    def http_open(self, req):
        return self.do_open(lambda host, **kw: _HTTPConnection(host, relay=self.relay, **kw), req)


class _HTTPSHandler(HTTPSHandler):
    def __init__(self, relay):
        super().__init__()
        self.relay = relay

    def https_open(self, req):
        return self.do_open(lambda host, **kw: _HTTPSConnection(host, relay=self.relay, **kw),
                            req, context=self._context)


class CodexRelay:
    """A separate threaded listener for each CLI; never serializes other sides."""

    def __init__(
        self, base_url: str, api_key: str, log_path: Path, *,
        proxies: dict[str, str] | None = None, max_attempts: int = 10,
        request_timeout: float = 900, delay=retry_delay, progress_path: Path | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.token = secrets.token_urlsafe(32)
        self.log_path = log_path
        self.progress_path = progress_path
        self.max_attempts = max(1, min(10, max_attempts))
        self.request_timeout = request_timeout
        self.delay = delay
        self.stopped = threading.Event()
        self.exhausted = threading.Event()
        self.permanent_failure = threading.Event()
        self.failure_reason = ""
        self._log_lock = threading.Lock()
        self._connections_lock = threading.Lock()
        self._connections: set = set()
        self._clients: set[socket.socket] = set()
        self._proxies = dict(getproxies() if proxies is None else proxies)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.relay = self
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True,
        )
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("Relay has not started")
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def __exit__(self, *exc):
        self.stopped.set()
        with self._connections_lock:
            sockets = list(self._clients) + [c.transport for c in self._connections if c.transport]
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            # makefile() keeps an IO reference on Windows; close() alone can
            # leave a header read blocked. Detach then close the OS socket so
            # the reader is cancelled and a later file close cannot reuse fd.
            descriptor = sock.detach()
            if descriptor != -1:
                socket.close(descriptor)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def request(self, body: bytes, headers: dict) -> Request:
        req = Request(self.base_url + "/responses", body, headers, method="POST")
        target = urlsplit(req.full_url)
        # set_proxy runs before urllib's request preprocessing here. Pin Host
        # to the origin, otherwise HTTPS CONNECT uses the proxy host as Host.
        req.add_unredirected_header("Host", target.netloc)
        proxy = self._proxies.get(target.scheme) or self._proxies.get("all")
        if proxy and not proxy_bypass_environment(target.netloc, self._proxies):
            parsed = urlsplit(proxy)
            # urllib/http.client does not implement TLS-to-proxy followed by
            # TLS-in-TLS for HTTPS origins. Never silently downgrade that URL.
            if parsed.scheme != "http" or not parsed.hostname:
                raise ValueError("Relay requires an http:// forward proxy")
            req.set_proxy(parsed.netloc.rsplit("@", 1)[-1], parsed.scheme)
            if parsed.username is not None:
                credentials = unquote(parsed.username) + ":" + unquote(parsed.password or "")
                req.add_header("Proxy-Authorization", "Basic " + base64.b64encode(credentials.encode()).decode())
        return req

    def log(self, request_id: str, attempt: int, status: int, **fields) -> None:
        record = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "request": request_id, "attempt": attempt, "http_status": status, **fields,
        }
        with self._log_lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            if (self.progress_path is not None and not self.stopped.is_set()
                    and fields.get("result") != "waiting"):
                with self.progress_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        f"[relay] request={request_id} HTTP {status} "
                        f"attempt={attempt}/{self.max_attempts} {fields.get('result', '')}"
                        + (f" wait={fields['wait_seconds']}s; keeping current session/workspace"
                           if "wait_seconds" in fields else "") + "\n"
                    )


class _Handler(BaseHTTPRequestHandler):
    # Closing the response delimits SSE without buffering a generated answer.
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(30)
        self.streaming = False
        with self.server.relay._connections_lock:
            self.server.relay._clients.add(self.connection)

    def finish(self):
        try:
            super().finish()
        finally:
            with self.server.relay._connections_lock:
                self.server.relay._clients.discard(self.connection)
                connections = [c for c in self.server.relay._connections
                               if c.owner_thread == threading.get_ident()]
            for conn in connections:
                conn.close()
            with self.server.relay._connections_lock:
                self.server.relay._connections.difference_update(connections)

    def _start_stream(self):
        if not self.streaming:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.streaming = True

    def _error(self, status: int, message: str):
        if self.streaming:
            event = {"type": "error", "code": "relay_upstream_error", "message": message}
            self.wfile.write(("event: error\ndata: " + json.dumps(event) + "\n\n").encode())
        else:
            body = json.dumps({"error": {"message": message}}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        self.wfile.flush()

    def _pause(self, delay: float, request_id: str, attempt: int, status: int) -> bool:
        relay = self.server.relay
        self._start_stream()
        deadline = time.monotonic() + delay
        while not relay.stopped.is_set():
            self.wfile.write(b": waiting for upstream retry\n\n")
            self.wfile.flush()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            relay.log(request_id, attempt, status, result="waiting",
                      remaining_seconds=round(remaining, 2))
            relay.stopped.wait(min(5, remaining))
        return False

    def do_POST(self):
        try:
            self._post()
        except (OSError, HTTPException):
            # Client departure ends this handler, with no further upstream call.
            self.close_connection = True

    def _post(self):
        relay = self.server.relay
        if self.path != "/v1/responses":
            return self._error(404, "Unsupported relay endpoint")
        if not hmac.compare_digest(
            self.headers.get("Authorization", "").encode(),
            ("Bearer " + relay.token).encode(),
        ):
            return self._error(401, "Invalid relay token")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                return self._error(413, "Invalid request size")
            body = self.rfile.read(size)
            if len(body) != size or json.loads(body).get("stream") is not True:
                return self._error(400, "Relay requires a streaming Responses request")
        except (ValueError, AttributeError, UnicodeError):
            return self._error(400, "Invalid JSON request")
        headers = {
            "Authorization": "Bearer " + relay.api_key,
            "Content-Type": "application/json", "Accept": "text/event-stream",
            "Accept-Encoding": "identity",
        }
        for name in ("OpenAI-Beta", "User-Agent", "X-Client-Request-Id", "Session_id"):
            if self.headers.get(name):
                headers[name] = self.headers[name]
        opener = build_opener(ProxyHandler({}), _NoRedirect(), _HTTPHandler(relay), _HTTPSHandler(relay))
        request_id = secrets.token_hex(6)
        for attempt in range(1, relay.max_attempts + 1):
            if relay.stopped.is_set():
                return
            status, retry_after, delivered = 502, "", False
            phase, upstream_bytes = "upstream_open", 0
            started = time.monotonic()
            try:
                request = relay.request(body, headers)
                with opener.open(request, timeout=relay.request_timeout) as response:
                    status = response.status
                    if "text/event-stream" not in response.headers.get("Content-Type", ""):
                        relay.permanent_failure.set()
                        relay.log(request_id, attempt, status, result="invalid_content_type")
                        return self._error(502, "Upstream returned a non-SSE response")
                    upstream_id = (response.headers.get("X-Request-Id")
                                   or response.headers.get("X-Tt-Logid") or "")
                    upstream_id = upstream_id.replace(relay.api_key, "[redacted]")[:200]
                    relay.log(request_id, attempt, status, result="streaming",
                              upstream_request_id=upstream_id)
                    phase = "downstream_write"
                    self._start_stream()
                    while not relay.stopped.is_set():
                        phase = "upstream_read"
                        chunk = response.read1(65536)
                        if not chunk:
                            # Codex itself checks for a completed Responses event.
                            relay.log(request_id, attempt, status, result="stream_eof",
                                      upstream_bytes=upstream_bytes,
                                      elapsed_seconds=round(time.monotonic() - started, 3))
                            return
                        upstream_bytes += len(chunk)
                        phase = "downstream_write"
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        delivered = True
                    return
            except HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After", "")
                # Billing/quota failures should not be amplified by retries.
                detail = exc.read(65536).lower()
                exc.close()
                quota = any(x in detail for x in (
                    b"insufficient_quota", b"billing_hard_limit", b"credit balance",
                ))
                if status not in TRANSIENT or quota:
                    relay.failure_reason = f"上游 HTTP {status}，认证/请求/额度错误，停止重试"
                    relay.permanent_failure.set()
                    relay.log(request_id, attempt, status, result="permanent_error")
                    return self._error(status, f"Upstream HTTP {status}; automatic retries stopped")
            except ValueError:
                relay.permanent_failure.set()
                relay.failure_reason = "本机转发器代理配置无效"
                return self._error(502, "Invalid relay proxy configuration")
            except (URLError, OSError, HTTPException) as exc:
                detail = {"phase": phase, "error_type": type(exc).__name__,
                          "upstream_bytes": upstream_bytes,
                          "elapsed_seconds": round(time.monotonic() - started, 3)}
                if phase == "downstream_write":
                    relay.log(request_id, attempt, status, result="client_disconnected_no_replay", **detail)
                    return
                if delivered:
                    relay.log(request_id, attempt, status, result="stream_interrupted_no_replay", **detail)
                    return
                status = 502
            if attempt == relay.max_attempts:
                relay.failure_reason = f"上游 HTTP {status}，同一请求已尝试 {attempt} 次仍未恢复"
                relay.exhausted.set()
                relay.log(request_id, attempt, status, result="exhausted")
                return self._error(status, f"Upstream HTTP {status}; relay exhausted {attempt} attempts")
            wait = relay.delay(attempt, retry_after)
            relay.log(request_id, attempt, status, result="retry_wait", wait_seconds=round(wait, 2))
            if not self._pause(wait, request_id, attempt, status):
                return
