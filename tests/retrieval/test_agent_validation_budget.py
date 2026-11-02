"""Offline admission and replay tests for the natural-run HTTP ledger."""

import io
import json
import socket
import urllib.error
import urllib.request

import pytest

from ora.live.provider import MODEL, URL
from ora.mission.figure_reading import VISION_MODEL
from ora.models.token_estimate import (
    QWEN_SURROGATE_BASIS,
    QWEN_TEMPLATE_BASIS,
    QWEN_TEMPLATE_LOW_BASIS,
    estimate_request_tokens,
)
from ora.retrieval.agent_validation_budget import AgentBudget, BudgetStop


class Response(io.BytesIO):
    status = 200
    code = 200
    headers = {}
    url = URL

    def geturl(self):
        return self.url

    def info(self):
        return self.headers


@pytest.mark.parametrize("allow_qwen_text", [False, True])
def test_vision_requires_explicit_allowance_and_shares_model_attempt_cap(
    tmp_path, allow_qwen_text,
):
    packet = {"model": VISION_MODEL, "stream": False, "reasoning_effort": "none",
              "max_completion_tokens": 1024, "messages": [{"role": "user", "content": [
                  {"type": "text", "text": "Read the synthetic axis label"},
                  {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 200_000}},
              ]}]}
    vision = urllib.request.Request(URL, data=json.dumps(packet).encode(), method="POST")
    sent = []

    def opener(_self, _url, **_kwargs):
        sent.append(1)
        return response(total=2400)

    with AgentBudget(tmp_path / "disabled.json") as budget:
        with pytest.raises(BudgetStop, match="model_request_mismatch"):
            budget.open(opener, object(), vision)
    assert not sent
    with AgentBudget(tmp_path / "enabled.json", allow_vision=True,
                     allow_qwen_text=allow_qwen_text,
                     model_limit=1, token_limit=5000) as budget:
        budget.open(opener, object(), vision)
        entry = budget.data["model_attempts"][0]
        assert entry["model"] == VISION_MODEL and entry["model_role"] == "vision"
        assert 2048 < entry["estimated_input_tokens"] < 2400
        assert entry["actual_tokens"] == 2400
        with pytest.raises(BudgetStop, match="model_attempt_cap"):
            budget.open(opener, object(), request())
    assert len(sent) == 1
    assert "A" * 100 not in (tmp_path / "enabled.json").read_text()


