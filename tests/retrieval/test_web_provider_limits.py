"""Both Tavily pacing windows coexist with existing shared provider limits."""

import pytest

from ora.coordination.capacity import CapacityPolicy, QuotaLimit
from ora.retrieval.web_config import provider_policy


def test_tavily_has_shared_second_and_minute_limits_without_raising_exa():
    base = CapacityPolicy(
        {"model": 2, "retrieval": 6, "compute": 2},
        {
            "web:exa": QuotaLimit(1, requests=3, concurrency=2),
            "web:tavily": QuotaLimit(1, requests=2, concurrency=2),
            "web:tavily:minute": QuotaLimit(60, requests=40),
        },
    )
    policy = provider_policy(base, {"OSRA_TAVILY_REQUESTS_PER_MINUTE": "80"})
    assert policy.quotas["web:tavily"].requests == 2
    assert policy.quotas["web:tavily:minute"].requests == 40
    assert policy.quotas["web:exa"].requests == 3
    assert policy.permits["retrieval"] == 6


def test_tavily_minute_default_and_explicit_lower_limit():
    base = CapacityPolicy({"model": 2, "retrieval": 6, "compute": 2})
    assert provider_policy(base, {}).quotas["web:tavily:minute"].requests == 100
    assert (
        provider_policy(base, {"OSRA_TAVILY_REQUESTS_PER_MINUTE": "25"})
        .quotas["web:tavily:minute"]
        .requests
        == 25
    )
    assert (
        provider_policy(base, {"OSRA_TAVILY_WINDOW_SECONDS": "120"})
        .quotas["web:tavily:minute"]
        .window_seconds
        == 120
    )
    for invalid in ("0", "101"):
        with pytest.raises(ValueError):
            provider_policy(base, {"OSRA_TAVILY_REQUESTS_PER_MINUTE": invalid})
    for invalid in ("59", "3601"):
        with pytest.raises(ValueError):
            provider_policy(base, {"OSRA_TAVILY_WINDOW_SECONDS": invalid})
