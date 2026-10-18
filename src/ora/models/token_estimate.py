"""Offline, versioned token estimates for exact Groq GPT-OSS request bytes.

The tokenizer count is an empirical admission estimate, not the
provider's private chat-rendering count. Unsupported requests retain the
historical byte estimate. No runtime path downloads vocabulary data or writes
a tokenizer cache.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from ora.live.provider import MODEL

LEGACY_BASIS = "ora.request_bytes_div4_times1_25.v1"
TOKENIZER_BASIS_V1 = "ora.o200k_harmony_ordinary_canonical_json_plus128.v1"
TOKENIZER_BASIS_V2 = "ora.o200k_harmony_ordinary_canonical_json.v2"
TOKENIZER_BASIS = "ora.o200k_harmony_ordinary_canonical_json.v3"
ASSET_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
ASSET_PATH = Path(__file__).parent / "assets" / "o200k_base.tiktoken"
_V1_JSON_ALLOWANCE = 128
_ENCODING: Any | None = None
_ENCODING_LOCK = Lock()


@dataclass(frozen=True)
class TokenEstimate:
    tokens: int
    basis: str
    asset_sha256: str | None
    fallback_reason: str | None


class TokenEstimateUnavailable(ValueError):
    """An explicitly selected saved estimate cannot be reproduced locally."""

    def __init__(self, reason: str):
        super().__init__(f"Token estimate unavailable: {reason}")
        self.reason = reason


def _legacy(raw: bytes, reason: str | None) -> TokenEstimate:
    return TokenEstimate(
        tokens=(len(raw) * 5 + 15) // 16, basis=LEGACY_BASIS,
        asset_sha256=None, fallback_reason=reason,
    )


def _ordinary_request_shape(raw: bytes, *, historical_output_cap: bool) -> str | None:
    try:
        body = json.loads(raw)
        encoded = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (UnicodeError, ValueError, TypeError, OverflowError):
        return "invalid_request_json"
    if encoded != raw:
        return "noncanonical_request_bytes"
    if type(body) is not dict or set(body) != {
        "model", "messages", "reasoning_effort", "include_reasoning",
        "max_completion_tokens", "stream", "tools", "tool_choice",
        "parallel_tool_calls",
    }:
        return "unsupported_request_shape"
    if body["model"] != MODEL:
        return "selected_model_mismatch"
    if (body["reasoning_effort"] != "high" or body["include_reasoning"] is not True
            or body["stream"] is not False
            or historical_output_cap and body["max_completion_tokens"] != 8192):
        return "unsupported_reasoning_or_stream"
    # Output allowance does not change input serialization. Count the exact cap
    # bytes within the adapter's existing bounds; v1/v2 retain their old gate.
    if not historical_output_cap and (
        type(body["max_completion_tokens"]) is not int
        or not 1 <= body["max_completion_tokens"] <= 65536
    ):
        return "unsupported_completion_budget"
    if body["tool_choice"] != "auto" or body["parallel_tool_calls"] is not False:
        return "unsupported_tool_choice"
    tools = body["tools"]
    if type(tools) is not list or not 1 <= len(tools) <= 128:
        return "unsupported_tools"
    names: set[str] = set()
    for tool in tools:
        if type(tool) is not dict or set(tool) != {"type", "function"}:
            return "native_or_unfamiliar_tool"
        function = tool["function"]
        if (tool["type"] != "function" or type(function) is not dict
                or set(function) not in (
                    {"name", "parameters"}, {"name", "description", "parameters"}
                ) or type(function.get("name")) is not str
                or not function["name"] or type(function.get("parameters")) is not dict
                or "description" in function
                and type(function["description"]) is not str
                or function["name"] == "browser_search"
                or function["name"] in names):
            return "native_or_unfamiliar_tool"
        names.add(function["name"])
    messages = body["messages"]
    if (type(messages) is not list or len(messages) < 2
            or type(messages[0]) is not dict
            or messages[0].get("role") != "developer"):
        return "unsupported_messages"
    seen_user = False
    for message in messages:
        if type(message) is not dict:
            return "unsupported_messages"
        role = message.get("role")
        if type(role) is not str:
            return "unsupported_messages"
        if role in {"developer", "user"}:
            if set(message) != {"role", "content"} or type(message["content"]) is not str:
                return "unsupported_messages"
            seen_user |= role == "user"
        elif role == "tool":
            if (set(message) != {"role", "name", "tool_call_id", "content"}
                    or any(type(message[key]) is not str for key in (
                        "name", "tool_call_id", "content"
                    ))):
                return "unsupported_messages"
        elif role == "assistant":
            if (set(message) != {"role", "content", "tool_calls"}
                    or message["content"] is not None
                    or type(message["tool_calls"]) is not list
                    or not message["tool_calls"]):
                return "unsupported_messages"
            for call in message["tool_calls"]:
                if (type(call) is not dict or set(call) != {"id", "type", "function"}
                        or call["type"] != "function" or type(call["id"]) is not str
                        or type(call["function"]) is not dict
                        or set(call["function"]) != {"name", "arguments"}
                        or any(type(value) is not str for value in call["function"].values())):
                    return "unsupported_messages"
        else:
            return "unsupported_messages"
    return None if seen_user else "unsupported_messages"


def _build_encoding(asset: bytes) -> Any:
    try:
        import tiktoken
    except ImportError as exc:
        raise TokenEstimateUnavailable("tokenizer_dependency_unavailable") from exc
    # Equivalent to the pinned OpenAI loader, with verified package bytes in
    # memory rather than its network/cache-backed URL loader.
    try:
        ranks = {
            base64.b64decode(token): int(rank)
            for line in asset.splitlines() if line
            for token, rank in (line.split(),)
        }
        pattern = "|".join((
            r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
            r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
            r"""\p{N}{1,3}""",
            r""" ?[^\s\p{L}\p{N}]+[\r\n/]*""",
            r"""\s*[\r\n]+""",
            r"""\s+(?!\S)""",
            r"""\s+""",
        ))
        special = {
            "<|endoftext|>": 199999, "<|endofprompt|>": 200018,
            "<|startoftext|>": 199998,
            "<|reserved_200000|>": 200000,
            "<|reserved_200001|>": 200001,
            "<|return|>": 200002,
            "<|constrain|>": 200003,
            "<|reserved_200004|>": 200004,
            "<|channel|>": 200005,
            "<|start|>": 200006,
            "<|end|>": 200007,
            "<|message|>": 200008,
            "<|reserved_200009|>": 200009,
            "<|reserved_200010|>": 200010,
            "<|reserved_200011|>": 200011,
            "<|call|>": 200012,
        } | {f"<|reserved_{index}|>": index for index in range(200013, 201088)}
        return tiktoken.Encoding(
            name="o200k_harmony_local", pat_str=pattern,
            mergeable_ranks=ranks, special_tokens=special,
        )
    except (ValueError, TypeError, RuntimeError) as exc:
        raise TokenEstimateUnavailable("tokenizer_initialization_failed") from exc


def _verified_encoding() -> Any:
    try:
        asset = ASSET_PATH.read_bytes()
    except FileNotFoundError as exc:
        raise TokenEstimateUnavailable("asset_missing") from exc
    except OSError as exc:
        raise TokenEstimateUnavailable("asset_unreadable") from exc
    if hashlib.sha256(asset).hexdigest() != ASSET_SHA256:
        raise TokenEstimateUnavailable("asset_hash_mismatch")
    global _ENCODING
    if _ENCODING is None:
        with _ENCODING_LOCK:
            if _ENCODING is None:
                _ENCODING = _build_encoding(asset)
    return _ENCODING


def estimate_request_tokens(raw: bytes, *, basis: str = "auto") -> TokenEstimate:
    """Estimate exact canonical request bytes; never obtain tokenizer data online.

    An explicit saved tokenizer basis is strict: failing closed preserves its
    original accounting instead of silently relabeling it as legacy on resume.
    """
    if type(raw) is not bytes:
        raise TypeError("Exact request bytes are required")
    if type(basis) is not str or basis not in {
        "auto", LEGACY_BASIS, TOKENIZER_BASIS_V1, TOKENIZER_BASIS_V2, TOKENIZER_BASIS,
    }:
        raise ValueError("Unknown token estimate basis")
    if basis == LEGACY_BASIS:
        return _legacy(raw, None)
    strict = basis != "auto"
    reason = _ordinary_request_shape(
        raw, historical_output_cap=basis in {TOKENIZER_BASIS_V1, TOKENIZER_BASIS_V2},
    )
    if reason:
        if strict:
            raise TokenEstimateUnavailable(reason)
        return _legacy(raw, reason)
    try:
        encoding = _verified_encoding()
        tokens = len(encoding.encode_ordinary(raw.decode("utf-8")))
        if basis == TOKENIZER_BASIS_V1:
            tokens += _V1_JSON_ALLOWANCE
    except TokenEstimateUnavailable as exc:
        if strict:
            raise
        return _legacy(raw, exc.reason)
    except (UnicodeError, ValueError, RuntimeError) as exc:
        if strict:
            raise TokenEstimateUnavailable("tokenization_failed") from exc
        return _legacy(raw, "tokenization_failed")
    return TokenEstimate(tokens, TOKENIZER_BASIS if basis == "auto" else basis, ASSET_SHA256, None)


def encode_context_token_ids(value: str) -> tuple[int, ...]:
    """Encode local ordinary token IDs for a text fragment without network I/O."""
    if type(value) is not str:
        raise TypeError("Context text is required")
    try:
        return tuple(_verified_encoding().encode_ordinary(value))
    except TokenEstimateUnavailable:
        raise
    except (UnicodeError, ValueError, RuntimeError) as exc:
        raise TokenEstimateUnavailable("tokenization_failed") from exc


def estimate_context_tokens(value: str) -> int:
    """Count a packer text fragment without the whole-request allowance."""
    return len(encode_context_token_ids(value))