def qwen_text_request(tmp_path, *, reasoning_effort="medium"):
    from ora.models.groq import CallConfiguration, GroqSelectedAdapter

    config = CallConfiguration(
        "synthetic-qwen", "a" * 64, "Use supplied evidence.",
        model_id=VISION_MODEL, reasoning_effort=reasoning_effort, include_reasoning=None,
        reasoning_format="parsed", max_completion_tokens=100,
    )
    prepared = GroqSelectedAdapter(tmp_path).prepare(
        config, call_id="qwen-call", attempt_id="qwen-attempt",
        task_category="fixture", phase="collect",
        messages=[{"role": "user", "content": "Read the synthetic fixture"}],
        tools=({"type": "function", "function": {
            "name": "read_fixture", "parameters": {"type": "object", "properties": {},
                                                   "additionalProperties": False},
        }},),
    )
    raw = json.dumps(prepared.body, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode()
    return urllib.request.Request(URL, data=raw, method="POST"), raw


@pytest.mark.parametrize("policy", ["unknown", None, []])
def test_qwen_estimate_policy_must_be_known_text(tmp_path, policy):
    with pytest.raises(ValueError, match="Qwen estimate policy"):
        AgentBudget(tmp_path / "invalid-policy.json", qwen_estimate_policy=policy)


def test_qwen_text_requires_opt_in_and_reserves_native_input(tmp_path):
    request, raw = qwen_text_request(tmp_path)
    estimate = estimate_request_tokens(raw)
    old = estimate_request_tokens(raw, basis=QWEN_SURROGATE_BASIS)
    assert estimate.basis == QWEN_TEMPLATE_BASIS
    assert estimate.tokens < old.tokens
    sent = []

    def opener(_self, value, **_kwargs):
        sent.append(json.loads(value.data))
        return Response(json.dumps({"model": VISION_MODEL, "usage": {"total_tokens": 20}}).encode())

    with AgentBudget(tmp_path / "disabled.json") as budget:
        with pytest.raises(BudgetStop, match="model_request_mismatch"):
            budget.open(opener, object(), request)
    assert sent == []

    path = tmp_path / "enabled.json"
    with AgentBudget(path, allow_qwen_text=True,
                     qwen_estimate_policy="native_when_supported", model_limit=1,
                     token_limit=estimate.tokens + 100, retrieval_limit=0) as budget:
        budget.open(opener, object(), request)
        entry = budget.data["model_attempts"][0]
        assert entry["model"] == VISION_MODEL and entry["model_role"] == "reasoning"
        assert entry["estimated_input_tokens"] == estimate.tokens
        assert entry["token_estimate_basis"] == QWEN_TEMPLATE_BASIS
        assert entry["planned_tokens"] == estimate.tokens + 100
        assert entry["actual_tokens"] == 20
        assert budget.data["planned_tokens"] == 20
        assert budget.data["limits"]["allow_qwen_text"] is True
        assert budget.data["limits"]["qwen_estimate_policy"] == "native_when_supported"
        assert "allow_vision" not in budget.data["limits"]
    assert sent[0]["reasoning_format"] == "parsed"
    assert sent[0]["reasoning_effort"] == "medium"
    assert sent[0]["tools"][0]["type"] == "function"
    with pytest.raises(BudgetStop, match="budget_identity_mismatch"):
        with AgentBudget(path, model_limit=1, token_limit=estimate.tokens + 100,
                         retrieval_limit=0, allow_qwen_text=True):
            pytest.fail("reopened native Qwen ledger under legacy estimate policy")


def test_qwen_low_native_budget_uses_distinct_pinned_basis(tmp_path):
    request, raw = qwen_text_request(tmp_path, reasoning_effort="low")
    estimate = estimate_request_tokens(raw)
    assert estimate.basis == QWEN_TEMPLATE_LOW_BASIS
    sent = []

    def opener(_self, value, **_kwargs):
        sent.append(json.loads(value.data))
        return Response(json.dumps({"model": VISION_MODEL, "usage": {
            "total_tokens": 20,
        }}).encode())

    with AgentBudget(
        tmp_path / "low-native.json", allow_qwen_text=True,
        qwen_estimate_policy="native_when_supported", model_limit=1,
        token_limit=estimate.tokens + 100, retrieval_limit=0,
    ) as budget:
        budget.open(opener, object(), request)
        entry = budget.data["model_attempts"][0]
        assert entry["estimated_input_tokens"] == estimate.tokens
        assert entry["token_estimate_basis"] == QWEN_TEMPLATE_LOW_BASIS
        assert entry["planned_tokens"] == estimate.tokens + 100
        assert entry["actual_tokens"] == 20
    assert sent[0]["reasoning_effort"] == "low"
    assert sent[0]["reasoning_format"] == "parsed"


@pytest.mark.parametrize(("change", "reason"), [
    (lambda packet: packet.update(reasoning_effort="high"), "model_request_mismatch"),
    (lambda packet: packet.update(reasoning_format="raw"), "model_request_mismatch"),
    (lambda packet: packet.update(include_reasoning=True), "model_request_mismatch"),
    (lambda packet: packet.update(extra_setting=True), "model_request_invalid"),
    (lambda packet: packet["messages"].append({"role": "tool", "content": "orphan",
                                               "tool_call_id": "none"}), "model_request_invalid"),
    (lambda packet: packet.update(tools=[{"type": "browser_search"}]), "model_request_mismatch"),
    (lambda packet: packet["messages"][1].update(content=[{
        "type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="},
    }]), "model_request_invalid"),
])
def test_qwen_text_rejects_unsupported_or_image_request_before_transport(
    tmp_path, change, reason,
):
    request, raw = qwen_text_request(tmp_path)
    packet = json.loads(raw)
    change(packet)
    request = urllib.request.Request(URL, data=json.dumps(
        packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode(), method="POST")
    sent = []

    def opener(_self, _value, **_kwargs):
        sent.append(1)
        return response()

    with AgentBudget(tmp_path / "invalid.json", allow_qwen_text=True,
                     qwen_estimate_policy="native_when_supported") as budget:
        with pytest.raises(BudgetStop, match=reason):
            budget.open(opener, object(), request)
        assert budget.data["model_attempts"] == []
    assert sent == []


def test_qwen_text_429_keeps_unknown_reservation_and_blocks_next_dispatch(tmp_path):
    request, raw = qwen_text_request(tmp_path)
    reservation = estimate_request_tokens(raw, basis=QWEN_SURROGATE_BASIS).tokens + 100
    sent = []

    def limited(_self, _value, **_kwargs):
        sent.append(1)
        raise urllib.error.HTTPError(URL, 429, "Too Many Requests", {}, io.BytesIO(b"{}"))

    with AgentBudget(tmp_path / "qwen-429.json", allow_qwen_text=True,
                     model_limit=None, token_limit=reservation, retrieval_limit=0) as budget:
        with pytest.raises(urllib.error.HTTPError):
            budget.open(limited, object(), request)
        entry = budget.data["model_attempts"][0]
        assert entry["state"] == "rate_limited"
        assert entry["usage_status"] == "unreported_reservation_retained"
        assert "actual_tokens" not in entry
        assert budget.data["planned_tokens"] == reservation
        with pytest.raises(BudgetStop, match="token_cap"):
            budget.open(limited, object(), request)
    assert sent == [1]


def test_historical_qwen_surrogate_reservation_is_not_repriced_on_reopen(tmp_path):
    model_request, raw = qwen_text_request(tmp_path)
    historical = estimate_request_tokens(raw, basis=QWEN_SURROGATE_BASIS)
    old_charge = historical.tokens + 100
    path = tmp_path / "historical-qwen.json"
    options = {"allow_qwen_text": True, "model_limit": None,
               "token_limit": old_charge * 2, "retrieval_limit": 0}
    with AgentBudget(path, **options):
        pass
    saved = json.loads(path.read_text())
    old_entry = {"number": 1, "state": "rate_limited", "status": 429,
                 "estimated_input_tokens": historical.tokens,
                 "token_estimate_basis": QWEN_SURROGATE_BASIS,
                 "output_reservation": 100, "planned_tokens": old_charge,
                 "usage_status": "unreported_reservation_retained"}
    saved["model_attempts"] = [old_entry]
    saved["planned_tokens"] = old_charge
    saved["peak_planned_tokens"] = old_charge
    path.write_text(json.dumps(saved))
    sent = []

    def opener(_self, _request, **_kwargs):
        sent.append(1)
        return Response(json.dumps({"model": VISION_MODEL,
                                    "usage": {"total_tokens": 20}}).encode())

    with AgentBudget(path, **options) as budget:
        assert budget.data["model_attempts"][0] == old_entry
        budget.open(opener, object(), model_request)
        assert budget.data["model_attempts"][0] == old_entry
        assert budget.data["model_attempts"][1]["token_estimate_basis"] == QWEN_SURROGATE_BASIS
        assert budget.data["model_attempts"][1]["estimated_input_tokens"] == historical.tokens
    assert sent == [1]
    with pytest.raises(BudgetStop, match="budget_identity_mismatch"):
        with AgentBudget(path, **{**options,
                                 "qwen_estimate_policy": "native_when_supported"}):
            pytest.fail("legacy ledger changed estimate policy on resume")


def test_qwen_nonfinite_tool_history_is_denied_before_http(tmp_path):
    _request, raw = qwen_text_request(tmp_path)
    packet = json.loads(raw)
    packet["messages"].extend([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "synthetic-tool", "type": "function",
            "function": {"name": "read_fixture", "arguments": '{"value":[1e999]}'},
        }]},
        {"role": "tool", "name": "read_fixture", "tool_call_id": "synthetic-tool",
         "content": "synthetic result"},
        {"role": "user", "content": "Continue from the result."},
    ])
    invalid = urllib.request.Request(URL, data=json.dumps(
        packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode(), method="POST")
    sent = []
    with AgentBudget(tmp_path / "nonfinite.json", allow_qwen_text=True,
                     qwen_estimate_policy="native_when_supported") as budget:
        with pytest.raises(BudgetStop, match="model_request_invalid"):
            budget.open(lambda *_a, **_kw: sent.append(1), object(), invalid)
        assert budget.data["model_attempts"] == []
    assert sent == []


def request(*, native=False, cap=100, secret="private prompt"):
    body = {"model": MODEL, "messages": [{"role": "user", "content": secret}],
            "max_completion_tokens": cap, "reasoning_effort": "low" if native else "high",
            "include_reasoning": True, "stream": False,
            "tools": [{"type": "browser_search"}] if native else []}
    return urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                  headers={"Authorization": "Bearer private-key"})


