"""Pooled HTTPX parity against a synthetic loopback provider, never the network."""

from __future__ import annotations

import gzip
import json
import socket
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest

from ora.retrieval.discovery import available_routes, run_discovery_page
from ora.retrieval.http_transport import PooledHTTPTransport
from ora.retrieval.providers import HTTPResult, http_transport
from ora.state import StateStore

_LIMIT = 8 * 1024 * 1024 + 1
_COMPRESSED = gzip.compress(b"synthetic-compressed-payload", mtime=0)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.connections = 0
        self.requests = 0
        self.redirect_target = None
        self.slow_started = threading.Event()

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server_port}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def log_message(self, *_args):
        pass

    def _send(self, status, body, **headers):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        with self.server.lock:
            self.server.requests += 1
        path = urlsplit(self.path).path
        if path == "/ok":
            return self._send(200, b"synthetic-ok", Content_Type="text/plain")
        if path == "/redirect":
            return self._send(302, b"", Location="/ok")
        if path == "/redirect-loop":
            return self._send(302, b"loop", Location="/redirect-loop", Connection="close")
        if path == "/redirect-cross-origin":
            return self._send(302, b"", Location=self.server.redirect_target)
        if path == "/error":
            return self._send(503, b"synthetic-wait", Retry_After="7")
        if path == "/missing":
            return self._send(404, b"synthetic-missing")
        if path == "/compressed":
            return self._send(200, _COMPRESSED, Content_Encoding="gzip")
        if path == "/oversize":
            return self._send(200, b"x" * (_LIMIT + 1024))
        if path == "/set-cookie":
            return self._send(200, b"cookie-offered", Set_Cookie="fixture_secret=never_forward")
        if path == "/redirect-cookie":
            return self._send(302, b"", Location="/echo", Set_Cookie="fixture_secret=never_forward")
        if path == "/echo":
            return self._send(200, json.dumps({
                "cookie": self.headers.get("Cookie"),
                "authorization": self.headers.get("Authorization"),
                "user_agent": self.headers.get("User-Agent"),
                "accept_encoding": self.headers.get("Accept-Encoding"),
                "method": "GET",
            }).encode())
        if path == "/unknown":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if path == "/slow":
            self.server.slow_started.set()
            time.sleep(0.2)
            return self._send(200, b"late")
        return self._send(404, b"unknown")

    def do_POST(self):
        with self.server.lock:
            self.server.requests += 1
        if self.path.startswith("/post-redirect"):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            status = int(self.path.rsplit("-", 1)[-1]) if self.path != "/post-redirect" else 302
            return self._send(status, b"", Location=self.server.redirect_target or "/echo")
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        self._send(201, json.dumps({
            "body": raw.decode(), "content_type": self.headers.get("Content-Type"),
            "accept_encoding": self.headers.get("Accept-Encoding"),
        }).encode(), Content_Type="application/json")


@pytest.fixture
def server():
    host = _Server()
    thread = threading.Thread(target=host.serve_forever, daemon=True)
    thread.start()
    try:
        yield host
    finally:
        host.shutdown()
        host.server_close()
        thread.join(timeout=2)


def test_httpx_result_parity_redirects_post_headers_raw_bytes_and_cap(server):
    with PooledHTTPTransport(trust_env=False) as transport:
        for path in ("/ok", "/redirect", "/error", "/missing", "/compressed", "/oversize"):
            baseline = http_transport("GET", server.origin + path)
            candidate = transport("GET", server.origin + path)
            assert isinstance(candidate, HTTPResult)
            assert candidate.status == baseline.status
            assert candidate.body == baseline.body
            assert candidate.headers.get("retry-after") == baseline.headers.get("retry-after")
            assert candidate.headers.get("content-encoding") == baseline.headers.get("content-encoding")
        assert len(transport("GET", server.origin + "/oversize").body) == _LIMIT
        assert transport("GET", server.origin + "/compressed").body == _COMPRESSED
        body = {"synthetic": "value"}
        baseline = http_transport("POST", server.origin + "/post", body=body)
        candidate = transport("POST", server.origin + "/post", body=body)
        assert json.loads(candidate.body) == json.loads(baseline.body)
        custom = transport("POST", server.origin + "/post", body=body, headers={"Content-Type": "application/json"})
        assert json.loads(custom.body)["content_type"] == "application/json"
        redirected = transport("POST", server.origin + "/post-redirect", body=body)
        assert json.loads(redirected.body)["method"] == "GET"


