"""Pinned Groq GPT-OSS requests with source-use and conversation preflight.

The host owns tool execution, retries, admission, and durable receipts. A call is
one request; a continuation or repair must receive a new call and attempt ID.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Literal

from ora.credentials import CredentialProfileError, GroqCredentialProfiles, validate_selection
from ora.live.provider import (
    MAX_RESPONSE_BYTES,
    MODEL,
    URL,
    USER_AGENT,
    CredentialUnavailable,
    GroqProvider,
    _retry_after,
)
from ora.models.usage import normalize_groq_usage, observe_groq_cache

Phase = Literal["collect", "final"]
Status = Literal["complete", "tool_requested", "partial", "failed"]


class ModelPreflightError(ValueError):
    """A request was rejected before credential lookup or network dispatch."""

    def __init__(self, message: str, *, reasoning_status: Literal["unknown", "unsupported"] = "unknown"):
        super().__init__(message)
        self.reasoning_status = reasoning_status


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


@dataclass(frozen=True)
class SourceUse:
    """Exact source version and its explicit model-use decision.

    Source bytes are supplied here, not embedded in caller-authored messages.
    Unknown and restricted copies are rejected even when reached via cache or peer.
    """

    source_id: str
    version: int
    sha256: str
    use: Literal["permitted", "restricted", "unknown"]
    allowed_for_model: bool
    unknown_policy: Literal["reject", "allow_metadata_only"] = "reject"
    text: str | None = None
    route: Literal["direct", "cache", "peer"] = "direct"
    copy_sha256: str | None = None
    passage_refs: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CallConfiguration:
    configuration_id: str
    configuration_sha256: str
    instructions: str
    provider: str = "groq"
    endpoint: str = URL
    model_id: str = MODEL
    reasoning_effort: str = "high"
    include_reasoning: bool = True
    max_completion_tokens: int = 8192
    stream: bool = False
    credential_profile: str = "primary"
    quota_domain: str = "groq-selected"


@dataclass(frozen=True)
class PreparedCall:
    call_id: str
    attempt_id: str
    parent_call_id: str | None
    task_category: str
    phase: Phase
    configuration_id: str
    configuration_sha256: str
    evidence_refs: tuple[dict[str, Any], ...]
    body: dict[str, Any] = field(repr=False)
    request_sha256: str
    credential_profile: str = "primary"
    quota_domain: str = "groq-selected"

    def manifest(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id, "attempt_id": self.attempt_id,
            "parent_call_id": self.parent_call_id, "task_category": self.task_category,
            "phase": self.phase, "provider": "groq", "endpoint": URL, "model_id": MODEL,
            "configuration_id": self.configuration_id,
            "configuration_sha256": self.configuration_sha256,
            "credential_profile": self.credential_profile,
            "quota_domain": self.quota_domain,
            "effective_reasoning_effort": self.body["reasoning_effort"],
            "reasoning_effort_basis": (
                "browser_search" if {"type": "browser_search"} in self.body.get("tools", [])
                else "selected_configuration"
            ),
            "requested_settings": {key: value for key, value in self.body.items()
                                   if key not in {"messages", "tools"}},
            "request_sha256": self.request_sha256,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class ModelOutcome:
    status: Status
    call_id: str
    attempt_id: str
    request_sha256: str
    response_sha256: str | None
    response_id: str | None
    provider_request_id: str | None
    returned_model: str | None
    finish_reason: str | None
    content: str | None = field(repr=False)
    tool_calls: tuple[dict[str, Any], ...] = field(repr=False)
    reasoning: str | None = field(repr=False)  # Restricted; not a public projection.
    reasoning_status: Literal["returned", "absent", "truncated", "unknown", "unsupported"]
    usage: dict[str, Any]
    rate_headers: dict[str, str]
    timings: dict[str, float]
    error_kind: str | None = None
    http_status: int | None = None
    retry_after_seconds: float | None = None
    partial_response: bytes | None = field(default=None, repr=False)
    provider_error: dict[str, Any] | None = field(default=None, repr=False)
    provider_metadata: dict[str, Any] | None = field(default=None, repr=False)
    cache_observation: dict[str, str | int] = field(
        default_factory=lambda: {"status": "unavailable"},
    )

    @property
    def executable_tool_calls(self) -> tuple[dict[str, Any], ...]:
        """Only complete and JSON-decodable calls may cross into tool execution."""
        return self.tool_calls if self.status == "tool_requested" else ()

    def assistant_tool_message(self) -> dict[str, Any]:
        """Preserve the exact completed assistant request for a dependent turn."""
        if self.status != "tool_requested":
            raise ModelPreflightError("only a complete tool request can be continued")
        message = {"role": "assistant", "content": self.content,
                   "tool_calls": list(self.tool_calls)}
        if self.reasoning is not None:
            message["reasoning"] = self.reasoning
        return message

    def tool_result_message(self, *, content: str, name: str | None = None) -> dict[str, Any]:
        if self.status != "tool_requested" or len(self.tool_calls) != 1:
            raise ModelPreflightError("tool result needs one completed model request")
        if not isinstance(content, str):
            raise ModelPreflightError("tool result content must be text")
        call = self.tool_calls[0]
        if name is not None and name != call["function"]["name"]:
            raise ModelPreflightError("tool result name must match the model request")
        return {"role": "tool", "tool_call_id": call["id"],
                "name": call["function"]["name"], "content": content}


def _function_call(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("type") != "function":
        raise ModelPreflightError("tool call must be a local function")
    function = raw.get("function")
    if not isinstance(raw.get("id"), str) or not raw["id"] or not isinstance(function, dict):
        raise ModelPreflightError("tool call needs an ID and function")
    if not isinstance(function.get("name"), str) or not function["name"]:
        raise ModelPreflightError("tool call needs a function name")
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        raise ModelPreflightError("tool call arguments must be complete JSON text")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise ModelPreflightError("tool call arguments are incomplete or malformed") from exc
    if not isinstance(parsed, dict):
        raise ModelPreflightError("tool call arguments must be a JSON object")
    return {"id": raw["id"], "type": "function",
            "function": {"name": function["name"], "arguments": arguments}}


_MISSING_TOOL_NAME = re.compile(
    r"\battempted to call tool ['\"]([A-Za-z_][A-Za-z0-9_.-]{0,80})['\"] which was not in\b",
    re.IGNORECASE,
)

_NULL_SELECTED_ID = re.compile(
    r"\bselected_id\b.{0,80}\bexpected\b.{0,60}\bgot\b.{0,12}\bnull\b",
    re.IGNORECASE | re.DOTALL,
)

_MISSING_REQUIRED_TOOL_ARGUMENT = re.compile(
    r"^Tool call validation failed: tool call validation failed: parameters for tool "
    r"(?P<tool>[A-Za-z_][A-Za-z0-9_]*) did not match schema: errors: "
    r"\[missing properties: '(?P<field>[A-Za-z_][A-Za-z0-9_]*)'\]$",
    re.IGNORECASE,
)


def is_selected_id_null_tool_rejection(error: dict[str, Any] | None) -> bool:
    """Recognize only the provider's exact known optional-ID validation failure."""
    if not isinstance(error, dict):
        return False
    message = error.get("message")
    return bool(
        error.get("code") == "tool_use_failed"
        and isinstance(message, str)
        and "search_scholarly" in message
        and _NULL_SELECTED_ID.search(message)
    )