def response(total=20):
    return Response(json.dumps({"model": MODEL, "usage": {"total_tokens": total},
                                "choices": [{"message": {"content": "private answer"}}]}).encode())


@pytest.mark.parametrize("effort", ["medium", "high"])
def test_selected_effort_is_tokenized_reserved_and_capped_across_restart(tmp_path, effort):
    from ora.models.groq import CallConfiguration, GroqSelectedAdapter
    from ora.models.token_estimate import TOKENIZER_BASIS, estimate_request_tokens

    config = CallConfiguration("synthetic-config", "a" * 64, "Use supplied evidence.",
                               reasoning_effort=effort, max_completion_tokens=100)
    prepared = GroqSelectedAdapter(tmp_path).prepare(
        config, call_id="call", attempt_id="attempt", task_category="fixture",
        phase="collect", messages=[{"role": "user", "content": "Read the fixture"}],
        tools=({"type": "function", "function": {
            "name": "read_fixture", "parameters": {"type": "object", "properties": {},
                                                     "additionalProperties": False},
        }},),
    )
    raw = json.dumps(prepared.body, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode()
    estimate = estimate_request_tokens(raw)
    assert estimate.basis == TOKENIZER_BASIS and estimate.fallback_reason is None
    model_request = urllib.request.Request(URL, data=raw, method="POST")
    path = tmp_path / "ledger.json"
    sent = []

    def opener(_self, value, **_kwargs):
        pending = json.loads(path.read_text())
        assert len(pending["model_attempts"]) == 1
        entry = pending["model_attempts"][0]
        assert entry["state"] == "attempted"
        assert entry["estimated_input_tokens"] == estimate.tokens
        assert entry["planned_tokens"] == estimate.tokens + 100
        sent.append(json.loads(value.data)["reasoning_effort"])
        return response()

    with AgentBudget(path, model_limit=1, token_limit=20_000, retrieval_limit=0) as budget:
        budget.open(opener, object(), model_request)
        assert budget.data["planned_tokens"] == 20
    with AgentBudget(path, model_limit=1, token_limit=20_000, retrieval_limit=0) as reopened:
        with pytest.raises(BudgetStop, match="model_attempt_cap"):
            reopened.open(opener, object(), model_request)
    assert sent == [effort]


def test_actual_usage_releases_unused_reservation_and_preserves_secrets(tmp_path):
    path = tmp_path / "ledger.json"
    calls = []

    def opener(_self, _url, **_kwargs):
        calls.append(1)
        return response()

    with AgentBudget(path, token_limit=650, model_limit=3) as budget:
        for _ in range(3):
            with budget.open(opener, object(), request(cap=200)) as result:
                assert b"private answer" in result.read()
        assert budget.data["planned_tokens"] == 60
        assert budget.data["peak_planned_tokens"] > budget.data["planned_tokens"]
    assert len(calls) == 3
    saved = path.read_text()
    assert "private prompt" not in saved
    assert "private answer" not in saved
    assert "private-key" not in saved


def test_token_and_call_caps_block_before_dispatch(tmp_path):
    sent = []

    def opener(_self, _url, **_kwargs):
        sent.append(1)
        return response()

    path = tmp_path / "tokens.json"
    with AgentBudget(path, token_limit=10) as budget:
        with pytest.raises(BudgetStop, match="token_cap"):
            budget.open(opener, object(), request(cap=100))
    assert not sent

    path = tmp_path / "calls.json"
    with AgentBudget(path, model_limit=1) as budget:
        budget.open(opener, object(), request())
        with pytest.raises(BudgetStop, match="model_attempt_cap"):
            budget.open(opener, object(), request())
    assert len(sent) == 1


def test_token_only_budget_counts_past_eight_and_keeps_ceiling_after_restart(tmp_path):
    from ora.models.token_estimate import estimate_request_tokens

    path = tmp_path / "token-only.json"
    packet = request(cap=100)
    reservation = estimate_request_tokens(packet.data).tokens + 100
    options = dict(model_limit=None, token_limit=reservation + 180, retrieval_limit=0)
    sent = []

    def opener(_self, _url, **_kwargs):
        saved = json.loads(path.read_text())
        assert saved["model_attempts"][-1]["state"] == "attempted"
        assert saved["planned_tokens"] <= options["token_limit"]
        sent.append(1)
        return response(total=20)

    with AgentBudget(path, **options) as budget:
        for _ in range(9):
            budget.open(opener, object(), packet)
        assert budget.data["planned_tokens"] == 180
        assert budget.data["limits"]["model"] is None
    with AgentBudget(path, **options) as budget:
        # Equal-to-budget admission is valid; the following call would reserve
        # more than the remaining token allowance and must never reach HTTP.
        budget.open(opener, object(), packet)
        with pytest.raises(BudgetStop, match="token_cap"):
            budget.open(opener, object(), packet)
    assert len(sent) == 10
    with pytest.raises(BudgetStop, match="token_cap"):
        with AgentBudget(path, **options):
            pytest.fail("reopened an exhausted token allowance")


def test_token_only_budget_still_denies_retrieval_and_stops_unknown_usage(tmp_path):
    options = dict(model_limit=None, token_limit=1000, retrieval_limit=0)
    sent = []

    def opener(_self, _url, **_kwargs):
        sent.append(1)
        return Response(b'{"choices": []}')

    with AgentBudget(tmp_path / "retrieval.json", **options) as budget:
        with pytest.raises(BudgetStop, match="retrieval_attempt_cap"):
            budget.open(opener, object(), "https://example.org/source")
    assert not sent
    with AgentBudget(tmp_path / "unknown.json", **options) as budget:
        budget.open(opener, object(), request())
        with pytest.raises(BudgetStop, match="unknown_model_usage"):
            budget.open(opener, object(), request())
    assert len(sent) == 1


def test_default_eight_model_and_four_retrieval_attempts(tmp_path):
    sent = []

    def opener(_self, value, **_kwargs):
        sent.append(value)
        return response() if isinstance(value, urllib.request.Request) else Response(b"source")

    with AgentBudget(tmp_path / "ledger.json") as budget:
        for _ in range(8):
            budget.open(opener, object(), request())
        for _ in range(4):
            budget.open(opener, object(), "https://example.org/source")
        with pytest.raises(BudgetStop, match="model_attempt_cap"):
            budget.open(opener, object(), request())
    assert len(sent) == 12


def test_native_browser_request_keeps_full_tools_and_marks_internal_count_unknown(tmp_path):
    observed = []

    def opener(_self, value, **_kwargs):
        observed.append(json.loads(value.data))
        return response()

    with AgentBudget(tmp_path / "ledger.json") as budget:
        budget.open(opener, object(), request(native=True))
        entry = budget.data["model_attempts"][0]
        assert entry["native_browser_search"] is True
        assert entry["provider_internal_search_attempts"] == "unknown"
    assert observed[0]["tools"] == [{"type": "browser_search"}]
    assert observed[0]["reasoning_effort"] == "low"


def test_retrieval_redirect_recursion_counts_each_open(tmp_path):
    path = tmp_path / "ledger.json"
    seen = []
    with AgentBudget(path, retrieval_limit=2) as budget:
        def opener(_self, url, **_kwargs):
            seen.append(url)
            if len(seen) == 1:
                return budget.open(opener, object(), "https://example.org/redirect")
            return Response(b"source")

        assert budget.open(opener, object(), "https://example.org/start").read() == b"source"
        with pytest.raises(BudgetStop, match="retrieval_attempt_cap"):
            budget.open(opener, object(), "https://example.org/third")
    assert len(seen) == 2


def test_zero_retrieval_allowance_rejects_before_http(tmp_path):
    called = []

    def opener(*_args, **_kwargs):
        called.append(True)
        return Response(b"unexpected")

    with AgentBudget(tmp_path / "ledger.json", retrieval_limit=0,
                     model_limit=1, token_limit=1000) as budget:
        with pytest.raises(BudgetStop, match="retrieval_attempt_cap"):
            budget.open(opener, object(), "https://example.org/source")
        assert budget.data["retrieval_attempts"] == []
    assert called == []

    with AgentBudget(tmp_path / "native-ledger.json", retrieval_limit=0,
                     model_limit=1, token_limit=10000) as budget:
        with pytest.raises(BudgetStop, match="retrieval_attempt_cap"):
            budget.open(opener, object(), request(native=True))
        assert budget.data["model_attempts"] == []
        assert budget.data["retrieval_attempts"] == []
    assert called == []


def test_uncertain_transport_is_durable_and_cannot_restart(tmp_path):
    path = tmp_path / "ledger.json"

    def failed(_self, _url, **_kwargs):
        raise TimeoutError()

    with AgentBudget(path) as budget:
        with pytest.raises(BudgetStop, match="uncertain_transport"):
            budget.open(failed, object(), "https://example.org/source")
    with pytest.raises(BudgetStop, match="uncertain_transport"):
        with AgentBudget(path):
            pytest.fail("replayed uncertain request")


@pytest.mark.parametrize(("failure", "category"), [
    (urllib.error.URLError(socket.gaierror(8, "private DNS hostname")), "dns"),
    (urllib.error.URLError(TimeoutError("private timeout target")), "timeout"),
    (urllib.error.URLError(ConnectionRefusedError("private endpoint")), "connection"),
    (urllib.error.URLError("private opaque failure"), "other"),
])
def test_uncertain_opening_records_only_sanitized_failure_category(
    tmp_path, failure, category,
):
    path = tmp_path / "ledger.json"
    calls = []

    def failed(_self, _url, **_kwargs):
        calls.append(1)
        raise failure

    with AgentBudget(path) as budget:
        with pytest.raises(BudgetStop, match="uncertain_transport"):
            budget.open(failed, object(), request())
        entry = budget.data["model_attempts"][0]
        assert entry["state"] == "uncertain"
        assert entry["failure_stage"] == "opening"
        assert entry["failure_category"] == category
        assert budget.data["planned_tokens"] == entry["planned_tokens"]
        assert "actual_tokens" not in entry
    with pytest.raises(BudgetStop, match="uncertain_transport"):
        with AgentBudget(path):
            pytest.fail("replayed uncertain request")
    assert calls == [1]
    saved = path.read_text()
    assert "private" not in saved
    assert "Bearer" not in saved


@pytest.mark.parametrize(("http_error", "category"), [
    (False, "timeout"), (True, "connection"),
])
def test_response_read_failure_is_uncertain_capture_without_body_or_replay(
    tmp_path, http_error, category,
):
    path = tmp_path / "ledger.json"
    calls = []

    class BrokenRead(Response):
        def read(self, *_args, **_kwargs):
            failure = (ConnectionResetError("private response and URL") if http_error
                       else TimeoutError("private response and URL"))
            raise failure

    def failed(_self, _url, **_kwargs):
        calls.append(1)
        if http_error:
            raise urllib.error.HTTPError(
                URL, 503, "private HTTP reason", {}, BrokenRead(b"private response"),
            )
        return BrokenRead(b"private response")

    with AgentBudget(path) as budget:
        with pytest.raises(BudgetStop, match="uncertain_transport"):
            budget.open(failed, object(), request())
        entry = budget.data["model_attempts"][0]
        assert entry["state"] == "uncertain"
        assert entry["failure_stage"] == "capture"
        assert entry["failure_category"] == category
        assert budget.data["stop_reason"] == "uncertain_transport"
        assert budget.data["planned_tokens"] == entry["planned_tokens"]
        assert "response_sha256" not in entry
        assert "actual_tokens" not in entry
    with pytest.raises(BudgetStop, match="uncertain_transport"):
        with AgentBudget(path):
            pytest.fail("replayed uncertain response")
    assert calls == [1]
    assert "private" not in path.read_text()


def test_missing_model_usage_latches_before_next_request(tmp_path):
    path = tmp_path / "ledger.json"
    sent = []

    def no_usage(_self, _url, **_kwargs):
        sent.append(1)
        return Response(b'{"model":"openai/gpt-oss-120b","choices":[]}')

    with AgentBudget(path) as budget:
        budget.open(no_usage, object(), request())
        with pytest.raises(BudgetStop, match="unknown_model_usage"):
            budget.open(no_usage, object(), request())
    assert len(sent) == 1
    with pytest.raises(BudgetStop, match="unknown_model_usage"):
        with AgentBudget(path):
            pytest.fail("replayed unknown usage")


def test_429_preserves_error_body_and_unknown_usage_reservation(tmp_path):
    raw = b'{"error":"private rate limit"}'

    def limited(_self, _url, **_kwargs):
        raise urllib.error.HTTPError(URL, 429, "Too Many Requests", {"Retry-After": "2"},
                                     io.BytesIO(raw))

    with AgentBudget(tmp_path / "ledger.json") as budget:
        with pytest.raises(urllib.error.HTTPError) as raised:
            budget.open(limited, object(), request())
        assert raised.value.read() == raw
        assert budget.data["model_attempts"][0]["state"] == "rate_limited"
        assert budget.data["planned_tokens"] > 0
        assert "actual_tokens" not in budget.data["model_attempts"][0]
        assert budget.data["stop_reason"] is None
    assert "private rate limit" not in (tmp_path / "ledger.json").read_text()
