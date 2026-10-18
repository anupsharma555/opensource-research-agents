"""One explicit Groq chat completion for the local research host.

The host owns the tool loop and budget. This adapter never retries a request,
executes a tool, or loads a credential during import or cost estimation.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ora.credentials import GROQ_CREDENTIAL_ENV_NAMES, GroqCredentialProfiles

MODEL = "openai/gpt-oss-120b"
URL = "https://api.groq.com/openai/v1/chat/completions"
USER_AGENT = "OpenSourceResearchAgents/0.1 live-provider"
INPUT_USD_PER_M = 0.15
OUTPUT_USD_PER_M = 0.60
MESSAGE_OVERHEAD_TOKENS = 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ProviderError(RuntimeError):
    """A safe, user-facing provider failure without response or credential text."""

    def __init__(
        self, message: str, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class ConfigurationDrift(ProviderError):
    """The saved request configuration changed before any provider dispatch."""


class CredentialUnavailable(ProviderError):
    """The local credential was rejected before any provider dispatch."""


def _function_call(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProviderError("Groq returned an invalid tool call.", status=502)
    function = value.get("function")
    if (
        not isinstance(value.get("id"), str)
        or not value["id"]
        or value.get("type") != "function"
        or not isinstance(function, dict)
        or not isinstance(function.get("name"), str)
        or not function["name"]
        or not isinstance(function.get("arguments"), str)
    ):
        raise ProviderError("Groq returned an invalid tool call.", status=502)
    return {
        "id": value["id"],
        "type": "function",
        "function": {"name": function["name"], "arguments": function["arguments"]},
    }


def _messages_for_request(messages: list[dict]) -> list[dict[str, object]]:
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a nonempty list")
    cleaned: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("each message must be an object")
        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError("message has an unsupported role")
        if not isinstance(content, str) and not (role == "assistant" and content is None):
            raise ValueError("message content must be text or null for an assistant tool call")
        if role == "assistant" and content is None and not message.get("tool_calls"):
            raise ValueError("assistant null content requires tool_calls")
        entry: dict[str, object] = {"role": role, "content": content}
        if role == "assistant" and message.get("tool_calls") is not None:
            calls = message["tool_calls"]
            if not isinstance(calls, list) or not calls:
                raise ValueError("assistant tool_calls must be a nonempty list")
            # Keep the call IDs and function arguments unchanged for the next model turn.
            entry["tool_calls"] = [_function_call(call) for call in calls]
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("tool message needs a tool_call_id")
            entry["tool_call_id"] = call_id
            if "name" in message:
                if not isinstance(message["name"], str) or not message["name"]:
                    raise ValueError("tool message name must be nonempty text")
                entry["name"] = message["name"]
        cleaned.append(entry)
    return cleaned


def _retry_after(headers: Any) -> float | None:
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if 0 <= seconds < float("inf") else None


class GroqProvider:
    model = MODEL

    def __init__(self, root: Path, max_completion_tokens: int = 8192) -> None:
        if (
            isinstance(max_completion_tokens, bool)
            or not isinstance(max_completion_tokens, int)
            or not 1 <= max_completion_tokens <= 65536
        ):
            raise ValueError("max_completion_tokens must be between 1 and 65536")
        self.root = Path(root)
        self.credential_profiles = GroqCredentialProfiles(self.root)
        self.max_completion_tokens = max_completion_tokens

    def _key(self, *, profile: str = "primary") -> str:
        def usable(value: str) -> bool:
            return bool(value) and all(33 <= ord(character) <= 126 for character in value)

        name = GROQ_CREDENTIAL_ENV_NAMES.get(profile)
        if name is None:
            raise CredentialUnavailable("Groq credential profile is unavailable.", status=503)
        value = os.environ.get(name, "").strip()
        if value:
            if usable(value):
                return value
            raise CredentialUnavailable("Groq credential is invalid.", status=503)
        try:
            lines = (self.root / ".env").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            lines = []
        for line in lines:
            field_name, separator, value = line.partition("=")
            if separator and field_name.strip() == name:
                value = value.strip().strip('"').strip("'")
                if value:
                    if usable(value):
                        return value
                    raise CredentialUnavailable("Groq credential is invalid.", status=503)
        raise CredentialUnavailable("Groq credential is unavailable.", status=503)

    def _request_body(self, messages: list[dict], tools: list[dict] | None) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self.model,
            "messages": _messages_for_request(messages),
            "max_completion_tokens": self.max_completion_tokens,
            "reasoning_effort": "high",
            "include_reasoning": False,
            "stream": False,
        }
        if tools is not None:
            if not isinstance(tools, list) or not all(isinstance(tool, dict) for tool in tools):
                raise ValueError("tools must be a list of objects")
            if tools:
                if any(
                    tool.get("type") != "function"
                    or not isinstance(tool.get("function"), dict)
                    or not isinstance(tool["function"].get("name"), str)
                    or not tool["function"]["name"]
                    for tool in tools
                ):
                    raise ValueError("only named local function tools are supported")
                body["tools"] = tools
                body["tool_choice"] = "auto"
                body["parallel_tool_calls"] = False
        return body

    def effective_configuration(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict[str, object]:
        """Return the nonsecret request settings before any credential or network use."""
        return self._effective_configuration_from_body(self._request_body(messages, tools))

    @staticmethod
    def _effective_configuration_from_body(body: dict[str, object]) -> dict[str, object]:
        def digest(value: object) -> str:
            encoded = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            return hashlib.sha256(encoded).hexdigest()

        return {
            "provider": "groq",
            "endpoint": URL,
            "model_id": body["model"],
            "settings": {
                key: value
                for key, value in body.items()
                if key not in {"model", "messages", "tools", "reasoning_effort"}
            } | {
                "request_messages_sha256": digest(body["messages"]),
                "request_tools_sha256": digest(body.get("tools", [])),
            },
            "reasoning_settings": {"reasoning_effort": body["reasoning_effort"]},
        }

    def estimate_max_cost(self, messages: list[dict], tools: list[dict] | None = None) -> float:
        """Conservative byte-based admission estimate at documented list rates.

        This is a planning estimate, not a provider charge or a hard token bound.
        """
        body = self._request_body(messages, tools)
        prompt = {"messages": body["messages"], "tools": body.get("tools", [])}
        try:
            prompt_bytes = len(json.dumps(prompt, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError):
            raise ValueError("messages and tools must be JSON serializable") from None
        input_estimate = prompt_bytes + MESSAGE_OVERHEAD_TOKENS
        return (
            input_estimate * INPUT_USD_PER_M
            + self.max_completion_tokens * OUTPUT_USD_PER_M
        ) / 1_000_000

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        expected_configuration: dict[str, object] | None = None,
    ) -> dict:
        if not self.credential_profiles.is_enabled("primary"):
            raise CredentialUnavailable("Primary credential paused by operator.", status=503)
        body = self._request_body(messages, tools)
        if (
            expected_configuration is not None
            and self._effective_configuration_from_body(body) != expected_configuration
        ):
            raise ConfigurationDrift("Groq settings changed before dispatch.")
        try:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise ValueError("messages and tools must be JSON serializable") from None
        key = self._key()
        request = urllib.request.Request(
            URL,
            data=encoded,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise ProviderError(
                f"Groq request failed with HTTP {exc.code}; no retry was made.",
                status=exc.code,
                retry_after=_retry_after(exc.headers),
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProviderError("Groq request could not be completed; no retry was made.") from None
        elapsed = time.monotonic() - started
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("Groq response exceeded the supported size.", status=502)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("model") != self.model:
                raise ProviderError("Groq returned a different or missing model.", status=502)
            choices = payload.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ProviderError("Groq returned an invalid completion.", status=502)
            choice = choices[0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ProviderError("Groq returned an invalid completion.", status=502)
            message = choice["message"]
            content = message.get("content")
            calls = message.get("tool_calls") or []
            if content is not None and not isinstance(content, str):
                raise ProviderError("Groq returned invalid message content.", status=502)
            if not isinstance(calls, list):
                raise ProviderError("Groq returned invalid tool calls.", status=502)
            tool_calls = [_function_call(call) for call in calls]
            if len({call["id"] for call in tool_calls}) != len(tool_calls):
                raise ProviderError("Groq returned duplicate tool call IDs.", status=502)
            finish_reason = choice.get("finish_reason")
            if not isinstance(finish_reason, str):
                raise ProviderError("Groq returned no finish reason.", status=502)
            usage = payload.get("usage")
            if usage is None:
                raise ProviderError("Groq returned no token usage.", status=502)
            # Import after module initialization: the selected model adapter
            # also imports this legacy provider for shared transport constants.
            from ora.models.usage import normalize_groq_usage, observe_groq_cache

            counts = normalize_groq_usage(usage)
            if counts is None:
                raise ProviderError("Groq returned invalid token usage.", status=502)
            cache_observation = observe_groq_cache(usage)
        except (UnicodeError, json.JSONDecodeError):
            raise ProviderError("Groq returned invalid JSON.", status=502) from None
        return {
            "content": content,
            "tool_calls": tool_calls,
            "usage": counts,
            "cache_observation": cache_observation,
            "model": self.model,
            "elapsed_seconds": elapsed,
            "finish_reason": finish_reason,
            "estimated_usd": (
                counts["prompt_tokens"] * INPUT_USD_PER_M
                + counts["completion_tokens"] * OUTPUT_USD_PER_M
            ) / 1_000_000,
        }
