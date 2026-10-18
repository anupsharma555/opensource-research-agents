"""Offline tests for the durable provider validation boundary."""

import json

import pytest

from ora.retrieval.providers import HTTPResult
from ora.retrieval.validation_transport import BatchTransport, ValidationStop

EXA = "https://api.exa.ai/search"
NCBI = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?api_key=private-key"


def test_cap_is_durable_across_restart(tmp_path):
    path = tmp_path / "budget.json"
    calls = []

    def opener(*args, **kwargs):
        calls.append(args)
        return HTTPResult(200, b"{}", {})

    with BatchTransport(path, batch_id="one", max_attempts=2, opener=opener) as transport:
        transport.set_case("a")
        transport("POST", EXA)
        transport("POST", EXA)
        with pytest.raises(ValidationStop, match="attempt_cap"):
            transport("POST", EXA)
    with pytest.raises(ValidationStop, match="attempt_cap"):
        with BatchTransport(path, batch_id="one", max_attempts=2, opener=opener):
            pass
    assert len(calls) == 2
    assert len(json.loads(path.read_text())["attempts"]) == 2


def test_unresolved_reservation_blocks_replay(tmp_path):
    path = tmp_path / "budget.json"
    with BatchTransport(path, batch_id="one") as transport:
        transport.set_case("a")
        transport.data["attempts"].append({"state": "attempted", "number": 1})
        transport.save()
    with pytest.raises(ValidationStop, match="unresolved_attempt"):
        with BatchTransport(path, batch_id="one", opener=lambda *_a, **_kw: pytest.fail("replayed")):
            pass
    assert json.loads(path.read_text())["stop_reason"] == "unresolved_attempt"


def test_credentials_redacted_in_ledger_and_capture(tmp_path):
    path = tmp_path / "budget.json"
    body = {"text": "private-key and bearer-value", "authorization": "bearer-value"}
    def opener(*_a, **_kw):
        return HTTPResult(200, json.dumps(body).encode(), {})

    with BatchTransport(path, batch_id="one", opener=opener) as transport:
        transport.set_case("a")
        transport("GET", NCBI + "&term=bearer-value", headers={"Authorization": "Bearer bearer-value"})
    ledger = path.read_text()
    capture = (tmp_path / "budget-captures" / "attempt-01.json").read_text()
    assert "private-key" not in ledger + capture
    assert "bearer-value" not in ledger + capture
    assert "REDACTED" in ledger
    assert "text" not in capture
    assert "authorization" not in capture
    assert "headers" not in ledger


def test_redirects_consume_attempts_and_cross_origin_is_denied(tmp_path):
    path = tmp_path / "budget.json"
    calls = []
    def opener(_method, url, **_kw):
        calls.append(url)
        if len(calls) == 1:
            return HTTPResult(307, b"{}", {"Location": "/search?next=1"})
        return HTTPResult(200, b"{}", {})
    with BatchTransport(path, batch_id="one", max_attempts=2, opener=opener) as transport:
        transport.set_case("a")
        assert transport("POST", EXA).status == 200
    assert len(calls) == 2
    assert len(json.loads(path.read_text())["attempts"]) == 2

    path2 = tmp_path / "other.json"
    def cross(*_a, **_kw):
        return HTTPResult(302, b"{}", {"location": "https://api.tavily.com/search"})
    with BatchTransport(path2, batch_id="one", opener=cross) as transport:
        transport.set_case("a")
        with pytest.raises(ValidationStop, match="redirect_origin"):
            transport("POST", EXA)
    assert len(json.loads(path2.read_text())["attempts"]) == 1


def test_model_endpoint_and_disallowed_pubmed_route_never_dispatch(tmp_path):
    calls = []
    def opener(*_a, **_kw):
        calls.append(1)
        return HTTPResult(200, b"{}", {})
    for index, url in enumerate(("https://api.groq.com/openai/v1/chat/completions",
                                 "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi")):
        with BatchTransport(tmp_path / f"{index}.json", batch_id="one", opener=opener) as transport:
            transport.set_case("a")
            with pytest.raises(ValidationStop, match="endpoint_not_allowed"):
                transport("POST" if index == 0 else "GET", url)
    assert calls == []


