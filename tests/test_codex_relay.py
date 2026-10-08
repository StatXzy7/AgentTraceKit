"""Real loopback HTTP fixtures; no provider traffic or retry sleeps."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from agent_trace_kit.codex_relay import CodexRelay, retry_delay

SUCCESS = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'


@contextmanager
def upstream(responses):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append({
                "body": self.rfile.read(int(self.headers["Content-Length"])),
                "key": self.headers.get("Authorization"), "path": self.path,
            })
            status, body, extra = responses[min(len(calls) - 1, len(responses) - 1)]
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def request(relay, *, token=None, body=None, path="/responses"):
    return build_opener(ProxyHandler({})).open(Request(
        relay.url + path,
        data=body or b'{"model":"fixture","stream":true,"input":"private prompt"}',
        headers={"Authorization": "Bearer " + (token if token is not None else relay.token)},
    ), timeout=5)


def test_retries_same_request_then_streams_without_exposing_provider_key(tmp_path):
    log = tmp_path / "relay.jsonl"
    with upstream([(429, b"rate limited", {"Retry-After": "2"}),
                   (504, b"gateway timeout", {}), (200, SUCCESS, {})]) as (base, calls):
        waits = []
        def delay(attempt, header):
            waits.append((attempt, header))
            return 0
        with CodexRelay(base, "upstream-secret", log, proxies={}, delay=delay) as relay:
            with request(relay) as response:
                data = response.read()
                assert response.status == 200 and SUCCESS in data
            assert not relay.exhausted.is_set()
        assert waits == [(1, "2"), (2, "")]
        assert len(calls) == 3
        assert len({c["body"] for c in calls}) == 1
        assert all(c["key"] == "Bearer upstream-secret" for c in calls)
    text = log.read_text(encoding="utf-8")
    assert "upstream-secret" not in text and "private prompt" not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert [r["http_status"] for r in rows if r["result"] != "stream_eof"] == [429, 504, 200]
    assert rows[-1]["result"] == "stream_eof"
    assert rows[-1]["upstream_bytes"] == len(SUCCESS)


@pytest.mark.parametrize("status,body", [(401, b"secret"), (403, b"secret"),
                                        (400, b"secret"), (429, b"insufficient_quota")])
def test_permanent_errors_do_not_retry_or_echo_body(tmp_path, status, body):
    with upstream([(status, body, {})]) as (base, calls):
        with CodexRelay(base, "key", tmp_path / "log", proxies={}) as relay:
            with pytest.raises(HTTPError) as caught:
                request(relay)
            assert caught.value.code == status
            assert body not in caught.value.read()
            caught.value.close()
        assert len(calls) == 1


def test_exhaustion_is_bounded_and_signalled_after_keepalives(tmp_path):
    with upstream([(429, b"limited", {})]) as (base, calls):
        with CodexRelay(base, "key", tmp_path / "log", proxies={},
                        max_attempts=3, delay=lambda *_: 0) as relay:
            with request(relay) as response:
                assert b"relay exhausted 3 attempts" in response.read()
            assert relay.exhausted.is_set()
        assert len(calls) == 3


def test_partial_stream_never_replayed(tmp_path):
    partial = b'event: response.output_text.delta\ndata: {"delta":"partial"}\n\n'
    with upstream([(200, partial, {"Content-Length": "99999"})]) as (base, calls):
        with CodexRelay(base, "key", tmp_path / "log", proxies={}, delay=lambda *_: 0) as relay:
            with request(relay) as response:
                assert response.read() == partial
        assert len(calls) == 1


def test_broken_upstream_chunk_identifies_transport_phase_and_request_id(tmp_path):
    partial = b'event: response.created\ndata: {"type":"response.created"}\n\n'
    truncated = f'{len(partial):x}\r\n'.encode() + partial + b'\r\n'
    log = tmp_path / 'relay.jsonl'
    with upstream([(200, truncated, {'Transfer-Encoding': 'chunked',
                                     'X-Request-Id': 'provider-request-123'})]) as (base, calls):
        with CodexRelay(base, 'private-key', log, proxies={}) as relay:
            with request(relay) as response:
                assert response.read() == partial
        assert len(calls) == 1
    rows = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    assert rows[0]['upstream_request_id'] == 'provider-request-123'
    failure = rows[-1]
    assert failure['result'] == 'stream_interrupted_no_replay'
    assert failure['phase'] == 'upstream_read'
    assert failure['error_type'] == 'IncompleteRead'
    assert failure['upstream_bytes'] == len(partial)
    assert failure['elapsed_seconds'] >= 0
    assert 'private-key' not in log.read_text(encoding='utf-8')


def test_redirect_and_unauthenticated_requests_cannot_forward_key(tmp_path):
    with upstream([(302, b"redirect", {"Location": "http://127.0.0.1:1/leak"})]) as (base, calls):
        with CodexRelay(base, "key", tmp_path / "log", proxies={}) as relay:
            for token, status in [("bad", 401), (relay.token, 302)]:
                with pytest.raises(HTTPError) as caught:
                    request(relay, token=token)
                assert caught.value.code == status
                caught.value.close()
        assert len(calls) == 1


def test_stop_interrupts_backoff_without_another_request(tmp_path):
    waiting = threading.Event()
    def delay(*_):
        waiting.set()
        return 3600
    with upstream([(429, b"limited", {})]) as (base, calls):
        with CodexRelay(base, "key", tmp_path / "log", proxies={}, delay=delay) as relay:
            with ThreadPoolExecutor() as pool:
                def read():
                    with request(relay) as response:
                        return response.read()
                future = pool.submit(read)
                assert waiting.wait(3)
                relay.stopped.set()
                future.result(timeout=3)
        assert len(calls) == 1


def test_independent_requests_enter_backoff_concurrently(tmp_path):
    barrier = threading.Barrier(8)
    def delay(*_):
        barrier.wait(timeout=5)
        return 0
    with upstream([(429, b"limited", {})]) as (base, calls):
        with CodexRelay(base, "key", tmp_path / "log", proxies={},
                        max_attempts=2, delay=delay) as relay:
            def read():
                with request(relay) as response:
                    return response.read()
            with ThreadPoolExecutor(max_workers=8) as pool:
                outputs = list(pool.map(lambda _: read(), range(8)))
            assert all(b"exhausted 2 attempts" in data for data in outputs)
        assert len(calls) == 16


def test_retry_after_is_lower_bound_and_jitter_is_progressive():
    for attempt in range(1, 11):
        assert retry_delay(attempt, "1800") >= 1800
    assert 30 <= retry_delay(1) <= 37.5
    assert 60 <= retry_delay(2) <= 75
    assert 600 <= retry_delay(10, "invalid") <= 750
    assert retry_delay(1, "NaN") >= 30


@pytest.mark.parametrize("send_headers", [False, True])
def test_stop_closes_active_upstream_before_headers_or_sse_data(tmp_path, send_headers):
    from http.client import HTTPException
    from urllib.error import URLError
    import time
    entered, release, downstream_headers = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(1)
            self.rfile.read(int(self.headers["Content-Length"]))
            if send_headers:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.flush()
            entered.set()
            release.wait(10)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    relay = CodexRelay(f"http://127.0.0.1:{server.server_port}/v1", "key", tmp_path / "log",
                       proxies={}, request_timeout=900)

    def read():
        try:
            with request(relay) as response:
                downstream_headers.set()
                return response.read()
        except (OSError, HTTPException, URLError):
            return b"closed"

    try:
        with ThreadPoolExecutor() as pool:
            relay.__enter__()
            future = pool.submit(read)
            assert entered.wait(3)
            if send_headers:
                assert downstream_headers.wait(3)
            start = time.monotonic()
            relay.__exit__()
            future.result(timeout=2)
            while relay._connections or relay._clients:
                assert time.monotonic() - start < 2, (
                    len(relay._clients), [(c.sock, c.transport, c.owner_thread) for c in relay._connections]
                )
                time.sleep(0.01)
            assert calls == [1]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_explicit_proxy_settings_are_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "unrelated.invalid")
    monkeypatch.setenv("no_proxy", "unrelated.invalid")
    with upstream([(200, SUCCESS, {})]) as (base, calls):
        with CodexRelay(base, "key", tmp_path / "log", proxies={
            "http": "http://127.0.0.1:1", "no": "127.0.0.1",
        }) as relay:
            with request(relay) as response:
                assert SUCCESS in response.read()
        assert len(calls) == 1
    with upstream([(200, SUCCESS, {})]) as (proxy, calls):
        with CodexRelay("http://provider.invalid/v1", "key", tmp_path / "log2",
                        proxies={"all": proxy.removesuffix("/v1")}) as relay:
            with request(relay) as response:
                assert SUCCESS in response.read()
        assert calls[0]["path"] == "http://provider.invalid/v1/responses"


def test_tls_proxy_is_rejected_without_sending_credentials_in_cleartext(tmp_path):
    with upstream([(200, SUCCESS, {})]) as (base, calls):
        proxy = base.removesuffix("/v1").replace("http://", "https://fixture-user:fixture-pass@")
        with CodexRelay("https://provider.invalid/v1", "key", tmp_path / "log",
                        proxies={"https": proxy, "no": ""}) as relay:
            with pytest.raises(HTTPError) as caught:
                request(relay)
            assert caught.value.code == 502
            assert b"fixture-pass" not in caught.value.read()
            caught.value.close()
            assert relay.permanent_failure.is_set()
        assert not calls


def test_https_connect_preserves_origin_host_header(tmp_path):
    from urllib.request import HTTPSHandler
    relay = CodexRelay("https://provider.invalid:8443/v1", "key", tmp_path / "log",
                       proxies={"https": "http://127.0.0.1:7890", "no": ""})
    req = relay.request(b"{}", {})
    handler = HTTPSHandler()
    build_opener(handler)
    handler.do_request_(req)
    assert req.get_header("Host") == "provider.invalid:8443"
    assert req._tunnel_host == "provider.invalid:8443"
