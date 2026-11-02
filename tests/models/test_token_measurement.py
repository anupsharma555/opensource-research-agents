"""Provider admission demand never substitutes for reported token usage."""

import pytest

from ora.models.token_measurement import (
    input_limit_observation,
    measure_tokens,
    read_token_measurement,
)

MODEL = "synthetic/model"


@pytest.mark.parametrize("status,prefix,used", [
    (413, "Request too large", ""),
    (429, "Rate limit reached", "Used 6000, "),
])
def test_rate_error_preserves_numeric_admission_separately_from_unknown_usage(status, prefix, used):
    error = {"code": "rate_limit_exceeded", "type": "tokens", "message": (
        f"{prefix} for model `{MODEL}` in organization `synthetic-private` "
        f"on tokens per minute (TPM): Limit 8000, {used}Requested 8200, try later."
    )}
    result = measure_tokens(estimate={"tokens": 7400, "basis": "synthetic"}, usage={},
                            http_status=status, provider_error=error, model=MODEL)
    assert result["provider_admission_tokens"] == 8200
    assert result["admission_minus_local_estimate"] == 800
    assert result["reported_input_tokens"] is None
    assert result["provider_admission_scope"] == "combined_tokens_per_minute"
    assert result["usage_minus_local_estimate"] is None
    assert "synthetic-private" not in str(result)
    assert "message" not in result


def test_itpm_refusal_preserves_typed_numeric_input_bound_without_account_text():
    error = {"code": "rate_limit_exceeded", "type": "tokens", "message": (
        f"Request too large for model `{MODEL}` in organization `synthetic-private` "
        "on input tokens per minute (ITPM): Limit 7,000, Requested 7,030, try later."
    )}
    observation = input_limit_observation(413, error, MODEL)
    assert observation == {"scope": "input_tokens_per_minute",
                           "limit_tokens": 7000, "used_tokens": None,
                           "requested_tokens": 7030}
    result = measure_tokens(
        estimate={"tokens": 6980, "basis": "synthetic"}, usage={},
        http_status=413, provider_error=error, model=MODEL,
    )
    assert result["provider_admission_scope"] == "input_tokens_per_minute"
    assert result["provider_admission_limit_tokens"] == 7000
    assert result["provider_admission_tokens"] == 7030
    assert result["admission_minus_local_estimate"] == 50
    assert result["reported_input_tokens"] is None
    assert read_token_measurement({"token_measurement": result})[
        "provider_admission_scope"] == "input_tokens_per_minute"
    assert "synthetic-private" not in str(result)


def test_itpm_parser_accepts_explicit_used_count_without_counting_it_as_usage():
    error = {"code": "rate_limit_exceeded", "type": "tokens", "message": (
        f"Request too large for model `{MODEL}` on input tokens per minute "
        "(ITPM): Limit 7,000, Used 100, Requested 7,030, retry later."
    )}
    observed = input_limit_observation(413, error, MODEL)
    assert observed == {"scope": "input_tokens_per_minute", "limit_tokens": 7000,
                        "used_tokens": 100, "requested_tokens": 7030}
    measured = measure_tokens(estimate={"tokens": 6980}, usage={}, http_status=413,
                              provider_error=error, model=MODEL)
    assert measured["reported_input_tokens"] is None
    assert measured["provider_admission_tokens"] == 7030


@pytest.mark.parametrize("change", [
    {"http_status": 429},
    {"model": "another/model"},
    {"message": "Request too large for model `synthetic/model` on tokens per minute (TPM): Limit 7000, Requested 7030."},
    {"message": "Request too large for model `synthetic/model` on output tokens per minute (OTPM): Limit 7000, Requested 7030."},
    {"message": "Request too large for model `synthetic/model` on input tokens per minute (ITPM): Limit 7000, Requested 6999."},
    {"message": "Request too large for model `synthetic/model` on input tokens per minute (ITPM): Limit 0, Requested 7030."},
    {"message": "Request too large for model `synthetic/model` on input tokens per minute (ITPM): Limit 7000, Requested 7030; on input tokens per minute (ITPM): Limit 7000, Requested 7030."},
])
def test_itpm_parser_denies_unmatched_or_ambiguous_limits(change):
    error = {"code": "rate_limit_exceeded", "type": "tokens", "message": (
        f"Request too large for model `{MODEL}` on input tokens per minute "
        "(ITPM): Limit 7000, Requested 7030."
    )}
    status = change.get("http_status", 413)
    model = change.get("model", MODEL)
    error.update({key: value for key, value in change.items()
                  if key not in {"http_status", "model"}})
    assert input_limit_observation(status, error, model) is None


def test_success_reports_usage_error_without_inventing_provider_admission():
    result = measure_tokens(estimate={"tokens": 6000, "basis": "synthetic"},
                            usage={"prompt_tokens": 5400}, http_status=200,
                            provider_error=None, model=MODEL)
    assert result["usage_minus_local_estimate"] == -600
    assert result["provider_admission_tokens"] is None
    assert result["provider_admission_status"] == "not_reported"


@pytest.mark.parametrize("change", [
    {"code": "unrelated"}, {"type": "requests"},
    {"message": "Request too large for model `different` on tokens per minute (TPM): Limit 8000, Requested 8200."},
    {"message": "Request too large for model `synthetic/model` on tokens per day (TPD): Limit 8000, Requested 8200."},
    {"message": "Request too large for model `synthetic/model` on tokens per minute (TPM): Limit 0, Requested 8200."},
])
def test_unmatched_errors_do_not_claim_admission_counts(change):
    error = {"code": "rate_limit_exceeded", "type": "tokens", "message": (
        "Request too large for model `synthetic/model` on tokens per minute (TPM): "
        "Limit 8000, Requested 8200."
    ), **change}
    result = measure_tokens(estimate=None, usage={}, http_status=413,
                            provider_error=error, model=MODEL)
    assert result["provider_admission_tokens"] is None


def test_failed_capture_links_measurements_to_exact_saved_request(tmp_path):
    import json
    from dataclasses import asdict, replace

    from ora.models.token_estimate import estimate_request_tokens
    from ora.state.store import canonical
    from tests.models.test_session import MODEL as selected_model
    from tests.models.test_session import response, select, setup

    store, _, bridge, call, _, source, captured = setup(tmp_path)
    messages = [{"role": "user", "content": "Inspect the synthetic source"}]
    preview = bridge.preview_request(call=call, phase="collect", messages=messages,
                                     sources=select(source))
    estimate = estimate_request_tokens(canonical(preview.body).encode())
    prepared = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                              messages=messages, sources=select(source),
                              request_token_estimate=asdict(estimate))
    outcome = bridge.adapter.decode(prepared.request, json.loads(response()))
    error = {"code": "rate_limit_exceeded", "type": "tokens", "message": (
        f"Request too large for model `{selected_model}` on tokens per minute (TPM): "
        "Limit 8000, Requested 8200."
    )}
    rejected = replace(outcome, status="failed", http_status=413, usage={}, content=None,
                       tool_calls=(), reasoning=None, reasoning_status="unknown",
                       error_kind="http_error", provider_error=error)
    receipt = store.get(**bridge.capture(prepared.intent_ref, rejected))["body"]
    measurement = receipt["token_measurement"]
    assert measurement["local_input_estimate"] == estimate.tokens
    assert measurement["provider_admission_tokens"] == 8200
    assert measurement["reported_input_tokens"] is None
    assert receipt["usage"] == {}
    assert receipt["request_sha256"] == preview.request_sha256
    assert captured == []