def classify_tool_failure_from_tools(
    tools: list[dict[str, Any]], error: dict[str, Any] | None,
) -> str | None:
    """Classify a bounded provider rejection against the exact saved tool schema."""
    if not error or error.get("code") != "tool_use_failed":
        return None
    message = error.get("message")
    matched = _MISSING_TOOL_NAME.search(message) if isinstance(message, str) else None
    functions = [item.get("function") for item in tools
                 if isinstance(item, dict)]
    if not matched:
        if is_selected_id_null_tool_rejection(error):
            for function in functions:
                if not isinstance(function, dict) or function.get("name") != "search_scholarly":
                    continue
                schema = (function.get("parameters") or {}).get("properties", {}).get("selected_id", {})
                types = schema.get("type") if isinstance(schema, dict) else None
                if isinstance(types, list) and set(types) == {"string", "integer"}:
                    return "optional_selected_id_null"
        return None
    attempted = matched.group(1)
    if (attempted == "browser_search"
            and {"type": "browser_search"} in tools):
        # The native descriptor has no function.name. A provider rejection of
        # that name is distinct from an unregistered local function call.
        return "native_tool_validation"
    if attempted in {item.get("name") for item in functions if isinstance(item, dict)}:
        return None
    for function in functions:
        if not isinstance(function, dict):
            continue
        route = (function.get("parameters") or {}).get("properties", {}).get("route", {})
        if isinstance(route, dict) and isinstance(route.get("enum"), list) and attempted in route["enum"]:
            return "route_used_as_function_name"
    return "unregistered_tool_function"