def test_pool_reuses_connections_without_cookies_and_closes(server):
    transport = PooledHTTPTransport(trust_env=False)
    try:
        for _ in range(12):
            assert transport("GET", server.origin + "/ok").status == 200
        assert server.connections == 1
        transport("GET", server.origin + "/set-cookie")
        echo = json.loads(transport("GET", server.origin + "/echo").body)
        assert echo["cookie"] is None
        assert echo["accept_encoding"] == "identity"
        assert echo["user_agent"] == "ORA-stage4-discovery/0.1"
        redirected = json.loads(transport("GET", server.origin + "/redirect-cookie").body)
        assert redirected["cookie"] is None
    finally:
        transport.close()
    transport.close()
    with pytest.raises(RuntimeError, match="closed"):
        transport("GET", server.origin + "/ok")


def test_concurrent_calls_and_unknown_response_do_not_retry(server):
    with PooledHTTPTransport(trust_env=False) as transport:
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(
                lambda _: transport("GET", server.origin + "/ok"), range(24),
            ))
        assert all(result.status == 200 and result.body == b"synthetic-ok" for result in results)
        before = server.requests
        with pytest.raises(urllib.error.URLError, match="Provider transport failed"):
            transport("GET", server.origin + "/unknown")
        assert server.requests == before + 1
        with pytest.raises(TimeoutError, match="Provider request timed out"):
            transport("GET", server.origin + "/slow", timeout=0.01)


def test_discovery_classifies_mapped_timeout_and_unknown_response(server, tmp_path):
    route = available_routes()["crossref.search"]
    def request(attempt):
        return {
            "mission_id": "synthetic-httpx-mission", "task_id": "synthetic-task",
            "request_id": "synthetic-httpx-search", "attempt_id": attempt,
            "agent_instance_id": "synthetic-agent", "route": "crossref.search",
            "provider": route["provider"], "index": route["index"],
            "operation": route["operation"], "query": "synthetic lookup",
            "filters": {}, "requested_at": "2026-10-30T10:00:00Z",
            "requested_window": {"offset": 0, "limit": 2},
        }
    store = StateStore(tmp_path)
    with PooledHTTPTransport(trust_env=False) as transport:
        for attempt, path, timeout in (("unknown", "/unknown", 20), ("timed", "/slow", 0.01)):
            def local(_method, _url, **_kwargs):
                return transport("GET", server.origin + path, timeout=timeout)
            result = run_discovery_page(store, request(attempt), transport=local)
            assert result["provider_payload_condition"] == "transport_error"
            assert result["http_legs"] == []
            assert result["outcome"] != "success"


def test_close_waits_for_active_callback_without_blocking_host_shutdown(server, monkeypatch):
    transport = PooledHTTPTransport(trust_env=False)
    close_calls = []
    original_close = transport._client.close
    def counted_close():
        close_calls.append(1)
        original_close()
    monkeypatch.setattr(transport._client, "close", counted_close)
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(transport, "GET", server.origin + "/slow")
        assert server.slow_started.wait(timeout=2)
        started = time.perf_counter()
        transport.close()
        assert time.perf_counter() - started < 0.1
        assert not transport.closed
        with pytest.raises(RuntimeError, match="closed"):
            transport("GET", server.origin + "/ok")
        assert pending.result(timeout=2).body == b"late"
    assert transport.closed
    transport.close()
    assert len(close_calls) == 1


