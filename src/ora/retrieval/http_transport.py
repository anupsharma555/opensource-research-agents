"""Host-owned pooled HTTPX adapter for the existing discovery transport contract.

This handles only provider discovery/render routes injected by the mission host.
Source intake and bounded validation transports keep their separate policies.
"""

from __future__ import annotations

import json
import threading
import urllib.error
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy

import httpx

from .providers import HTTPResult

_BODY_LIMIT = 8 * 1024 * 1024 + 1
_MAX_REDIRECTS = 10
_SAFE_REDIRECT_HEADERS = frozenset({
    "user-agent", "accept-encoding", "content-type", "authorization", "cookie",
})


def _origin(url: httpx.URL) -> tuple[str, str | None, int | None]:
    port = url.port or {"http": 80, "https": 443}.get(url.scheme)
    return url.scheme, url.host, port


class _RejectResponseCookies(DefaultCookiePolicy):
    """Keep pooled connections without adding cookie persistence to urllib behavior."""

    def set_ok(self, cookie: Cookie, request: object) -> bool:
        return False


class PooledHTTPTransport:
    """Share bounded connections until the owning mission host closes the pool.

    The client respects HTTP_PROXY/HTTPS_PROXY/NO_PROXY captured at creation.
    urllib may also discover macOS/Windows system proxies when no environment
    proxy is set; that platform-specific difference needs an explicit rollout
    check before replacing the default transport there.
    """

    def __init__(self, *, trust_env: bool = True) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self._closing = False
        self._pool_closed = False
        self._client = httpx.Client(
            follow_redirects=False,
            max_redirects=_MAX_REDIRECTS,
            trust_env=trust_env,
            cookies=CookieJar(policy=_RejectResponseCookies()),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    def __call__(
        self,
        method: str,
        url: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 20,
    ) -> HTTPResult:
        with self._lock:
            if self._closing:
                raise RuntimeError("Pooled HTTP transport is closed")
            self._active += 1
        try:
            data = json.dumps(body).encode() if body is not None else None
            outgoing = httpx.Headers({
                "User-Agent": "ORA-stage4-discovery/0.1",
                "Accept-Encoding": "identity",
            })
            if data is not None:
                outgoing["Content-Type"] = "application/x-www-form-urlencoded"
            outgoing.update(headers or {})
            has_unmanaged_headers = any(
                name.lower() not in _SAFE_REDIRECT_HEADERS for name in (headers or {})
            )
            try:
                request = self._client.build_request(
                    method, url, content=data, headers=outgoing, timeout=timeout,
                )
                for redirects in range(_MAX_REDIRECTS + 1):
                    response = self._client.send(request, stream=True, follow_redirects=False)
                    try:
                        next_request = response.next_request
                        if next_request is not None:
                            # urllib never replays a POST body for 307/308.
                            # Cross-origin POSTs and unmanaged headers must not
                            # send provider data or API keys away.
                            post_replay = (request.method not in {"GET", "HEAD"}
                                           and response.status_code in {307, 308})
                            unsafe_origin_change = (
                                _origin(request.url) != _origin(next_request.url)
                                and (request.method not in {"GET", "HEAD"}
                                     or has_unmanaged_headers)
                            )
                            may_follow = not (post_replay or unsafe_origin_change)
                            if may_follow:
                                if redirects == _MAX_REDIRECTS:
                                    raise urllib.error.URLError("Provider redirect limit exceeded")
                                request = next_request
                                continue
                        content = bytearray()
                        # iter_raw preserves compressed wire bytes and limits the read
                        # before decompression. The extra byte is the oversize sentinel.
                        for chunk in response.iter_raw():
                            content.extend(chunk[: _BODY_LIMIT - len(content)])
                            if len(content) >= _BODY_LIMIT:
                                break
                        return HTTPResult(
                            response.status_code, bytes(content),
                            {key.lower(): value for key, value in response.headers.items()},
                        )
                    finally:
                        response.close()
            except httpx.TimeoutException:
                raise TimeoutError("Provider request timed out") from None
            except httpx.RequestError:
                raise urllib.error.URLError("Provider transport failed") from None
        finally:
            self._finish_request()

    def _finish_request(self) -> None:
        should_close = False
        with self._lock:
            self._active -= 1
            if self._closing and self._active == 0 and not self._pool_closed:
                self._pool_closed = True
                should_close = True
        if should_close:
            self._client.close()

    def close(self) -> None:
        """Stop new calls immediately; let active callbacks finish before pool close."""
        should_close = False
        with self._lock:
            self._closing = True
            if self._active == 0 and not self._pool_closed:
                self._pool_closed = True
                should_close = True
        if should_close:
            self._client.close()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._pool_closed

    def __enter__(self) -> PooledHTTPTransport:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