def rejected_unregistered_local_tool_name(
    tools: list[dict[str, Any]], error: dict[str, Any] | None,
) -> str | None:
    """Return only a literal, bounded rejected local function name."""
    if (not isinstance(error, dict)
            or error.get("type") != "invalid_request_error"
            or error.get("code") != "tool_use_failed"):
        return None
    message = error.get("message")
    if not isinstance(message, str) or len(message) > 180:
        return None
    match = _MISSING_TOOL_NAME.search(message)
    if match is None:
        return None
    attempted = match.group(1)
    if (attempted == "browser_search"
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,80}", attempted) is None
            or message not in {
                f"attempted to call tool '{attempted}' which was not in request.tools",
                "Tool call validation failed: "
                f"attempted to call tool '{attempted}' which was not in request.tools",
                "Tool call validation failed: tool call validation failed: "
                f"attempted to call tool '{attempted}' which was not in request.tools",
            }
            or classify_tool_failure_from_tools(tools, error) != "unregistered_tool_function"):
        return None
    return attempted


def classify_missing_required_tool_argument(
    tools: list[dict[str, Any]], error: dict[str, Any] | None,
) -> tuple[str, str] | None:
    """Match one captured provider rejection to a field required by its pinned tool schema."""
    if (not isinstance(error, dict) or error.get("type") != "invalid_request_error"
            or error.get("code") != "tool_use_failed"):
        return None
    message = error.get("message")
    match = _MISSING_REQUIRED_TOOL_ARGUMENT.fullmatch(message) if isinstance(message, str) else None
    if match is None:
        return None
    tool, field = match.group("tool", "field")
    functions = [
        item["function"] for item in tools
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
        and item["function"].get("name") == tool
    ]
    if len(functions) != 1:
        return None
    parameters = functions[0].get("parameters")
    if not isinstance(parameters, dict):
        return None
    required, properties = parameters.get("required"), parameters.get("properties")
    if (not isinstance(required, list) or not isinstance(properties, dict)
            or field not in required or not isinstance(properties.get(field), dict)):
        return None
    return tool, field


def _tool_failure_kind(prepared: PreparedCall, error: dict[str, Any] | None) -> str | None:
    return classify_tool_failure_from_tools(prepared.body.get("tools", []), error)


def _messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(messages, list) or not messages:
        raise ModelPreflightError("messages must be a nonempty list")
    cleaned: list[dict[str, Any]] = []
    pending: set[str] = set()
    used: set[str] = set()
    for raw in messages:
        if not isinstance(raw, dict) or raw.get("role") not in {
            "system", "developer", "user", "assistant", "tool",
        }:
            raise ModelPreflightError("message role is unsupported")
        role = raw["role"]
        if pending and role != "tool":
            raise ModelPreflightError("every assistant tool call needs its matching result")
        content = raw.get("content")
        if not isinstance(content, str) and not (
            role == "assistant" and content is None and raw.get("tool_calls")
        ):
            raise ModelPreflightError("message content must be text or an assistant tool request")
        entry: dict[str, Any] = {"role": role, "content": content}
        if role == "assistant" and raw.get("tool_calls") is not None:
            calls = raw["tool_calls"]
            if not isinstance(calls, list) or len(calls) != 1:
                raise ModelPreflightError("selected model requires one sequential tool call")
            call = _function_call(calls[0])
            if call["id"] in used:
                raise ModelPreflightError("tool call ID was reused")
            used.add(call["id"])
            pending.add(call["id"])
            entry["tool_calls"] = [call]
            # Groq's assistant-message shape permits returned reasoning on tool turns.
            # It must come from a restricted prior response, not a fabricated rationale.
            if "reasoning" in raw:
                if not isinstance(raw["reasoning"], str):
                    raise ModelPreflightError("assistant reasoning must be returned text")
                entry["reasoning"] = raw["reasoning"]
        elif "reasoning" in raw or "tool_calls" in raw:
            raise ModelPreflightError("reasoning and tool calls belong to assistant tool turns")
        if role == "tool":
            call_id = raw.get("tool_call_id")
            if call_id not in pending:
                raise ModelPreflightError("tool result needs a pending matching tool_call_id")
            pending.remove(call_id)
            entry["tool_call_id"] = call_id
            if "name" in raw:
                if not isinstance(raw["name"], str) or not raw["name"]:
                    raise ModelPreflightError("tool result name must be nonempty")
                entry["name"] = raw["name"]
        cleaned.append(entry)
    if pending:
        raise ModelPreflightError("unfinished tool turn cannot be sent as a continuation")
    return cleaned


