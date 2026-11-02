"""Offline, versioned token estimates for pinned Groq request bytes.

GPT-OSS uses a pinned local tokenizer for an empirical admission estimate.
Supported Qwen collection requests use a pinned local tokenizer and public
chat template, including sequential tool history. Saved legacy and unsupported
Qwen requests retain a separately labelled UTF-8 byte surrogate. Neither
count is the provider's private chat-rendering count. No runtime path downloads
vocabulary data or writes a tokenizer cache.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from ora.live.provider import MODEL

from .profiles import QWEN_MODEL, QWEN_REASONING_EFFORTS

LEGACY_BASIS = "ora.request_bytes_div4_times1_25.v1"
TOKENIZER_BASIS_V1 = "ora.o200k_harmony_ordinary_canonical_json_plus128.v1"
TOKENIZER_BASIS_V2 = "ora.o200k_harmony_ordinary_canonical_json.v2"
TOKENIZER_BASIS_V3 = "ora.o200k_harmony_ordinary_canonical_json.v3"
TOKENIZER_BASIS = "ora.o200k_harmony_ordinary_canonical_json.v4"
QWEN_SURROGATE_BASIS = "ora.qwen38_27b_utf8_bytes_plus128_per_item_surrogate.v1"
QWEN_TEMPLATE_BASIS = "ora.qwen38_27b_official_chat_template_tokenizers.v1"
QWEN_TEMPLATE_LOW_BASIS = "ora.qwen38_27b_official_chat_template_low_tokenizers.v1"
QWEN_TOKENIZER_SHA256 = "0997f410c57a1f4e53b09e4be8f4a172d90edd9564368fb0847030937229b9f3"
QWEN_TEMPLATE_SHA256 = "49d20d090bd09c0030f3f12d5e05c5e2c57c6864825ef2f640ca73e7578dfe24"
QWEN_ASSET_SHA256 = "9dd98d59cd4c0817e12eba3827913f7ffd1cf874b5a7b2f908a79f052a02303e"
QWEN_TOKENIZER_PATH = Path(__file__).parent / "assets" / "qwen38_27b_tokenizer.json"
QWEN_TEMPLATE_PATH = Path(__file__).parent / "assets" / "qwen38_27b_chat_template.jinja"
ASSET_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
ASSET_PATH = Path(__file__).parent / "assets" / "o200k_base.tiktoken"
_V1_JSON_ALLOWANCE = 128
_ENCODING: Any | None = None
_ENCODING_LOCK = Lock()
_QWEN_ENGINE: Any | None = None
_QWEN_ENGINE_KEY: tuple[Path, Path, str] | None = None
_QWEN_ENGINE_LOCK = Lock()


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


def _ordinary_request_shape(raw: bytes, *, historical_output_cap: bool,
                            allow_medium_effort: bool) -> str | None:
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
    efforts = ("medium", "high") if allow_medium_effort else ("high",)
    if (body["reasoning_effort"] not in efforts or body["include_reasoning"] is not True
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


def _qwen_request_shape(raw: bytes) -> tuple[dict[str, Any] | None, str | None]:
    """Only adapter-shaped Qwen requests receive the distinct surrogate basis."""
    try:
        body = json.loads(raw)
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError, OverflowError):
        return None, "invalid_request_json"
    if encoded != raw:
        return None, "noncanonical_request_bytes"
    if type(body) is not dict or body.get("model") != QWEN_MODEL:
        return None, "selected_model_mismatch"
    common = {"model", "messages", "reasoning_effort", "reasoning_format",
              "max_completion_tokens", "stream"}
    collect = common | {"tools", "tool_choice", "parallel_tool_calls"}
    final = common | {"response_format"}
    if set(body) not in (collect, collect | {"stream_options"}, final):
        return None, "unsupported_request_shape"
    if (body["reasoning_effort"] not in QWEN_REASONING_EFFORTS
            or body["reasoning_format"] != "parsed"
            or type(body["stream"]) is not bool
            or type(body["max_completion_tokens"]) is not int
            or not 1 <= body["max_completion_tokens"] <= 16384):
        return None, "unsupported_reasoning_or_stream"
    messages = body["messages"]
    if (type(messages) is not list or len(messages) < 2
            or not all(type(item) is dict and item.get("role") in {
                "user", "assistant", "tool", "system"
            } for item in messages)
            or messages[0].get("role") != "system"
            or any(item.get("role") == "system" for item in messages[1:])
            or any("reasoning" in item for item in messages)):
        return None, "unsupported_messages"
    if set(body) == final:
        if body["stream"] is not False or type(body["response_format"]) is not dict:
            return None, "unsupported_request_shape"
    else:
        tools = body["tools"]
        if (type(tools) is not list or not 1 <= len(tools) <= 128
                or any(type(item) is not dict or item.get("type") != "function"
                       for item in tools)
                or body["tool_choice"] != "auto"
                or body["parallel_tool_calls"] is not False
                or ("stream_options" in body) != body["stream"]
                or body.get("stream_options", {"include_usage": True})
                != {"include_usage": True}):
            return None, "unsupported_tools"
    return body, None


def _strict_json_object(value: str) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate_tool_argument_key")
            result[key] = item
        return result

    def nonfinite(_value: str) -> None:
        raise ValueError("nonfinite_tool_argument")

    result = json.loads(value, object_pairs_hook=unique_pairs,
                        parse_constant=nonfinite)
    if type(result) is not dict:
        raise ValueError("tool_arguments_not_object")

    def require_finite(item: Any) -> None:
        if type(item) is float and not math.isfinite(item):
            raise ValueError("nonfinite_tool_argument")
        if type(item) is dict:
            for child in item.values():
                require_finite(child)
        elif type(item) is list:
            for child in item:
                require_finite(child)

    require_finite(result)
    return result


def _qwen_template_messages(
    messages: list[dict[str, Any]], *, allow_incomplete_tool: bool = False,
) -> list[dict[str, Any]]:
    """Validate supported adapter history and normalize its copied tool arguments."""
    if not messages or set(messages[0]) != {"role", "content"} or (
        messages[0]["role"] != "system" or type(messages[0]["content"]) is not str
    ):
        raise ValueError("unsupported_qwen_messages")
    result = deepcopy(messages)
    seen_user = False
    pending: str | None = None
    used: set[str] = set()
    for message in result[1:]:
        role = message.get("role")
        if pending is not None and role != "tool":
            raise ValueError("unmatched_qwen_tool_call")
        if role == "user":
            if set(message) != {"role", "content"} or type(message["content"]) is not str:
                raise ValueError("unsupported_qwen_messages")
            seen_user = True
        elif role == "tool":
            if (set(message) not in ({"role", "content", "tool_call_id"},
                                     {"role", "content", "tool_call_id", "name"})
                    or type(message.get("content")) is not str
                    or type(message.get("tool_call_id")) is not str
                    or not message["tool_call_id"]
                    or "name" in message and type(message["name"]) is not str):
                raise ValueError("unsupported_qwen_messages")
            if message["tool_call_id"] != pending:
                raise ValueError("unmatched_qwen_tool_result")
            pending = None
        elif role == "assistant":
            calls = message.get("tool_calls")
            if (set(message) != {"role", "content", "tool_calls"}
                    or message["content"] is not None
                    and type(message["content"]) is not str
                    or type(calls) is not list or len(calls) != 1):
                raise ValueError("unsupported_qwen_messages")
            call = calls[0]
            if (type(call) is not dict or set(call) != {"id", "type", "function"}
                    or call["type"] != "function" or type(call["id"]) is not str
                    or type(call["function"]) is not dict
                    or set(call["function"]) != {"name", "arguments"}
                    or type(call["function"]["name"]) is not str
                    or not call["function"]["name"]
                    or type(call["function"]["arguments"]) is not str):
                raise ValueError("unsupported_qwen_messages")
            if not call["id"] or call["id"] in used:
                raise ValueError("duplicate_qwen_tool_call")
            used.add(call["id"])
            pending = call["id"]
            call["function"]["arguments"] = _strict_json_object(
                call["function"]["arguments"]
            )
        else:
            raise ValueError("unsupported_qwen_messages")
    if not seen_user:
        raise ValueError("unsupported_qwen_messages")
    if pending is not None and not allow_incomplete_tool:
        raise ValueError("unmatched_qwen_tool_call")
    return result


def _qwen_template_tools(tools: object) -> list[dict[str, Any]]:
    if type(tools) is not list or len(tools) > 128:
        raise ValueError("unsupported_qwen_tools")
    names: set[str] = set()
    for tool in tools:
        if type(tool) is not dict or set(tool) != {"type", "function"} or (
            tool["type"] != "function" or type(tool["function"]) is not dict
        ):
            raise ValueError("unsupported_qwen_tools")
        function = tool["function"]
        if (set(function) not in ({"name", "parameters"},
                                 {"name", "description", "parameters"})
                or type(function.get("name")) is not str or not function["name"]
                or function["name"] in names
                or type(function.get("parameters")) is not dict
                or "description" in function
                and type(function["description"]) is not str):
            raise ValueError("unsupported_qwen_tools")
        names.add(function["name"])
    return tools


def _verified_qwen_engine() -> tuple[Any, Any]:
    # The in-memory engine retains hash-verified bytes. A fresh process checks
    # its local files before strict saved-basis replay; previews reuse the
    # loaded engine instead of reading 13 MB for every fragment.
    key = (QWEN_TOKENIZER_PATH, QWEN_TEMPLATE_PATH, QWEN_ASSET_SHA256)
    global _QWEN_ENGINE, _QWEN_ENGINE_KEY
    if _QWEN_ENGINE is not None and _QWEN_ENGINE_KEY == key:
        return _QWEN_ENGINE
    with _QWEN_ENGINE_LOCK:
        if _QWEN_ENGINE is not None and _QWEN_ENGINE_KEY == key:
            return _QWEN_ENGINE
        try:
            tokenizer_bytes = QWEN_TOKENIZER_PATH.read_bytes()
            template_bytes = QWEN_TEMPLATE_PATH.read_bytes()
        except FileNotFoundError as exc:
            raise TokenEstimateUnavailable("qwen_asset_missing") from exc
        except OSError as exc:
            raise TokenEstimateUnavailable("qwen_asset_unreadable") from exc
        if (hashlib.sha256(tokenizer_bytes).hexdigest() != QWEN_TOKENIZER_SHA256
                or hashlib.sha256(template_bytes).hexdigest() != QWEN_TEMPLATE_SHA256
                or hashlib.sha256(tokenizer_bytes + b"\0" + template_bytes).hexdigest()
                != QWEN_ASSET_SHA256):
            raise TokenEstimateUnavailable("qwen_asset_hash_mismatch")
        try:
            from jinja2 import Environment
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_str(tokenizer_bytes.decode("utf-8"))
            template = Environment(autoescape=False).from_string(
                template_bytes.decode("utf-8")
            )
        except ImportError as exc:
            raise TokenEstimateUnavailable("qwen_tokenizer_dependency_unavailable") from exc
        except (UnicodeError, ValueError, RuntimeError) as exc:
            raise TokenEstimateUnavailable("qwen_tokenizer_initialization_failed") from exc
        _QWEN_ENGINE = tokenizer, template
        _QWEN_ENGINE_KEY = key
    return _QWEN_ENGINE


def _qwen_render_count(body: dict[str, Any], *, allow_incomplete_tool: bool = False) -> int:
    tokenizer, template = _verified_qwen_engine()
    messages = _qwen_template_messages(body["messages"],
                                       allow_incomplete_tool=allow_incomplete_tool)
    tools = _qwen_template_tools(body.get("tools", []))

    def fail(reason: str) -> None:
        raise ValueError(reason)

    # The pinned public template injects one extra directive for low. Medium
    # retains the historical no-extra-line high render exactly. Neither is a
    # claim about Groq's private serialization.
    template_effort = "low" if body.get("reasoning_effort") == "low" else "high"
    rendered = template.render(
        messages=messages, tools=tools,
        add_generation_prompt=True, enable_thinking=True,
        reasoning_effort=template_effort, raise_exception=fail, add_vision_id=False,
        preserve_thinking=False,
    )
    return len(tokenizer.encode(rendered, add_special_tokens=False).ids)


def estimate_qwen_context_tokens(value: str, *, reasoning_effort: str = "medium") -> int:
    """Estimate a canonical context message-list fragment in local Qwen units.

    The actual complete request is checked separately; this fragment guides
    packing and deliberately uses a valid synthetic user prefix for history.
    """
    try:
        if type(value) is not str:
            raise TypeError("context must be text")
        messages = json.loads(value)
        encoded = json.dumps(
            messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
        if type(messages) is not list or encoded != value:
            raise ValueError("noncanonical_context_messages")
        if reasoning_effort not in {"medium", "low"}:
            raise ValueError("unsupported_qwen_fragment_effort")
        base = [{"role": "system", "content": ""}, {"role": "user", "content": ""}]
        return _qwen_render_count({
            "messages": [*base, *messages], "reasoning_effort": reasoning_effort,
        }) - _qwen_render_count({
            "messages": base, "reasoning_effort": reasoning_effort,
        }
        )
    except TokenEstimateUnavailable:
        raise
    except (TypeError, KeyError, ValueError, RuntimeError) as exc:
        raise TokenEstimateUnavailable("qwen_context_tokenization_failed") from exc


def estimate_qwen_stage_tokens(body: dict[str, Any]) -> int:
    """Count a staged prefix after making pre-user prefixes renderable.

    Intermediate stages are diagnostic allocations. A complete request is
    counted without insertion and matches the final pinned estimate exactly.
    """
    messages = body.get("messages")
    if type(messages) is not list:
        raise TokenEstimateUnavailable("qwen_stage_messages_invalid")
    staged = {**body, "messages": list(messages)}
    if not staged["messages"]:
        staged["messages"].append({"role": "system", "content": ""})
    if not any(item.get("role") == "user" for item in staged["messages"]):
        staged["messages"].append({"role": "user", "content": ""})
    try:
        return _qwen_render_count(staged, allow_incomplete_tool=True)
    except TokenEstimateUnavailable:
        raise
    except (TypeError, KeyError, ValueError, RuntimeError) as exc:
        raise TokenEstimateUnavailable("qwen_stage_tokenization_failed") from exc


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
        "auto", LEGACY_BASIS, TOKENIZER_BASIS_V1, TOKENIZER_BASIS_V2,
        TOKENIZER_BASIS_V3, TOKENIZER_BASIS, QWEN_SURROGATE_BASIS,
        QWEN_TEMPLATE_BASIS, QWEN_TEMPLATE_LOW_BASIS,
    }:
        raise ValueError("Unknown token estimate basis")
    if basis == LEGACY_BASIS:
        return _legacy(raw, None)
    qwen_selected = False
    if basis == "auto" and b'"qwen/qwen3.8-27b"' in raw:
        try:
            candidate = json.loads(raw)
            qwen_selected = type(candidate) is dict and candidate.get("model") == QWEN_MODEL
        except (UnicodeError, ValueError, TypeError):
            pass
    if basis in {
        QWEN_SURROGATE_BASIS, QWEN_TEMPLATE_BASIS, QWEN_TEMPLATE_LOW_BASIS,
    } or qwen_selected:
        body, reason = _qwen_request_shape(raw)
        if reason:
            if basis != "auto":
                raise TokenEstimateUnavailable(reason)
            return _legacy(raw, reason)
        assert body is not None
        surrogate = TokenEstimate(len(raw) + 128 * (
            len(body["messages"]) + len(body.get("tools", [])) + 1
        ), QWEN_SURROGATE_BASIS, None, "qwen_tokenizer_unavailable")
        if basis == QWEN_SURROGATE_BASIS:
            return surrogate
        # The public chat template does not consume response_format. Keep the
        # earlier conservative basis for strict-final output until its provider
        # serialization is independently qualified.
        effort = body["reasoning_effort"]
        if ("response_format" in body or effort not in {"medium", "low"}
                or basis == QWEN_TEMPLATE_BASIS and effort != "medium"
                or basis == QWEN_TEMPLATE_LOW_BASIS and effort != "low"):
            if basis in {QWEN_TEMPLATE_BASIS, QWEN_TEMPLATE_LOW_BASIS}:
                raise TokenEstimateUnavailable("qwen_template_unsupported_settings")
            return TokenEstimate(surrogate.tokens, surrogate.basis, None,
                                 "qwen_template_unsupported_settings")
        try:
            tokens = _qwen_render_count(body)
        except TokenEstimateUnavailable as exc:
            if basis in {QWEN_TEMPLATE_BASIS, QWEN_TEMPLATE_LOW_BASIS}:
                raise
            return TokenEstimate(surrogate.tokens, surrogate.basis, None, exc.reason)
        except (TypeError, KeyError, ValueError, RuntimeError) as exc:
            if basis in {QWEN_TEMPLATE_BASIS, QWEN_TEMPLATE_LOW_BASIS}:
                raise TokenEstimateUnavailable("qwen_template_unsupported_shape") from exc
            return TokenEstimate(surrogate.tokens, surrogate.basis, None,
                                 "qwen_template_unsupported_shape")
        native_basis = QWEN_TEMPLATE_LOW_BASIS if effort == "low" else QWEN_TEMPLATE_BASIS
        return TokenEstimate(tokens, native_basis, QWEN_ASSET_SHA256, None)
    strict = basis != "auto"
    reason = _ordinary_request_shape(
        raw, historical_output_cap=basis in {TOKENIZER_BASIS_V1, TOKENIZER_BASIS_V2},
        allow_medium_effort=basis in {"auto", TOKENIZER_BASIS},
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
