"""Explicit Groq model capabilities used at request and replay boundaries."""

from __future__ import annotations

from ora.credentials import validate_selection
from ora.live.provider import MODEL as GPT_OSS_MODEL

QWEN_MODEL = "qwen/qwen3.8-27b"
SUPPORTED_MODELS = frozenset({GPT_OSS_MODEL, QWEN_MODEL})
QWEN_REASONING_EFFORTS = frozenset({"none", "default", "low", "medium", "high"})


def model_supports_native_browser(model_id: object) -> bool:
    """Groq's native browser_search descriptor is available only on GPT-OSS."""
    return model_id == GPT_OSS_MODEL


def model_quota_scope(quota_domain: str, model_id: str, *, legacy: bool = False) -> str:
    """Name one organization's model allowance without using a credential label.

    The caller alone decides whether an older saved run retains its account-only
    scope. This helper validates the label's shape, not its organization mapping.
    """
    validate_selection("primary", quota_domain)
    if not isinstance(model_id, str) or model_id not in SUPPORTED_MODELS:
        raise ValueError("Unsupported Groq model for quota scope")
    if type(legacy) is not bool:
        raise ValueError("Legacy quota scope selection must be explicit boolean")
    return quota_domain if legacy else f"model:{quota_domain}:{model_id}"


def supported_reasoning(model_id: str, reasoning: dict) -> bool:
    """Accept only an explicit, provider-supported setting set for each model."""
    if type(reasoning) is not dict:
        return False
    if model_id == GPT_OSS_MODEL:
        return (set(reasoning) == {"reasoning_effort", "include_reasoning"}
                and reasoning.get("reasoning_effort") in {"medium", "high"}
                and reasoning.get("include_reasoning") is True)
    if model_id == QWEN_MODEL:
        return (set(reasoning) == {"reasoning_effort", "reasoning_format"}
                and reasoning.get("reasoning_effort") in QWEN_REASONING_EFFORTS
                and reasoning.get("reasoning_format") == "parsed")
    return False
