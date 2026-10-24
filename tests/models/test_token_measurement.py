"""Provider admission demand never substitutes for reported token usage."""

import pytest

from ora.models.token_measurement import measure_tokens

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
    assert result["usage_minus_local_estimate"] is None
    assert "synthetic-private" not in str(result)
    assert "message" not in result


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
