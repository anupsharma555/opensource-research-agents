"""Private, durable HTTP allowance for one explicit natural-agent evaluation.

The wrapper observes urllib's existing opener path. It never selects tools,
changes request bytes, or stores provider text, headers, credentials, or URLs.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from ora.live.provider import MODEL, URL
from ora.mission.figure_reading import IMAGE_INPUT_TOKENS, VISION_MODEL
from ora.models.token_estimate import estimate_context_tokens, estimate_request_tokens
from ora.retrieval.providers import HTTPDispatchBlocked
from ora.retrieval.validation_transport import _atomic_json


class BudgetStop(RuntimeError):
    """A sanitized, durable reason to end this evaluation."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class BudgetAdmissionStop(BudgetStop, HTTPDispatchBlocked):
    """The current request was blocked before dispatch; earlier attempts are unchanged."""


def _usage(raw: bytes) -> int | None:
    try:
        packet = json.loads(raw)
        usage = packet["usage"]
        total = usage.get("total_tokens")
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if all(type(value) is int and value >= 0 for value in (prompt, completion)):
            summed = prompt + completion
            if total is not None and (type(total) is not int or total != summed):
                return None
            return summed
        if type(total) is int and total >= 0:
            return total
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return None


def _failure_category(error: Exception) -> str:
    """Classify transport failure without retaining exception text or addresses."""
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, TimeoutError):
        return "timeout"
    if isinstance(reason, ConnectionError) or (
        isinstance(reason, OSError)
        and reason.errno in {
            errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET,
            errno.EHOSTUNREACH, errno.ENETDOWN, errno.ENETUNREACH,
        }
    ):
        return "connection"
    return "other"