def _strict_schema(schema: dict[str, Any]) -> None:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ModelPreflightError("strict final schema needs an object root")

    def inspect(node: Any) -> None:
        if not isinstance(node, dict):
            raise ModelPreflightError("schema node must be an object")
        if set(node) - {"type", "properties", "required", "additionalProperties", "items",
                         "enum", "description", "title"}:
            raise ModelPreflightError("schema uses an unqualified validation keyword")
        kind = node.get("type")
        types = kind if isinstance(kind, list) else [kind]
        if not types or any(value not in {"object", "array", "string", "integer",
                                            "number", "boolean", "null"} for value in types):
            raise ModelPreflightError("schema type is unsupported")
        if len(types) > 1 and (len(types) != 2 or "null" not in types):
            raise ModelPreflightError("schema union supports only one type plus null")
        if "object" in types:
            props = node.get("properties")
            if not isinstance(props, dict) or node.get("additionalProperties") is not False:
                raise ModelPreflightError("strict object schema needs closed properties")
            required = node.get("required")
            if (not isinstance(required, list)
                or not all(isinstance(item, str) for item in required)
                or set(required) != set(props)):
                raise ModelPreflightError("strict object schema needs every property required")
            for child in props.values():
                inspect(child)
        if "array" in types:
            if "items" not in node:
                raise ModelPreflightError("strict array schema needs items")
            inspect(node["items"])
        if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
            raise ModelPreflightError("schema enum must be nonempty")
    inspect(schema)


