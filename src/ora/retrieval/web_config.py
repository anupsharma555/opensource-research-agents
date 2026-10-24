"""Read OSRA-owned retrieval configuration without loading unrelated integrations.

Provider credentials stay in memory. Merely reading settings never enables a
mission or contacts a provider; the mission host still owns live authorization.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from ora.coordination.capacity import CapacityPolicy, QuotaLimit


def configuration(root: Path) -> dict[str, str]:
    names = {
        "EXA_API_KEY",
        "TAVILY_API_KEY",
        "BROWSERLESS_API_KEY",
        "NCBI_API_KEY",
        "OSRA_SEARXNG_BASE_URL",
        "OSRA_BROWSERLESS_BASE_URL",
        "OSRA_EXA_CONCURRENCY",
        "OSRA_EXA_REQUESTS_PER_SECOND",
        "OSRA_RETRIEVAL_SLOTS",
        "OSRA_TAVILY_REQUESTS_PER_MINUTE",
        "OSRA_TAVILY_WINDOW_SECONDS",
        "OSRA_NCBI_REQUESTS_PER_SECOND",
    }
    values = {}
    path = root / ".env"
    if path.is_file():
        for line in path.read_text().splitlines():
            key, separator, value = line.partition("=")
            key = key.strip().removeprefix("export ")
            if separator and key in names:
                values[key] = value.strip().strip("\"'")
    values.update({key: os.environ[key].strip() for key in names if key in os.environ})
    return values


def ceiling(values: dict, name: str, maximum: int) -> int:
    value = int(values.get(name) or maximum)
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def tavily_window(values: dict) -> int:
    value = int(values.get("OSRA_TAVILY_WINDOW_SECONDS") or 60)
    if not 60 <= value <= 3600:
        raise ValueError("OSRA_TAVILY_WINDOW_SECONDS must be between 60 and 3600")
    return value


def provider_policy(base: CapacityPolicy, values: dict) -> CapacityPolicy:
    quotas = dict(base.quotas)
    # Existing lower operator limits win. Scopes are shared across missions and
    # credentials in this OSRA database, conservatively covering one provider pool.
    defaults = {
        "web:exa": QuotaLimit(
            1,
            requests=ceiling(values, "OSRA_EXA_REQUESTS_PER_SECOND", 5),
            concurrency=ceiling(values, "OSRA_EXA_CONCURRENCY", 5),
        ),
        "web:tavily": QuotaLimit(1, requests=5, concurrency=5),
        "web:tavily:minute": QuotaLimit(
            tavily_window(values),
            requests=ceiling(values, "OSRA_TAVILY_REQUESTS_PER_MINUTE", 100),
        ),
        "web:searxng": QuotaLimit(1, requests=2, concurrency=2),
        "web:browserless": QuotaLimit(1, requests=1, concurrency=1),
    }
    for key, default in defaults.items():
        previous = quotas.get(key)
        quotas[key] = (
            default
            if previous is None
            else QuotaLimit(
                max(previous.window_seconds, default.window_seconds),
                requests=min(previous.requests, default.requests)
                if previous.requests is not None
                else default.requests,
                tokens=previous.tokens,
                basis=previous.basis,
                concurrency=min(previous.concurrency, default.concurrency)
                if previous.concurrency is not None
                else default.concurrency,
            )
        )
    permits = dict(base.permits)
    permits["retrieval"] = min(permits["retrieval"], ceiling(values, "OSRA_RETRIEVAL_SLOTS", 6))
    return CapacityPolicy(permits, quotas, base.review_reserve)


def searx_endpoint(values: dict) -> str:
    endpoint = values.get("OSRA_SEARXNG_BASE_URL", "").rstrip("/")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Configure a dedicated OSRA SearXNG base URL")
    # Known neighboring deployments must never be used accidentally.
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.port in {8080, 18080}:
        raise ValueError("Use a dedicated OSRA SearXNG instance, not a neighboring project port")
    return endpoint
