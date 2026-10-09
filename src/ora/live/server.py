"""Serve the local research app and authenticated commands to its persistent host.

Only loopback clients with a private launch session can submit work or inspect
saved runs. Static assets stay separate from research state; this server neither
chooses scientific tasks nor puts the Groq credential in the browser.
"""

import hmac
import json
import mimetypes
import secrets
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import ValidationError

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest

MAX_BODY_BYTES = 1_048_576


class LocalResearchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host, web_root: Path, port: int = 0):
        self.research_host = host
        self.web_root = web_root.resolve()
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), ResearchHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    @property
    def launch_url(self):
        return f"{self.origin}/live#token={self.token}"


class ResearchHandler(BaseHTTPRequestHandler):
    server: LocalResearchServer

    def log_message(self, *_args):
        # Paths, headers and request bodies may contain private research details.
        pass

    def _headers(self, status, content_type="application/json", *, cookie=False, length=0):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            self.send_header("Set-Cookie", f"ora_session={self.server.token}; "
                             "HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()

    def _json(self, status, value, *, cookie=False):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(status, cookie=cookie, length=len(payload))
        self.wfile.write(payload)

    def _local_origin(self):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self._json(403, {"error": "Unrecognized local host."})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            self._json(403, {"error": "This command must come from the local app."})
            return False
        return True

    def _authenticated(self):
        bearer = self.headers.get("Authorization", "")
        if bearer:
            return (bearer.startswith("Bearer ") and bearer.isascii()
                    and hmac.compare_digest(bearer[7:], self.server.token))
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        value = cookies.get("ora_session")
        return (value is not None and value.value.isascii()
                and hmac.compare_digest(value.value, self.server.token))

    def _body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Send an application/json request.")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_BODY_BYTES:
            raise ValueError("Request must contain at most 1 MiB of JSON.")
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ValueError("Request must be a JSON object.")
        return body

    def do_GET(self):
        if not self._local_origin():
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/api/live/health":
            return self._json(200, {"available": True})
        if parsed.path.startswith("/api/"):
            if not self._authenticated():
                return self._json(401, {"error": "Open this app through the local live launcher."})
            try:
                query = parse_qs(parsed.query)
                run_id = query.get("runId", [None])[0]
                if parsed.path == "/api/live/state":
                    state = self.server.research_host.state(run_id)
                    return self._json(200, state.model_dump(by_alias=True, mode="json"))
                if parsed.path == "/api/live/calls":
                    if not run_id:
                        raise ValueError("A runId is required to inspect calls.")
                    records = self.server.research_host.calls(
                        run_id, query.get("attemptId", [None])[0]
                    )
                    return self._json(200, {"records": records})
                return self._json(404, {"error": "Unknown live API route."})
            except Exception as error:
                return self._error(error)
        requested = unquote(parsed.path)
        path = self.server.web_root / ("index.html" if requested in {"/", "/live"}
                                       else requested.lstrip("/"))
        path = path.resolve()
        if not path.is_relative_to(self.server.web_root) or not path.is_file():
            return self._json(404, {"error": "App asset unavailable; run scripts/dev check."})
        payload = path.read_bytes()
        self._headers(200, mimetypes.guess_type(str(path))[0] or "application/octet-stream",
                      length=len(payload))
        self.wfile.write(payload)

    def do_POST(self):
        if not self._local_origin():
            return
        try:
            path = urlsplit(self.path).path
            if path == "/api/live/session":
                body = self._body()
                token = body.get("token")
                if (not isinstance(token, str) or not token.isascii()
                        or not hmac.compare_digest(token, self.server.token)):
                    return self._json(401, {"error": "Invalid local launch session."})
                return self._json(200, {"authenticated": True}, cookie=True)
            if not self._authenticated():
                return self._json(401, {"error": "A local launch session is required."})
            # Cookie-based writes require a same-origin browser request. Explicit
            # bearer clients (the local CLI) do not need a browser Origin header.
            if (not self.headers.get("Authorization", "").startswith("Bearer ")
                    and self.headers.get("Origin") != self.server.origin):
                return self._json(403, {"error": "A same-origin request is required."})
            body = self._body()
            if path == "/api/live/start":
                receipt = self.server.research_host.start(LiveStartRequest.model_validate(body))
            elif path == "/api/live/followups":
                receipt = self.server.research_host.followup(LiveFollowupRequest.model_validate(body))
            else:
                return self._json(404, {"error": "Unknown live command."})
            return self._json(202, receipt.model_dump(by_alias=True, mode="json"))
        except Exception as error:
            return self._error(error)

    def _error(self, error):
        if isinstance(error, ValidationError):
            return self._json(400, {"error": "Invalid request fields.", "fields": [
                {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                for e in error.errors(include_input=False, include_url=False)
            ]})
        status = getattr(error, "status", None)
        if isinstance(status, int) and 400 <= status <= 599:
            return self._json(status, {"error": str(error)})
        if isinstance(error, (ValueError, json.JSONDecodeError)):
            return self._json(400, {"error": str(error)})
        return self._json(500, {"error": "The local host could not complete this request."})