class _ReplayResponse:
    """Replay inspected bytes while preserving urllib response metadata."""

    def __init__(self, original, raw: bytes):
        self._original = original
        self._stream = io.BytesIO(raw)
        self.status = getattr(original, "status", None)
        self.code = getattr(original, "code", None)
        self.headers = getattr(original, "headers", None)
        self.url = getattr(original, "url", None)
        self.reason = getattr(original, "reason", None)
        self.msg = getattr(original, "msg", None)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self):
        return self._original.geturl()

    def info(self):
        return self._original.info()

    def getcode(self):
        return self.code

    def __getattr__(self, name):
        return getattr(self._original, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        self._stream.close()
        self._original.close()


class AgentBudget:
    """Exclusive process ledger with thread-safe, pre-dispatch reservations."""

    MAX_CAPTURE_BYTES = 8 * 1024 * 1024 + 1

    def __init__(self, path: Path, *, model_limit: int | None = 8,
                 token_limit: int | None = 55_000, retrieval_limit: int | None = 4,
                 accounting_only: bool = False,
                 allow_vision: bool = False,
                 clock: Callable[[], float] = time.time):
        if type(accounting_only) is not bool:
            raise ValueError("accounting-only authorization must be explicit boolean")
        values = (model_limit, token_limit, retrieval_limit)
        # A finite token budget can bound model work without a separate call
        # count. Retrieval has no model-token charge, so it still needs its own
        # explicit bound unless wholly unbounded accounting was authorized.
        if (accounting_only and not all(value is None for value in values)) or (
                not accounting_only and (token_limit is None or retrieval_limit is None)):
            raise ValueError("unlimited aggregate accounting requires all limits None and explicit authorization")
        for value in (model_limit, token_limit):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError("model and token limits must be positive integers")
        if retrieval_limit is not None and (type(retrieval_limit) is not int or retrieval_limit < 0):
            raise ValueError("retrieval limit must be a nonnegative integer")
        self.path = Path(path)
        self.model_limit = model_limit
        self.token_limit = token_limit
        self.retrieval_limit = retrieval_limit
        self.accounting_only = accounting_only
        if type(allow_vision) is not bool:
            raise ValueError("vision allowance must be explicit boolean")
        self.allow_vision = allow_vision
        self.clock = clock
        self.data: dict = {}
        self._fd: int | None = None
        self._mutex = threading.RLock()

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BudgetStop("budget_lock_busy") from None
            if self.path.exists():
                self.data = json.loads(self.path.read_text())
                if self.data.get("limits") != self._limits():
                    raise BudgetStop("budget_identity_mismatch")
            else:
                self.data = {"schema": "ora.agent_validation_budget.v1",
                             "limits": self._limits(), "started_at": self.clock(),
                             "model_attempts": [], "retrieval_attempts": [],
                             "planned_tokens": 0, "peak_planned_tokens": 0,
                             "stop_reason": None}
                self._save()
            if any(entry.get("state") in {"attempted", "uncertain"}
                   for key in ("model_attempts", "retrieval_attempts")
                   for entry in self.data[key]):
                self.stop("unresolved_attempt")
            if self.data.get("stop_reason"):
                raise BudgetStop(self.data["stop_reason"])
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, _type, _value, _traceback):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def _limits(self) -> dict:
        return {"model": self.model_limit, "tokens": self.token_limit,
                "retrieval": self.retrieval_limit,
                **({"accounting_only": True} if self.accounting_only else {}),
                **({"allow_vision": True} if self.allow_vision else {})}

    def _save(self):
        if self._fd is None:
            raise RuntimeError("AgentBudget requires its process lock")
        _atomic_json(self.path, self.data)

    def stop(self, reason: str):
        with self._mutex:
            if not self.data.get("stop_reason"):
                self.data["stop_reason"] = reason
                self._save()
            raise BudgetStop(self.data["stop_reason"])

    def _guard(self):
        if self.data.get("stop_reason"):
            raise BudgetStop(self.data["stop_reason"])

    def _reserve(self, kind: str, raw: bytes | None, native: bool) -> dict:
        self._guard()
        entries = self.data[f"{kind}_attempts"]
        limit = self.model_limit if kind == "model" else self.retrieval_limit
        if limit is not None and len(entries) >= limit:
            self.stop(f"{kind}_attempt_cap")
        entry = {"number": len(entries) + 1, "state": "attempted",
                 "started_at": self.clock()}
        if kind == "model":
            try:
                assert raw is not None
                packet = json.loads(raw)
                cap = packet["max_completion_tokens"]
                vision = packet.get("model") == VISION_MODEL
                if vision:
                    if (not self.allow_vision or native or type(cap) is not int
                            or not 1 <= cap <= 2048 or packet.get("stream") is not False
                            or packet.get("reasoning_effort") != "none" or packet.get("tools")):
                        self.stop("model_request_mismatch")
                    # Count the documented image charge, never the base64 bytes
                    # as GPT-OSS text. Provider usage remains authoritative.
                    messages = packet["messages"]
                    if len(messages) != 1 or messages[0].get("role") != "user":
                        self.stop("model_request_invalid")
                    parts = messages[0]["content"]
                    images = [part for part in parts if part.get("type") == "image_url"]
                    texts = [part for part in parts if part.get("type") == "text"]
                    if (len(parts) != 2 or len(images) != 1 or len(texts) != 1
                            or not isinstance(texts[0].get("text"), str)
                            or not images[0]["image_url"]["url"].startswith((
                                "data:image/png;base64,", "data:image/jpeg;base64,"))):
                        self.stop("model_request_invalid")
                    text_packet = {**packet, "messages": [{"role": "user", "content": texts}]}
                    input_tokens = IMAGE_INPUT_TOKENS + estimate_context_tokens(json.dumps(text_packet))
                    basis = "qwen_one_image_2048_plus_o200k_text_estimate"
                elif (packet.get("model") != MODEL or type(cap) is not int
                    or not 1 <= cap <= 65536 or packet.get("stream") is not False
                    or packet.get("include_reasoning") is not True
                    or packet.get("reasoning_effort") not in (
                        ("low",) if native else ("medium", "high")
                    )):
                    self.stop("model_request_mismatch")
                else:
                    estimate = estimate_request_tokens(raw)
                    input_tokens, basis = estimate.tokens, estimate.basis
                charge = input_tokens + cap
            except (ValueError, TypeError, KeyError, AttributeError, AssertionError):
                self.stop("model_request_invalid")
            if self.token_limit is not None and self.data["planned_tokens"] + charge > self.token_limit:
                self.stop("token_cap")
            entry.update(request_sha256=hashlib.sha256(raw).hexdigest(),
                         model=packet["model"], model_role="vision" if vision else "reasoning",
                         estimated_input_tokens=input_tokens,
                         token_estimate_basis=basis,
                         output_reservation=cap, planned_tokens=charge,
                         native_browser_search=native,
                         provider_internal_search_attempts="unknown" if native else None)
            self.data["planned_tokens"] += charge
            self.data["peak_planned_tokens"] = max(
                self.data["peak_planned_tokens"], self.data["planned_tokens"])
        entries.append(entry)
        self._save()
        return entry

    def _observed(self, kind: str, entry: dict, status: int | None,
                  raw: bytes, *, http_error: bool = False):
        entry.update(state="observed", ended_at=self.clock(), status=status,
                     response_sha256=hashlib.sha256(raw).hexdigest(),
                     response_bytes=len(raw))
        if kind == "model":
            actual = _usage(raw) if not http_error else None
            if status == 429:
                # Usage was not reported. Preserve its reservation while the
                # host waits; rejection alone does not prove zero token usage.
                entry["state"] = "rate_limited"
                entry["usage_status"] = "unreported_reservation_retained"
            elif actual is None:
                self.data["stop_reason"] = "unknown_model_usage" if not http_error else "model_http_error"
            else:
                entry["actual_tokens"] = actual
                self.data["planned_tokens"] += actual - entry["planned_tokens"]
                if self.token_limit is not None and self.data["planned_tokens"] > self.token_limit:
                    self.data["stop_reason"] = "reported_token_cap"
        self._save()

    def _uncertain_transport(self, entry: dict, error: Exception, *, stage: str):
        entry.update(state="uncertain", failure_stage=stage,
                     failure_category=_failure_category(error))
        self.data["stop_reason"] = "uncertain_transport"
        self._save()
        raise BudgetStop("uncertain_transport") from None

    def open(self, original_open, opener, fullurl, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
             **kwargs):
        """Wrap one HTTP exchange; the caller preserves original urllib handling."""
        with self._mutex:
            try:
                self._guard()
                request = fullurl if isinstance(fullurl, urllib.request.Request) else None
                url = request.full_url if request is not None else fullurl
                try:
                    parsed = urllib.parse.urlsplit(url)
                    if (parsed.scheme != "https" or not parsed.hostname
                        or parsed.username is not None or parsed.password is not None):
                        self.stop("endpoint_not_https")
                except (ValueError, TypeError):
                    self.stop("endpoint_invalid")
                is_model = url == URL
                if parsed.hostname == urllib.parse.urlsplit(URL).hostname and not is_model:
                    self.stop("model_endpoint_mismatch")
                body = request.data if request is not None else data
                method = request.get_method() if request is not None else ("POST" if data is not None else "GET")
                if is_model and method != "POST":
                    self.stop("model_request_mismatch")
                native = False
                if is_model:
                    try:
                        packet = json.loads(body)
                        native = {"type": "browser_search"} in packet.get("tools", [])
                    except (ValueError, TypeError, AttributeError):
                        self.stop("model_request_invalid")
                    if native and self.retrieval_limit == 0:
                        self.stop("retrieval_attempt_cap")
                kind = "model" if is_model else "retrieval"
                entry = self._reserve(kind, body, native)
            except BudgetStop as exc:
                raise BudgetAdmissionStop(exc.reason) from None
            stage = "opening"
            try:
                result = original_open(opener, fullurl, data=data, timeout=timeout, **kwargs)
                stage = "capture"
                raw = result.read(self.MAX_CAPTURE_BYTES + 1)
                if len(raw) > self.MAX_CAPTURE_BYTES:
                    entry["state"] = "uncertain"
                    self.stop("response_capture_limit")
                status = getattr(result, "status", getattr(result, "code", None))
                self._observed(kind, entry, status, raw)
                return _ReplayResponse(result, raw)
            except urllib.error.HTTPError as exc:
                if stage == "capture":
                    self._uncertain_transport(entry, exc, stage=stage)
                try:
                    raw = exc.read(self.MAX_CAPTURE_BYTES + 1)
                except Exception as capture_error:
                    self._uncertain_transport(entry, capture_error, stage="capture")
                if len(raw) > self.MAX_CAPTURE_BYTES:
                    entry["state"] = "uncertain"
                    self.stop("response_capture_limit")
                exc.fp = io.BytesIO(raw)
                exc.file = exc.fp
                exc.read = exc.fp.read
                self._observed(kind, entry, exc.code, raw, http_error=True)
                if self.accounting_only and exc.code == 429:
                    from ora.live.provider import _retry_after

                    retry_after = _retry_after(exc.headers)
                    try:
                        error = json.loads(raw) if len(raw) <= 64_000 else {}
                        detail = error.get("error", error.get("provider_error", {}))
                        message = detail.get("message", "") if isinstance(detail, dict) else ""
                    except (ValueError, TypeError, AttributeError):
                        message = ""
                    daily = isinstance(message, str) and re.search(
                        r"\b(?:TPD|RPD)\b|tokens per day|requests per day", message,
                        re.IGNORECASE,
                    )
                    if daily or (retry_after is not None and retry_after >= 3600):
                        self.data["stop_reason"] = (
                            "provider_daily_quota_wait" if daily else "long_provider_wait"
                        )
                        entry["quota_classification"] = (
                            "provider_daily_limit" if daily else "long_wait_unknown_scope"
                        )
                        entry["retry_after_seconds"] = retry_after
                        self._save()
                raise
            except BudgetStop:
                raise
            except Exception as error:
                self._uncertain_transport(entry, error, stage=stage)
