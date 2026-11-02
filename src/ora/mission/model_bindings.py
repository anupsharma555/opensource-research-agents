"""Resolve each peer's saved model choice without changing it during a run."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ora.credentials import validate_selection
from ora.live.provider import URL
from ora.models.profiles import GPT_OSS_MODEL, QWEN_MODEL, SUPPORTED_MODELS
from ora.state import StateError

QWEN_DEFAULT_COMPLETION_TOKENS = 512
_BINDING_FIELDS = {"model", "reasoningEffort", "maxCompletionTokens"}


def _checked_choice(value: object) -> dict:
    if type(value) is not dict or set(value) != _BINDING_FIELDS:
        raise StateError("Saved agent model binding has an invalid shape")
    model = value["model"]
    effort = value["reasoningEffort"]
    cap = value["maxCompletionTokens"]
    if (type(model) is not str or model not in SUPPORTED_MODELS
            or type(effort) is not str
            or effort not in ({"low", "medium", "high"}
                              if model == QWEN_MODEL else {"medium", "high"})
            or type(cap) is not int or not 1 <= cap <= (
                16384 if model == QWEN_MODEL else 65536
            )):
        raise StateError("Saved agent model binding is unsupported")
    return value


def make_agent_model_bindings(
    aliases: Iterable[str], requested: Mapping[str, dict] | None, *,
    default_model: str = GPT_OSS_MODEL, default_effort: str = "medium",
    default_completion_tokens: int = 8192,
) -> dict[str, dict]:
    """Pin a full alias catalog, including roles that may be invited later."""
    names = tuple(aliases)
    if (not names or len(set(names)) != len(names)
            or any(type(name) is not str or not name for name in names)
            or type(requested) not in {dict, type(None)}
            or requested is not None and set(requested) - set(names)):
        raise ValueError("Agent model choices need known distinct aliases")
    choices = requested or {}
    bindings = {}
    for alias in names:
        selected = choices[alias] if alias in choices else {}
        if type(selected) is not dict or set(selected) - _BINDING_FIELDS:
            raise ValueError("Agent model choice has an invalid shape")
        model = selected.get("model", default_model)
        effort = (selected["reasoningEffort"]
                  if selected.get("reasoningEffort") is not None else
                  "medium" if model == QWEN_MODEL else default_effort)
        cap = (selected["maxCompletionTokens"]
               if selected.get("maxCompletionTokens") is not None else
               QWEN_DEFAULT_COMPLETION_TOKENS if model == QWEN_MODEL
               else default_completion_tokens)
        choice = {"model": model, "reasoningEffort": effort,
                  "maxCompletionTokens": cap}
        try:
            _checked_choice(choice)
        except StateError as exc:
            raise ValueError("Agent model choice is unsupported") from exc
        bindings[alias] = choice
    return bindings


def resolve_agent_model(
    run_body: dict, alias: str, *, prior_configuration: dict | None = None,
) -> dict:
    """Return the selected peer's provider inputs from immutable run state.

    Version 1 uses the full saved alias catalog. Older runs retain their own
    saved run-level selection, falling back to their prior configuration.
    """
    if type(run_body) is not dict or type(alias) is not str or not alias:
        raise StateError("Agent model selection needs a saved run and alias")
    profile = run_body.get("profile")
    if type(profile) is not dict:
        raise StateError("Saved run model profile is unavailable")
    version = run_body.get("agent_model_binding_version")
    if version is None:
        if "agentModelBindings" in profile:
            raise StateError("Unversioned agent model bindings cannot be used")
        previous = prior_configuration["body"] if prior_configuration else {}
        provider = profile.get("selectedProvider") or previous.get("provider", "groq")
        endpoint = profile.get("selectedEndpoint") or previous.get("endpoint", URL)
        model = profile.get("selectedModel") or previous.get("model_id", GPT_OSS_MODEL)
        settings = profile.get("modelSettings") or previous.get("settings") or {
            "max_completion_tokens": 8192, "stream": False,
            "unknown_source_policy": "reject",
        }
        reasoning = profile.get("reasoningSettings") or previous.get("reasoning_settings") or {
            "reasoning_effort": "medium", "include_reasoning": True,
        }
    elif version == 1:
        bindings = profile.get("agentModelBindings")
        if (type(bindings) is not dict or not bindings or alias not in bindings
                or any(type(name) is not str or not name for name in bindings)):
            raise StateError("Saved agent model catalog is incomplete")
        for choice in bindings.values():
            _checked_choice(choice)
        choice = bindings[alias]
        provider = profile.get("selectedProvider")
        endpoint = profile.get("selectedEndpoint")
        model = choice["model"]
        base_settings = profile.get("modelSettings")
        if type(base_settings) is not dict:
            raise StateError("Saved model settings are unavailable")
        settings = {**base_settings, "max_completion_tokens": choice["maxCompletionTokens"]}
        reasoning = {"reasoning_effort": choice["reasoningEffort"]}
        if model == QWEN_MODEL:
            reasoning["reasoning_format"] = "parsed"
        else:
            reasoning["include_reasoning"] = True
    else:
        raise StateError("Saved agent model binding version is unsupported")
    if (provider != "groq" or endpoint != URL or model not in SUPPORTED_MODELS
            or type(settings) is not dict or type(reasoning) is not dict):
        raise StateError("Saved selected model is unsupported")
    credential_profile = settings.get("credential_profile", "primary")
    quota_domain = settings.get("quota_domain", "groq-selected")
    validate_selection(credential_profile, quota_domain)
    return {"provider": provider, "endpoint": endpoint, "model_id": model,
            "settings": settings, "reasoning_settings": reasoning}