def test_case_and_batch_deadlines_are_checked_before_dispatch(tmp_path):
    now = [1000.0]
    calls = []
    def opener(*_a, **_kw):
        calls.append(1)
        return HTTPResult(200, b"{}", {})
    with BatchTransport(tmp_path / "case.json", batch_id="one", clock=lambda: now[0], opener=opener) as transport:
        transport.set_case("a")
        now[0] = 1120.0
        with pytest.raises(ValidationStop, match="case_deadline"):
            transport("POST", EXA)
    with BatchTransport(tmp_path / "batch.json", batch_id="one", clock=lambda: now[0], opener=opener) as transport:
        transport.set_case("a")
        now[0] = 1720.0
        with pytest.raises(ValidationStop, match="batch_deadline"):
            transport("POST", EXA)
    assert calls == []


def test_transport_error_is_uncertain_and_never_replayed(tmp_path):
    path = tmp_path / "budget.json"
    with BatchTransport(path, batch_id="one", opener=lambda *_a, **_kw: (_ for _ in ()).throw(TimeoutError())) as transport:
        transport.set_case("a")
        with pytest.raises(ValidationStop, match="uncertain_transport"):
            transport("POST", EXA)
    assert json.loads(path.read_text())["attempts"][0]["state"] == "uncertain"
    with pytest.raises(ValidationStop):
        with BatchTransport(path, batch_id="one"):
            pass


def test_numeric_cost_keeps_zero_without_provider_text(tmp_path):
    packet = {"results": [{"text": "publisher supplied text"}],
              "costDollars": {"total": 0, "search": {"neural": 0.001, "note": "secret"}}}
    path = tmp_path / "budget.json"
    with BatchTransport(path, batch_id="one", opener=lambda *_a, **_kw: HTTPResult(
            200, json.dumps(packet).encode(), {})) as transport:
        transport.set_case("a")
        transport("POST", EXA)
    capture = json.loads((tmp_path / "budget-captures" / "attempt-01.json").read_text())
    assert capture["provider_usage"]["costDollars"] == {"total": 0, "search": {"neural": 0.001}}
    assert "publisher supplied text" not in path.read_text() + json.dumps(capture)
    with pytest.raises(ValueError, match="integer from 1 to 8"):
        BatchTransport(path, batch_id="two", max_attempts=True)
    with pytest.raises(ValueError, match="integer from 1 to 8"):
        BatchTransport(path, batch_id="two", max_attempts=9)


def test_timeout_is_clamped_to_request_and_remaining_case(tmp_path):
    now = [1000.0]
    observed = []

    def opener(*_args, **kwargs):
        observed.append(kwargs["timeout"])
        return HTTPResult(200, b"{}", {})

    with BatchTransport(tmp_path / "budget.json", batch_id="one", clock=lambda: now[0], opener=opener) as transport:
        transport.set_case("a")
        transport("POST", EXA, timeout=60)
        now[0] = 1118.0
        transport("POST", EXA, timeout=30)
    assert observed == [30.0, 2.0]


def test_missing_capture_blocks_restart_before_dispatch(tmp_path):
    path = tmp_path / "budget.json"
    with BatchTransport(path, batch_id="one", opener=lambda *_a, **_kw: HTTPResult(200, b"{}", {})) as transport:
        transport.set_case("a")
        transport("POST", EXA)
    (tmp_path / "budget-captures/attempt-01.json").unlink()
    with pytest.raises(ValidationStop, match="missing_or_inconsistent_capture"):
        with BatchTransport(path, batch_id="one", opener=lambda *_a, **_kw: pytest.fail("replayed")):
            pass