class GroqSelectedAdapter:
    """One request at a time; no hidden retries, model changes or tool execution."""

    def __init__(self, root: Path, *, transport: Callable[..., Any] | None = None,
                 timeout_seconds: float = 120) -> None:
        self.root = Path(root)
        self.transport = transport or urllib.request.urlopen
        self.timeout_seconds = timeout_seconds
        self._credential = GroqProvider(self.root)
        self.credential_profiles = GroqCredentialProfiles(self.root)

    def prepare(
        self, configuration: CallConfiguration, *, call_id: str, attempt_id: str,
        task_category: str, phase: Phase, messages: list[dict[str, Any]],
        sources: tuple[SourceUse, ...] = (), tools: tuple[dict[str, Any], ...] = (),
        schema_name: str | None = None, schema: dict[str, Any] | None = None,
        parent_call_id: str | None = None,
    ) -> PreparedCall:
        try:
            validate_selection(configuration.credential_profile, configuration.quota_domain)
        except CredentialProfileError as exc:
            raise ModelPreflightError(str(exc)) from exc
        if not all((call_id, attempt_id, task_category, configuration.configuration_id,
                    configuration.configuration_sha256, configuration.instructions)):
            raise ModelPreflightError("call and configuration identity are required")
        if (len(configuration.configuration_sha256) != 64 or
            any(c not in "0123456789abcdef" for c in configuration.configuration_sha256)):
            raise ModelPreflightError("configuration needs an exact SHA-256")
        if (configuration.provider, configuration.endpoint, configuration.model_id) != (
            "groq", URL, MODEL,
        ) or configuration.reasoning_effort != "high" or configuration.include_reasoning is not True:
            raise ModelPreflightError("selected Groq model and high reasoning must stay pinned",
                                      reasoning_status="unsupported")
        if type(configuration.stream) is not bool:
            raise ModelPreflightError("stream setting must be boolean")
        budget = configuration.max_completion_tokens
        if type(budget) is not int or not 1 <= budget <= 65536:
            raise ModelPreflightError("output budget must be within selected model limits")
        if phase not in {"collect", "final"}:
            raise ModelPreflightError("phase must be collect or final")
        if not isinstance(sources, tuple) or not all(isinstance(s, SourceUse) for s in sources):
            raise ModelPreflightError("sources need typed use records")
        refs = []
        source_lines = []
        for source in sources:
            if (not source.source_id or type(source.version) is not int or source.version < 1
                or len(source.sha256) != 64
                or any(c not in "0123456789abcdef" for c in source.sha256)
                or source.route not in {"direct", "cache", "peer"}
                or source.unknown_policy not in {"reject", "allow_metadata_only"}):
                raise ModelPreflightError("source needs exact ID, version and SHA-256")
            if source.use == "permitted" and source.allowed_for_model is True:
                if source.text is None or hashlib.sha256(source.text.encode()).hexdigest() != source.sha256:
                    raise ModelPreflightError("permitted source bytes do not match their hash")
            elif (source.use == "unknown" and source.unknown_policy == "allow_metadata_only"
                  and source.allowed_for_model is False and source.text is None):
                pass
            else:
                raise ModelPreflightError("source copy is not permitted for model use")
            refs.append({"id": source.source_id, "version": source.version,
                         "sha256": source.sha256, "use": source.use,
                         "allowed_for_model": source.allowed_for_model,
                         "unknown_policy": source.unknown_policy, "route": source.route})
            line = {"id": source.source_id, "version": source.version,
                    "sha256": source.sha256}
            if source.passage_refs:
                if (not isinstance(source.copy_sha256, str) or len(source.copy_sha256) != 64
                        or any(c not in "0123456789abcdef" for c in source.copy_sha256)):
                    raise ModelPreflightError("Checked passages need the original source-copy checksum")
                for pointer in source.passage_refs:
                    if (not isinstance(pointer, dict) or pointer.get("kind") != "source_passage"
                            or not isinstance(pointer.get("id"), str)
                            or type(pointer.get("version")) is not int or pointer["version"] < 1):
                        raise ModelPreflightError("Checked passages need exact saved references")
                details = {"text_basis": "checked_passages", "copy_sha256": source.copy_sha256,
                           "passage_refs": list(source.passage_refs)}
                line.update(details)
                refs[-1].update(details)
            if source.text is not None:
                line["text"] = source.text
            source_lines.append(line)
        cleaned = _messages(messages)
        # Caller messages may contain ordinary task text, but explicit denied source
        # copies are rejected above regardless of whether their route was direct/peer/cache.
        body: dict[str, Any] = {
            "model": MODEL,
            "messages": [{"role": "developer", "content": configuration.instructions},
                         *cleaned],
            "reasoning_effort": "high", "include_reasoning": True,
            "max_completion_tokens": budget, "stream": configuration.stream,
        }
        if source_lines:
            body["messages"].insert(1, {"role": "user", "content": json.dumps(
                {"source_packets": source_lines}, ensure_ascii=False, separators=(",", ":"),
            )})
        if phase == "collect":
            if schema is not None or schema_name is not None:
                raise ModelPreflightError("collection cannot request strict output")
            if not tools:
                raise ModelPreflightError("collection needs at least one tool")
            if len(tools) > 128:
                raise ModelPreflightError("selected endpoint supports at most 128 functions")
            names: set[str] = set()
            browser_search_seen = False
            for tool in tools:
                if type(tool) is dict and tool == {"type": "browser_search"}:
                    if browser_search_seen:
                        raise ModelPreflightError("browser_search may be selected only once")
                    browser_search_seen = True
                    continue
                if not isinstance(tool, dict) or tool.get("type") != "function":
                    raise ModelPreflightError("unsupported tool type")
                function = tool.get("function")
                if (not isinstance(function, dict)
                    or not isinstance(function.get("name"), str)
                    or not function["name"]
                    or not isinstance(function.get("parameters"), dict)):
                    raise ModelPreflightError("local tool needs a named function")
                if function["name"] in names:
                    raise ModelPreflightError("local tool names must be distinct")
                names.add(function["name"])
            if browser_search_seen and configuration.stream:
                raise ModelPreflightError("browser_search requires a non-streaming response")
            if browser_search_seen:
                body["reasoning_effort"] = "low"
            body.update({"tools": list(tools), "tool_choice": "auto",
                         "parallel_tool_calls": False})
            if configuration.stream:
                body["stream_options"] = {"include_usage": True}
        else:
            if configuration.stream:
                raise ModelPreflightError("strict final output cannot stream")
            if tools or not isinstance(schema_name, str) or not schema_name or schema is None:
                raise ModelPreflightError("final phase needs strict schema and no tools")
            _strict_schema(schema)
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema_name, "strict": True, "schema": schema,
            }}
        try:
            request_hash = _sha(body)
        except (TypeError, ValueError) as exc:
            raise ModelPreflightError("request must be finite JSON") from exc
        return PreparedCall(call_id, attempt_id, parent_call_id, task_category,
                            phase, configuration.configuration_id,
                            configuration.configuration_sha256, tuple(refs), body, request_hash,
                            configuration.credential_profile, configuration.quota_domain)

    def invoke(self, prepared: PreparedCall) -> ModelOutcome:
        if _sha(prepared.body) != prepared.request_sha256:
            raise ModelPreflightError("prepared request changed before dispatch")
        encoded = _encoded(prepared.body)
        try:
            self.credential_profiles.validate_binding(
                prepared.credential_profile, prepared.quota_domain
            )
        except CredentialProfileError:
            return self._failed(prepared, "credential_profile_mismatch", None, 503)
        if not self.credential_profiles.is_enabled(prepared.credential_profile):
            return self._failed(prepared, "credential_profile_paused", None, 503)
        try:
            key = self._credential._key(profile=prepared.credential_profile)
        except CredentialUnavailable:
            return self._failed(prepared, "credential_unavailable", None, 503)
        request = urllib.request.Request(URL, data=encoded, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        })
        started = time.monotonic()
        headers: dict[str, str] = {}
        try:
            with self.transport(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                headers = _safe_headers(response.headers)
        except urllib.error.HTTPError as exc:
            failed = self._failed(prepared, "http_error", started, exc.code,
                                  _retry_after(exc.headers), _safe_headers(exc.headers))
            try:
                raw_error = exc.read(65_537)
                error = json.loads(raw_error).get("error") if len(raw_error) <= 65_536 else None
            except (OSError, ValueError, TypeError, AttributeError):
                raw_error, error = b"", None
            detail = None
            if isinstance(error, dict):
                from ora.retrieval.redact import redact_text

                detail = {key: redact_text(value)[0][:2048] for key in ("type", "code", "message")
                          if isinstance(value := error.get(key), str)}
            return replace(failed, error_kind=_tool_failure_kind(prepared, detail) or failed.error_kind,
                           provider_error=detail,
                           response_sha256=hashlib.sha256(raw_error).hexdigest() if raw_error else None)
        except (urllib.error.URLError, TimeoutError, OSError):
            return self._failed(prepared, "transport_error", started)
        if len(raw) > MAX_RESPONSE_BYTES:
            return self._failed(prepared, "response_too_large", started, 502, headers=headers)
        if prepared.body["stream"]:
            return self.decode_stream(prepared, raw, started=started, headers=headers)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            return replace(self._failed(prepared, "invalid_json", started, 502, headers=headers),
                           response_sha256=hashlib.sha256(raw).hexdigest(), partial_response=raw)
        return self.decode(prepared, payload, started=started, response_sha256=hashlib.sha256(raw).hexdigest(),
                           headers=headers)

    def decode_stream(self, prepared: PreparedCall, raw: bytes, *,
                      started: float | None = None,
                      headers: dict[str, str] | None = None) -> ModelOutcome:
        """Assemble SSE only after [DONE]; incomplete arguments stay non-executable."""
        digest = hashlib.sha256(raw).hexdigest()
        elapsed = max(0.0, time.monotonic() - started) if started is not None else 0.0
        content = ""
        reasoning = ""
        calls: dict[int, dict[str, Any]] = {}
        finish: str | None = None
        usage: dict[str, Any] | None = None
        raw_usage: Any = None
        model: str | None = None
        response_id: str | None = None
        request_id: str | None = None
        done = False
        malformed = False
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeError:
            lines = []
            malformed = True
        for line in lines:
            if not line.startswith("data:"):
                continue
            datum = line[5:].strip()
            if datum == "[DONE]":
                done = True
                break
            try:
                chunk = json.loads(datum)
                if not isinstance(chunk, dict):
                    raise ValueError("invalid chunk")
                chunk_model = chunk.get("model")
                if chunk_model is not None:
                    if model is not None and chunk_model != model:
                        raise ValueError("model changed in stream")
                    model = chunk_model
                if chunk.get("id") is not None:
                    response_id = chunk["id"]
                x_groq = chunk.get("x_groq")
                if isinstance(x_groq, dict) and isinstance(x_groq.get("id"), str):
                    request_id = x_groq["id"]
                if chunk.get("usage") is not None:
                    raw_usage = chunk["usage"]
                    usage = _usage(chunk["usage"])
                    if usage is None:
                        raise ValueError("invalid usage")
                choices = chunk.get("choices", [])
                if not isinstance(choices, list) or len(choices) > 1:
                    raise ValueError("invalid choices")
                if not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
                    raise ValueError("invalid delta")
                delta = choice["delta"]
                for field_name in ("content", "reasoning"):
                    part = delta.get(field_name)
                    if part is not None and not isinstance(part, str):
                        raise ValueError("invalid text delta")
                    if part:
                        if field_name == "content":
                            content += part
                        else:
                            reasoning += part
                for call in delta.get("tool_calls") or []:
                    if not isinstance(call, dict) or type(call.get("index")) is not int:
                        raise ValueError("invalid tool delta")
                    index = call["index"]
                    saved = calls.setdefault(index, {"id": None, "type": "function",
                                                     "function": {"name": None, "arguments": ""}})
                    if "id" in call:
                        saved["id"] = call["id"]
                    if "type" in call:
                        saved["type"] = call["type"]
                    function = call.get("function")
                    if function is not None:
                        if not isinstance(function, dict):
                            raise ValueError("invalid function delta")
                        if "name" in function:
                            saved["function"]["name"] = function["name"]
                        args = function.get("arguments")
                        if args is not None:
                            if not isinstance(args, str):
                                raise ValueError("invalid arguments delta")
                            saved["function"]["arguments"] += args
                if choice.get("finish_reason") is not None:
                    finish = choice["finish_reason"]
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                malformed = True
                break
        if not done or malformed or finish is None or usage is None:
            return ModelOutcome("partial", prepared.call_id, prepared.attempt_id,
                                prepared.request_sha256, digest, response_id, request_id,
                                model if isinstance(model, str) else None, finish,
                                content or None, (), reasoning or None,
                                "truncated" if reasoning else "unknown",
                                usage or {}, headers or {},
                                {"wall_seconds": elapsed, **{
                                    key: value for key, value in (usage or {}).items()
                                    if key.endswith("_time")}},
                                "interrupted_stream" if not done else "invalid_stream",
                                partial_response=raw,
                                cache_observation={"status": "unavailable"})
        payload = {"id": response_id, "model": model, "x_groq": {"id": request_id},
                   "choices": [{"message": {"role": "assistant", "content": content or None,
                                            "reasoning": reasoning or None,
                                            "tool_calls": [calls[index] for index in sorted(calls)]},
                                "finish_reason": finish}], "usage": raw_usage}
        return self.decode(prepared, payload, started=started, response_sha256=digest,
                           headers=headers)

    def decode(self, prepared: PreparedCall, payload: Any, *, started: float | None = None,
               response_sha256: str | None = None,
               headers: dict[str, str] | None = None) -> ModelOutcome:
        """Decode a saved provider fixture without credential lookup or network access."""
        elapsed = max(0.0, time.monotonic() - started) if started is not None else 0.0
        if response_sha256 is None:
            try:
                response_sha256 = _sha(payload)
            except (TypeError, ValueError):
                response_sha256 = None
        if not isinstance(payload, dict):
            return self._failed(prepared, "invalid_response", started, 502)
        returned_model = payload.get("model")
        if returned_model != MODEL:
            return self._failed(prepared, "model_mismatch", started, 502,
                                returned_model=returned_model if isinstance(returned_model, str) else None)
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            return self._failed(prepared, "invalid_response", started, 502)
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            return self._failed(prepared, "invalid_response", started, 502)
        content = message.get("content")
        finish = choice.get("finish_reason")
        if content is not None and not isinstance(content, str) or not isinstance(finish, str):
            return self._failed(prepared, "invalid_response", started, 502)
        reasoning = message.get("reasoning")
        if reasoning is not None and not isinstance(reasoning, str):
            return self._failed(prepared, "invalid_reasoning", started, 502)
        reasoning_status = (
            "truncated" if finish == "length" and reasoning else
            "unknown" if finish == "length" else
            "returned" if reasoning else "absent"
        )
        usage = _usage(payload.get("usage"))
        if usage is None:
            return self._failed(prepared, "invalid_usage", started, 502)
        cache_observation = observe_groq_cache(payload.get("usage"))
        x_groq = payload.get("x_groq")
        request_id = x_groq.get("id") if isinstance(x_groq, dict) else None
        if not isinstance(request_id, str):
            request_id = None
        response_id = payload.get("id") if isinstance(payload.get("id"), str) else None

        def failed_response(kind: str) -> ModelOutcome:
            base = self._failed(prepared, kind, started, 502, headers=headers)
            return replace(base, response_sha256=response_sha256,
                           response_id=response_id, provider_request_id=request_id,
                           returned_model=MODEL, finish_reason=finish, content=content,
                           reasoning=reasoning, reasoning_status=reasoning_status,
                           usage=usage, timings={"wall_seconds": elapsed, **{
                               key: value for key, value in usage.items() if key.endswith("_time")
                           }}, provider_metadata=metadata,
                           cache_observation=cache_observation)

        # Browser search is server-side. Preserve any provider-returned metadata
        # without treating it as a locally executable tool call or source record.
        metadata = None
        if {"type": "browser_search"} in prepared.body.get("tools", []):
            metadata = {
                "response": {key: value for key, value in payload.items()
                             if key not in {"choices", "usage"}},
                "choice": {key: value for key, value in choice.items()
                           if key not in {"message", "finish_reason"}},
                "message": {key: value for key, value in message.items()
                            if key not in {"role", "content", "reasoning", "tool_calls"}},
            }

        # A length cutoff can interrupt JSON tool arguments. Keep the visible
        # response for continuation, but never expose a cutoff call to execution.
        if finish == "length":
            calls = ()
        else:
            calls_raw = message.get("tool_calls") or []
            if not isinstance(calls_raw, list):
                return failed_response("invalid_tool_calls")
            try:
                calls = tuple(_function_call(call) for call in calls_raw)
            except ModelPreflightError:
                return failed_response("invalid_tool_calls")
            if len(calls) > 1 or len({call["id"] for call in calls}) != len(calls):
                return failed_response("unsupported_parallel_tool_calls")
        if prepared.phase == "final" and calls:
            return failed_response("unexpected_tool_call")
        if finish == "length":
            status: Status = "partial"
        elif calls and finish == "tool_calls" and prepared.phase == "collect":
            status = "tool_requested"
        elif not calls and finish == "stop" and content:
            status = "complete"
        else:
            status = "partial"
        if prepared.phase == "final" and status == "complete":
            try:
                final = json.loads(content or "")
                schema = prepared.body["response_format"]["json_schema"]["schema"]
                _validate_subset(schema, final)
            except (ValueError, TypeError, KeyError):
                status = "partial"
        return ModelOutcome(status, prepared.call_id, prepared.attempt_id, prepared.request_sha256,
                            response_sha256, response_id, request_id, MODEL, finish, content, calls,
                            reasoning, reasoning_status, usage, headers or {},
                            {"wall_seconds": elapsed, **{key: value for key, value in usage.items()
                                                          if key.endswith("_time")}},
                            "invalid_final_schema" if prepared.phase == "final" and status == "partial"
                            and finish == "stop" else None,
                            provider_metadata=metadata,
                            cache_observation=cache_observation)

    def _failed(self, prepared: PreparedCall, kind: str, started: float | None,
                http_status: int | None = None, retry_after: float | None = None,
                headers: dict[str, str] | None = None,
                returned_model: str | None = None) -> ModelOutcome:
        elapsed = max(0.0, time.monotonic() - started) if started is not None else 0.0
        return ModelOutcome("failed", prepared.call_id, prepared.attempt_id,
                            prepared.request_sha256, None, None, None, returned_model, None,
                            None, (), None, "unknown", {}, headers or {},
                            {"wall_seconds": elapsed}, kind, http_status, retry_after)


def _safe_headers(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    allowed = ("x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
               "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
               "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens", "retry-after",
               "x-request-id")
    return {name: str(headers.get(name)) for name in allowed if headers.get(name) is not None}


def _usage(raw: Any) -> dict[str, Any] | None:
    """Retain reported totals and valid optional breakdowns without estimating them."""
    return normalize_groq_usage(raw)


def _validate_subset(schema: dict[str, Any], value: Any) -> None:
    """Minimal strict-mode fixture check; provider validates the full schema."""
    kind = schema.get("type")
    if isinstance(kind, list):
        if value is None and "null" in kind:
            return
        kind = next(item for item in kind if item != "null")
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["properties"]):
            raise ValueError("object shape mismatch")
        for key, child in schema["properties"].items():
            _validate_subset(child, value[key])
    elif kind == "array":
        if not isinstance(value, list):
            raise ValueError("array shape mismatch")
        if "items" in schema:
            for child in value:
                _validate_subset(schema["items"], child)
    elif kind == "string" and not isinstance(value, str):
        raise ValueError("string shape mismatch")
    elif kind == "integer" and (type(value) is not int):
        raise ValueError("integer shape mismatch")
    elif kind == "boolean" and type(value) is not bool:
        raise ValueError("boolean shape mismatch")
    elif kind == "number" and (type(value) not in {int, float} or not math.isfinite(value)):
        raise ValueError("number shape mismatch")
    elif kind == "null" and value is not None:
        raise ValueError("null shape mismatch")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("enum mismatch")
