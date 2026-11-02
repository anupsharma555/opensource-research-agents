"""Only a captured provider-rejected generation admits a fresh corrective turn."""

from __future__ import annotations

import json
import shutil
import sqlite3
import urllib.error
import urllib.request
from io import BytesIO

import pytest

from ora.state.store import StateStore
from tests.benchmarks import test_search_agent_wrong_type_resume as fixture
from tests.benchmarks.test_search_agent_runner import response, runner
from tests.mission.test_service import answer

GENERATED = '<tool_call><function=publish_result><parameter=text>Unfinished synthetic draft'
ERROR = {"error": {
    "type": "invalid_request_error", "code": "tool_use_failed",
    "message": "Failed to call a function. Please adjust your prompt. "
    "See 'failed_generation' for more details.",
    "failed_generation": GENERATED,
}}


def _stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(fixture, "ERROR", ERROR)
    return fixture._stopped(tmp_path, monkeypatch)


def _lineage(root, source, request):
    return runner._resume_source(root, source, request, "offline_test", **{
        key: value for key, value in fixture._resume_options().items()
        if key != "execution_mode"
    })


def test_captured_malformed_generation_resumes_without_replaying_draft(tmp_path, monkeypatch):
    source, request = _stopped(tmp_path, monkeypatch)
    prior = (source / "budget.json").read_bytes()
    before = runner._state_digest(source / "state")
    lineage = _lineage(tmp_path, source, request)
    assert lineage["source_boundary"] == "provider_rejection_pending_local_repair"
    assert lineage["pending_type_repair"]["expected_feedback"] == {
        "status": "provider_rejected_malformed_tool_generation",
    }
    sent = []

    def corrected(_handler, _class, outgoing, **_kwargs):
        packet = json.loads(outgoing.data)
        sent.append(packet)
        payload = answer("The synthetic observation remains uncertain.", 2).payload
        payload["model"] = packet["model"]
        return response(outgoing, json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", corrected)
    output = tmp_path / ".local/search-validation/malformed-resumed"
    result = runner.run_case(tmp_path, output, resume_case=source,
                             start_request=request, **fixture._resume_options())
    assert len(sent) == 1
    serialized = json.dumps(sent[0])
    assert "provider_rejected_malformed_tool_generation" in serialized
    assert "Synthetic source: measured change" in serialized
    assert "Unfinished synthetic draft" not in serialized
    assert result["resume_integrity_complete"] is True
    assert (output / "prior-cumulative-budget.json").read_bytes() == prior
    assert runner._state_digest(source / "state") == before
    assert result["cumulative_accounting"]["unknown_model_reservations"] == (
        json.loads(prior)["planned_tokens"]
    )
    store = StateStore(output / "state", read_only=True)
    capture = store.records("restricted_failed_generation")[0]
    assert store.read_blob(capture["body"]["blob"]["sha256"]).decode() == GENERATED
    assert all("Unfinished synthetic draft" not in json.dumps(item["body"])
               for item in store.records("report"))


def test_later_malformed_rejection_keeps_prior_feedback_and_both_reservations(
    tmp_path, monkeypatch,
):
    source, request = fixture._stopped(tmp_path, monkeypatch)

    def malformed(_handler, _class, outgoing, **_kwargs):
        packet = json.loads(outgoing.data)
        assert "provider_rejected_wrong_type_argument" in json.dumps(packet)
        raise urllib.error.HTTPError(outgoing.full_url, 400, "synthetic rejection", {},
                                     BytesIO(json.dumps(ERROR).encode()))

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", malformed)
    middle = tmp_path / ".local/search-validation/malformed-after-type-error"
    result = runner.run_case(tmp_path, middle, resume_case=source,
                             start_request=request, **fixture._resume_options())
    assert result["outcome"] == "model_http_error"
    before = runner._state_digest(middle / "state")
    lineage = _lineage(tmp_path, middle, request)
    assert lineage["prior_cumulative"]["unknown_model_reservations"] == (
        json.loads((middle / "cumulative-budget.json").read_text())["planned_tokens"]
    )
    sent = []

    def corrected(_handler, _class, outgoing, **_kwargs):
        packet = json.loads(outgoing.data)
        sent.append(packet)
        payload = answer("A complete synthetic bounded answer.", 2).payload
        payload["model"] = packet["model"]
        return response(outgoing, json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", corrected)
    final = tmp_path / ".local/search-validation/malformed-third-phase"
    result = runner.run_case(tmp_path, final, resume_case=middle,
                             start_request=request, **fixture._resume_options())
    assert len(sent) == 1
    assert "provider_rejected_malformed_tool_generation" in json.dumps(sent[0])
    assert "Unfinished synthetic draft" not in json.dumps(sent[0])
    assert runner._state_digest(middle / "state") == before
    assert result["resume_integrity_complete"] is True
    assert result["cumulative_accounting"]["unknown_model_reservations"] == (
        lineage["prior_cumulative"]["unknown_model_reservations"]
    )


@pytest.mark.parametrize("tamper", [
    "missing", "hash", "accepted", "actor", "generic_error", "link",
])
def test_uncertain_or_mismatched_generation_does_not_admit_resume(tmp_path, monkeypatch, tamper):
    source, request = _stopped(tmp_path, monkeypatch)
    bad = tmp_path / ".local/search-validation" / f"malformed-{tamper}"
    shutil.copytree(source, bad)
    store = StateStore(bad / "state")
    capture = store.records("restricted_failed_generation")[0]
    if tamper == "generic_error":
        receipt = store.records("model_response_receipt")[0]
        payload = json.loads(store.read_blob(receipt["body"]["blob"]["sha256"]))
        payload["provider_error"]["message"] = "A different unclassified request error"
        new_blob = store.put_blob(json.dumps(payload).encode())
        kind, row, body = "model_response_receipt", receipt, {**receipt["body"], "blob": new_blob}
    else:
        kind, row, body = "restricted_failed_generation", capture, dict(capture["body"])
        if tamper == "missing":
            body.update(status="absent", blob=None)
        elif tamper == "hash":
            body["text_sha256"] = "0" * 64
        elif tamper == "accepted":
            body["accepted_as_answer_or_tool"] = True
        elif tamper == "actor":
            body["instance_id"] = "unrelated-synthetic-peer"
    with sqlite3.connect(bad / "state/state.sqlite3") as db:
        db.execute("UPDATE records SET body=? WHERE kind=? AND id=? AND version=?",
                   (json.dumps(body), kind, row["id"], row["version"]))
        if tamper == "link":
            db.execute("DELETE FROM links WHERE relation='has_restricted_failed_generation'")
    with pytest.raises(ValueError, match="recovered rejection"):
        _lineage(tmp_path, bad, request)
