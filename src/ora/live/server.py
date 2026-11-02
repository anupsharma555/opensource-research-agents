"""Serve the local research app and authenticated commands to its persistent host.

Only loopback clients with a private launch session can submit work or inspect
saved runs. Static assets stay separate from research state; this server neither
chooses scientific tasks nor puts the Groq credential in the browser.
"""

import hmac
import json
import mimetypes
import re
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import ValidationError

from ora.contracts.models import (
    LiveBranchCancelRequest,
    LiveFollowupRequest,
    LiveRunCancelRequest,
    LiveStartRequest,
    LiveUserInputRequest,
)
from ora.live.updates import stream_updates, supports_updates

MAX_BODY_BYTES = 1_048_576
MAX_UPDATE_STREAMS = 8


class LocalResearchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host, web_root: Path, port: int = 0):
        self.research_host = host
        self.web_root = web_root.resolve()
        self.token = secrets.token_urlsafe(32)
        self.updates_stopped = threading.Event()
        self.update_slots = threading.BoundedSemaphore(MAX_UPDATE_STREAMS)
        super().__init__(("127.0.0.1", port), ResearchHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def shutdown(self):
        self.updates_stopped.set()
        super().shutdown()

    def server_close(self):
        self.updates_stopped.set()
        super().server_close()

    @property
    def launch_url(self):
        return f"{self.origin}/live#token={self.token}"


class ResearchHandler(BaseHTTPRequestHandler):
    server: LocalResearchServer

    def log_message(self, *_args):
        # Paths, headers and request bodies may contain private research details.
        pass

    def _headers(self, status, content_type="application/json", *, cookie=False, length=0,
                 updates=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if updates and supports_updates(self.server.research_host):
            self.send_header("X-Atlas-Updates", "sqlite-invalidation-v1")
        if cookie:
            self.send_header("Set-Cookie", f"ora_session={self.server.token}; "
                             "HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()

    def _json(self, status, value, *, cookie=False, updates=False):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        try:
            self._headers(status, cookie=cookie, length=len(payload), updates=updates)
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # Obsolete viewer GETs are intentionally aborted. A lost response
            # does not undo a saved command or justify another error response.
            self.close_connection = True

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
            if parsed.path == "/api/live/updates":
                return stream_updates(self)
            try:
                query = parse_qs(parsed.query)
                run_id = query.get("runId", [None])[0]
                if parsed.path == "/api/live/state":
                    state = self.server.research_host.state(run_id)
                    return self._json(200, state.model_dump(by_alias=True, mode="json"),
                                      updates=True)
                if parsed.path == "/api/live/calls":
                    if not run_id:
                        raise ValueError("A runId is required to inspect calls.")
                    records = self.server.research_host.calls(
                        run_id, query.get("attemptId", [None])[0]
                    )
                    return self._json(200, {"records": records})
                if parsed.path == "/api/live/reasoning":
                    if (
                        len(query.get("runId", [])) != 1
                        or len(query.get("callId", [])) != 1
                        or not run_id
                        or not query["callId"][0]
                    ):
                        raise ValueError("One runId and callId are required to inspect reasoning.")
                    return self._json(200, self.server.research_host.returned_reasoning(
                        run_id, query["callId"][0], diagnostic_granted=True,
                    ))
                if parsed.path == "/api/live/peer-record":
                    if (
                        set(query) != {"runId", "kind", "id", "version"}
                        or any(len(query[key]) != 1 or not query[key][0] for key in query)
                        or not re.fullmatch(r"[1-9][0-9]*", query["version"][0])
                    ):
                        raise ValueError("One exact runId, kind, id and positive version are required.")
                    return self._json(200, self.server.research_host.peer_record(
                        query["runId"][0], query["kind"][0], query["id"][0],
                        int(query["version"][0]),
                    ))
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
            elif path == "/api/live/user-inputs":
                command = LiveUserInputRequest.model_validate(body)
                submit = getattr(self.server.research_host, "submit_user_input", None)
                if not callable(submit):
                    return self._json(501, {"error": "User input is unavailable for this local runtime."})
                receipt = submit(command)
            elif path == "/api/live/branch-cancellations":
                command = LiveBranchCancelRequest.model_validate(body)
                cancel_branch = getattr(self.server.research_host, "cancel_branch", None)
                if not callable(cancel_branch):
                    return self._json(501, {
                        "error": "Branch cancellation is unavailable for this local runtime."
                    })
                receipt = cancel_branch(command)
            elif path == "/api/live/run-cancellations":
                command = LiveRunCancelRequest.model_validate(body)
                cancel_run = getattr(self.server.research_host, "cancel_run", None)
                if not callable(cancel_run):
                    return self._json(501, {
                        "error": "Run cancellation is unavailable for this local runtime."
                    })
                receipt = cancel_run(command)
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