def test_cross_origin_redirect_does_not_forward_authorization(server):
    other = _Server()
    thread = threading.Thread(target=other.serve_forever, daemon=True)
    thread.start()
    try:
        server.redirect_target = other.origin + "/echo"
        with PooledHTTPTransport(trust_env=False) as transport:
            result = transport("GET", server.origin + "/redirect-cross-origin", headers={
                "Authorization": "Bearer synthetic-placeholder",
            })
        assert json.loads(result.body)["authorization"] is None
    finally:
        other.shutdown()
        other.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("status", [307, 308])
def test_post_preserving_redirects_return_status_without_replaying_body_or_key(server, status):
    other = _Server()
    thread = threading.Thread(target=other.serve_forever, daemon=True)
    thread.start()
    try:
        with PooledHTTPTransport(trust_env=False) as transport:
            before = server.requests
            same_origin = transport("POST", server.origin + f"/post-redirect-{status}",
                                    body={"synthetic": "private-body"},
                                    headers={"x-api-key": "synthetic-placeholder"})
            assert same_origin.status == status
            assert server.requests == before + 1
            server.redirect_target = other.origin + "/post"
            cross_origin = transport("POST", server.origin + f"/post-redirect-{status}",
                                     body={"synthetic": "private-body"},
                                     headers={"x-api-key": "synthetic-placeholder"})
            assert cross_origin.status == status
            assert other.requests == 0
            assert http_transport("POST", server.origin + f"/post-redirect-{status}",
                                  body={"synthetic": "private-body"}).status == status
    finally:
        other.shutdown()
        other.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("status", [301, 302, 303])
def test_post_redirects_drop_body_on_same_origin_and_stop_before_cross_origin(server, status):
    other = _Server()
    thread = threading.Thread(target=other.serve_forever, daemon=True)
    thread.start()
    try:
        with PooledHTTPTransport(trust_env=False) as transport:
            same_origin = transport("POST", server.origin + f"/post-redirect-{status}",
                                    body={"synthetic": "private-body"})
            assert json.loads(same_origin.body)["method"] == "GET"
            server.redirect_target = other.origin + "/echo"
            cross_origin = transport("POST", server.origin + f"/post-redirect-{status}",
                                     body={"synthetic": "private-body"},
                                     headers={"x-api-key": "synthetic-placeholder"})
            assert cross_origin.status == status
            assert other.requests == 0
    finally:
        other.shutdown()
        other.server_close()
        thread.join(timeout=2)


def test_cross_origin_get_with_unmanaged_api_key_stops_before_redirect(server):
    other = _Server()
    thread = threading.Thread(target=other.serve_forever, daemon=True)
    thread.start()
    try:
        server.redirect_target = other.origin + "/echo"
        with PooledHTTPTransport(trust_env=False) as transport:
            result = transport("GET", server.origin + "/redirect-cross-origin",
                               headers={"x-api-key": "synthetic-placeholder"})
        assert result.status == 302
        assert other.requests == 0
    finally:
        other.shutdown()
        other.server_close()
        thread.join(timeout=2)


def test_intermediate_redirect_body_is_not_eagerly_read(server, monkeypatch):
    original_read = httpx.Response.read

    def refuse_redirect_read(response, *args, **kwargs):
        if response.is_redirect:
            raise AssertionError("An intermediate redirect body was read without the byte cap")
        return original_read(response, *args, **kwargs)

    monkeypatch.setattr(httpx.Response, "read", refuse_redirect_read)
    with PooledHTTPTransport(trust_env=False) as transport:
        assert transport("GET", server.origin + "/redirect").body == b"synthetic-ok"


def test_redirect_loop_stops_after_ten_hops(server):
    with PooledHTTPTransport(trust_env=False) as transport:
        before = server.requests
        with pytest.raises(urllib.error.URLError, match="redirect limit exceeded"):
            transport("GET", server.origin + "/redirect-loop")
        assert server.requests == before + 11


def test_environment_proxy_and_no_proxy_are_selected_at_client_creation(server, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", server.origin)
    with PooledHTTPTransport() as transport:
        proxied = transport("GET", "http://synthetic.invalid/ok")
    assert proxied.status == 200 and proxied.body == b"synthetic-ok"
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with PooledHTTPTransport() as transport:
        direct = transport("GET", server.origin + "/ok")
    assert direct.status == 200
