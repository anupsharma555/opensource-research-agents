"""Private, fail-closed HTTP budget for explicit provider-only validation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from .providers import HTTPResult
from .redact import redact_url


class ValidationStop(RuntimeError):
    """A safe, latched reason to stop this validation batch."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _default_opener(method: str, url: str, *, body: dict | None, headers: dict, timeout: float) -> HTTPResult:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"User-Agent": "ORA-search-validation/0.1", **headers},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return HTTPResult(response.status, response.read(8 * 1024 * 1024 + 1),
                              {k.lower(): v for k, v in response.headers.items()})
    except urllib.error.HTTPError as exc:
        return HTTPResult(exc.code, exc.read(8 * 1024 * 1024 + 1),
                          {k.lower(): v for k, v in exc.headers.items()})


def _allowed(method: str, url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            return False
        host, path = parsed.hostname, parsed.path
    except ValueError:
        return False
    if parsed.fragment:
        return False
    return (
        (method == "POST" and host == "api.exa.ai" and path in {"/search", "/contents"})
        or (method == "POST" and host == "api.tavily.com" and path == "/search")
        or (method == "GET" and host == "eutils.ncbi.nlm.nih.gov" and path in {
            "/entrez/eutils/esearch.fcgi", "/entrez/eutils/esummary.fcgi"})
        or (method == "GET" and host == "api.crossref.org" and
            (path == "/works" or (path.startswith("/works/10.") and
                                  "%2f" in path.lower() and "/" not in path[7:])))
        or (method == "GET" and host == "www.ebi.ac.uk" and
            path == "/europepmc/webservices/rest/search")
    )


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix=".validation-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _numeric_usage(packet: object) -> dict:
    """Keep only bounded numeric accounting; source and error text stay transient."""
    if not isinstance(packet, dict):
        return {}
    output = {}
    for name in ("usage", "cost", "credits", "costDollars"):
        value = packet.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            output[name] = value
        elif isinstance(value, dict):
            allowed = {"requests", "tokens", "input_tokens", "output_tokens",
                       "total_tokens", "searches", "credits", "cost", "usd",
                       "total", "search", "contents", "neural", "keyword", "text"}
            def numeric_tree(node: object, depth: int = 0) -> dict:
                if not isinstance(node, dict) or depth >= 3:
                    return {}
                return {key: (child if isinstance(child, (int, float)) and
                              not isinstance(child, bool) and math.isfinite(child)
                              else numeric_tree(child, depth + 1))
                        for key, child in node.items() if key in allowed and
                        ((isinstance(child, (int, float)) and not isinstance(child, bool)
                          and math.isfinite(child)) or isinstance(child, dict))}
            output[name] = numeric_tree(value)
    return output


class BatchTransport:
    """Exclusive serial transport; ledger reservation is durable before dispatch."""

    def __init__(self, ledger_path: Path, *, batch_id: str, max_attempts: int = 8,
                 clock: Callable[[], float] = time.time, opener=None):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 8:
            raise ValueError("max_attempts must be an integer from 1 to 8")
        self.ledger_path = Path(ledger_path)
        self.batch_id = batch_id
        self.max_attempts = max_attempts
        self.clock = clock
        self.opener = opener or _default_opener
        self.data: dict = {}
        self._lock_fd: int | None = None
        self._case_id: str | None = None

    def __enter__(self):
        self.ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.ledger_path.parent, 0o700)
        self._lock_fd = os.open(str(self.ledger_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValidationStop("batch_lock_busy") from None
            if self.ledger_path.exists():
                self.data = json.loads(self.ledger_path.read_text())
                if self.data.get("batch_id") != self.batch_id or self.data.get("max_attempts") != self.max_attempts:
                    raise ValidationStop("batch_identity_or_budget_mismatch")
            else:
                now = self.clock()
                self.data = {"batch_id": self.batch_id, "max_attempts": self.max_attempts,
                             "started_at": now, "deadline": now + 600, "attempts": [],
                             "cases": {}, "stop_reason": None}
                self.save()
            if any(a.get("state") in {"attempted", "uncertain"} for a in self.data["attempts"]):
                self.stop("unresolved_attempt")
            for attempt in self.data["attempts"]:
                try:
                    capture = (self.ledger_path.parent / attempt["capture"]).resolve()
                    if not capture.is_relative_to(self.ledger_path.parent.resolve()):
                        raise ValueError
                    saved = json.loads(capture.read_text())
                    if any(saved[key] != attempt[key] for key in ("status", "sha256", "byte_count")):
                        raise ValueError
                except (OSError, ValueError, KeyError, TypeError):
                    self.stop("missing_or_inconsistent_capture")
            if self.data.get("stop_reason"):
                raise ValidationStop(self.data["stop_reason"])
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, _type, _value, _traceback):
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def save(self) -> None:
        if self._lock_fd is None:
            raise RuntimeError("BatchTransport must hold its process lock")
        _atomic_json(self.ledger_path, self.data)

    def stop(self, reason: str) -> None:
        # Reasons are internal constants. Never persist exception text or provider data.
        if not self.data.get("stop_reason"):
            self.data["stop_reason"] = reason
            self.save()
        raise ValidationStop(self.data["stop_reason"])

    def set_case(self, case_id: str, started_at: float | None = None) -> None:
        if not case_id or not isinstance(case_id, str):
            self.stop("invalid_case")
        cases = self.data["cases"]
        case = cases.setdefault(case_id, {})
        if "started_at" not in case:
            case["started_at"] = self.clock() if started_at is None else started_at
        case.setdefault("deadline", case["started_at"] + 120)
        self._case_id = case_id
        self.save()

    def _remaining(self) -> float:
        if self.data.get("stop_reason"):
            raise ValidationStop(self.data["stop_reason"])
        now = self.clock()
        if now >= self.data["deadline"]:
            self.stop("batch_deadline")
        if self._case_id is None:
            self.stop("case_not_set")
        case_deadline = self.data["cases"][self._case_id]["deadline"]
        if now >= case_deadline:
            self.stop("case_deadline")
        if len(self.data["attempts"]) >= self.max_attempts:
            self.stop("attempt_cap")
        return min(30.0, self.data["deadline"] - now, case_deadline - now)

    def __call__(self, method: str, url: str, *, body: dict | None = None,
                 headers: dict | None = None, timeout: float = 30) -> HTTPResult:
        method = method.upper()
        if not _allowed(method, url):
            self.stop("endpoint_not_allowed")
        headers = headers or {}
        current = url
        for redirects in range(4):
            if not _allowed(method, current):
                self.stop("redirect_not_allowed")
            remaining = self._remaining()
            safe_url, _ = redact_url(current)
            known_secrets = [str(value) for key, value in headers.items()
                             if key.lower() in {"authorization", "x-api-key", "api-key"}]
            known_secrets.extend(value for key, value in urllib.parse.parse_qsl(
                urllib.parse.urlsplit(current).query) if key.lower() == "api_key")
            for secret in known_secrets:
                for variant in {secret, urllib.parse.quote(secret, safe=""),
                                secret.removeprefix("Bearer "),
                                urllib.parse.quote(secret.removeprefix("Bearer "), safe="")}:
                    if variant:
                        safe_url = safe_url.replace(variant, "[REDACTED]")
            entry = {"number": len(self.data["attempts"]) + 1, "case_id": self._case_id,
                     "method": method, "url": safe_url, "started_at": self.clock(),
                     "state": "attempted"}
            self.data["attempts"].append(entry)
            self.save()
            try:
                result = self.opener(method, current, body=body, headers=headers,
                                     timeout=min(30.0, timeout, remaining))
                if not isinstance(result, HTTPResult):
                    raise TypeError("unexpected transport result")
            except Exception:
                entry["state"] = "uncertain"
                entry["ended_at"] = self.clock()
                self.data["stop_reason"] = "uncertain_transport"
                self.save()
                raise ValidationStop("uncertain_transport") from None
            entry.update(state="observed", ended_at=self.clock(), status=result.status,
                         sha256=hashlib.sha256(result.body).hexdigest(),
                         byte_count=len(result.body),
                         elapsed_ms=round((self.clock() - entry["started_at"]) * 1000, 3))
            try:
                try:
                    packet = json.loads(result.body)
                except (ValueError, UnicodeDecodeError):
                    packet = None
                usage = _numeric_usage(packet)
                capture = {"status": result.status, "sha256": entry["sha256"],
                           "byte_count": entry["byte_count"],
                           "hash_scope": "complete_body" if len(result.body) <= 8 * 1024 * 1024 else "observed_prefix",
                           "json_kind": ("object" if isinstance(packet, dict) else
                                         "array" if isinstance(packet, list) else
                                         "other" if packet is not None else "non_json"),
                           "provider_usage": usage,
                           "has_error": isinstance(packet, dict) and "error" in packet}
                capture_path = self.ledger_path.parent / (self.ledger_path.stem + "-captures") / f"attempt-{entry['number']:02d}.json"
                _atomic_json(capture_path, capture)
                entry["capture"] = str(capture_path.relative_to(self.ledger_path.parent))
                if usage:
                    entry["provider_usage"] = usage
                self.save()
            except Exception:
                entry["state"] = "uncertain"
                self.data["stop_reason"] = "capture_failure"
                self.save()
                raise ValidationStop("capture_failure") from None
            location = next((value for key, value in result.headers.items()
                             if key.lower() == "location"), None)
            if result.status not in {301, 302, 303, 307, 308} or not location:
                return result
            if redirects == 3:
                self.stop("redirect_cap")
            next_url = urllib.parse.urljoin(current, location)
            old, new = urllib.parse.urlsplit(current), urllib.parse.urlsplit(next_url)
            if (old.scheme, old.netloc) != (new.scheme, new.netloc):
                self.stop("redirect_origin")
            if result.status == 303 or (result.status in {301, 302} and method == "POST"):
                method, body = "GET", None
            current = next_url
        self.stop("redirect_cap")
