"""A captured required-field type error can resume through local repair once."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ora.mission.service import MissionService
from ora.state.store import StateStore
from tests.benchmarks.test_search_agent_runner import project, response, runner
from tests.mission.test_service import answer

ERROR = {
    "error": {
        "code": "tool_use_failed", "type": "invalid_request_error",
        "message": "tool call validation failed: parameters for tool publish_result "
        "did not match schema: errors: [`/citations`: expected array, but got string]",
    },
}


def _stopped(tmp_path, monkeypatch) -> tuple[Path, dict]:
    project(tmp_path, monkeypatch)
    request = {
        "request_id": "synthetic-wrong-type-resume",
        "question": "Summarize a synthetic supplied observation and its limits.",
        "scope": "Use only the supplied synthetic text.",
        "evidence_text": "Synthetic source: measured change with uncertain attribution.",
        "agent_ids": ["agent-synthesis"], "credential_profile": "primary",
        "agent_models": {"agent-synthesis": {
            "model": "qwen/qwen3.8-27b", "reasoning_effort": "medium",
            "max_completion_tokens": 960,
        }},
        "allow_vision": False,
    }

    def rejected(_handler, _class, outgoing, **_kwargs):
        raise urllib.error.HTTPError(
            outgoing.full_url, 400, "synthetic bad arguments", {},
            io.BytesIO(json.dumps(ERROR).encode()),
        )

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", rejected)
    source = tmp_path / ".local/search-validation/wrong-type-source"
    with monkeypatch.context() as no_repair:
        no_repair.setattr(MissionService, "_repair_tool_rejection",
                          lambda *_args, **_kwargs: False)
        result = runner.run_case(
            tmp_path, source, execution_mode="offline_test",
            start_request=request, model_limit=None, token_limit=30_000,
            retrieval_limit=0, deadline_seconds=8,
        )
    assert result["outcome"] == "model_http_error", result
    assert result["dispatch_drain_status"] == "settled"
    assert result["model_attempts"] == 1
    saved = StateStore(source / "state", read_only=True)
    receipt = saved.records("model_response_receipt")[0]
    assert receipt["body"]["http_status"] == 400
    assert receipt["body"]["error_kind"] == "http_error"
    assert saved.records("mission_task_control")[0]["body"].get("feedback") is None
    return source, request


def _resume_options() -> dict:
    return {"execution_mode": "offline_test", "model_limit": None,
            "token_limit": 30_000, "retrieval_limit": 0,
            "deadline_seconds": 8, "accounting_only": False,
            "until_complete": False}


def test_pending_wrong_type_resume_repairs_before_new_http_and_keeps_charge(
    tmp_path, monkeypatch,
):
    source, request = _stopped(tmp_path, monkeypatch)
    prior_budget = (source / "budget.json").read_bytes()
    prior_state = runner._state_digest(source / "state")
    prior = StateStore(source / "state", read_only=True)
    old_receipt = prior.records("model_response_receipt")[0]
    old_intent = prior.records("model_request_intent")[0]
    old_request = prior.read_blob(old_intent["body"]["blob"]["sha256"])
    lineage = runner._resume_source(tmp_path, source, request, "offline_test",
                                    **{k: v for k, v in _resume_options().items()
                                       if k != "execution_mode"})
    assert lineage["source_boundary"] == "provider_rejection_pending_local_repair"
    assert lineage["pending_type_repair"]["repair_state"] == "pending"
    assert lineage["pending_type_repair"]["expected_type"] == "array"
    assert lineage["pending_type_repair"]["observed_type"] == "string"
    old_charge = json.loads(prior_budget)["planned_tokens"]
    assert lineage["next_phase_limits"]["tokens"] == 30_000 - old_charge
    sent = []

    def corrected(_handler, _class, outgoing, **_kwargs):
        packet = json.loads(outgoing.data)
        sent.append(packet)
        payload = answer("The synthetic observation has uncertain attribution.", 2).payload
        payload["model"] = packet["model"]
        return response(outgoing, json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", corrected)
    output = tmp_path / ".local/search-validation/wrong-type-resumed"
    continued = runner.run_case(tmp_path, output, resume_case=source,
                                start_request=request, **_resume_options())
    assert continued["resume_lineage"]["source_boundary"] == (
        "provider_rejection_pending_local_repair"
    )
    assert len(sent) == 1
    assert "provider_rejected_wrong_type_argument" in json.dumps(sent[0]["messages"])
    assert "Synthetic source: measured change" in json.dumps(sent[0]["messages"])
    assert (output / "prior-cumulative-budget.json").read_bytes() == prior_budget
    assert runner._state_digest(source / "state") == prior_state
    copied = StateStore(output / "state", read_only=True)
    assert copied.get("model_response_receipt", old_receipt["id"]) == old_receipt
    assert copied.read_blob(old_intent["body"]["blob"]["sha256"]) == old_request
    cumulative = json.loads((output / "cumulative-budget.json").read_text())
    assert cumulative["model_attempts"][0] == json.loads(prior_budget)["model_attempts"][0]
    assert cumulative["planned_tokens"] == old_charge + json.loads(
        (output / "budget.json").read_text())["planned_tokens"]
    assert continued["cumulative_accounting"]["unknown_model_reservations"] >= old_charge


@pytest.mark.parametrize("tamper", [
    "unregistered", "ambiguous", "stale_source", "dispatch", "descriptor",
    "cap_charge",
])
def test_pending_wrong_type_resume_rejects_unmatched_saved_boundary(
    tmp_path, monkeypatch, tamper,
):
    source, request = _stopped(tmp_path, monkeypatch)
    bad = tmp_path / ".local/search-validation" / f"wrong-type-{tamper}"
    shutil.copytree(source, bad)
    state = bad / "state"
    if tamper in {"unregistered", "ambiguous"}:
        store = StateStore(state)
        receipt = store.records("model_response_receipt")[0]
        payload = json.loads(store.read_blob(receipt["body"]["blob"]["sha256"]))
        error = payload["provider_error"]
        error["message"] = (error["message"].replace(
            "publish_result", "unknown_tool") if tamper == "unregistered"
            else error["message"].replace("]", ", `/limitations`: expected array, but got string]"))
        new_blob = store.put_blob(json.dumps(payload, sort_keys=True,
                                              separators=(",", ":")).encode())
        with sqlite3.connect(state / "state.sqlite3") as db:
            db.execute("UPDATE records SET body=? WHERE kind='model_response_receipt' "
                       "AND id=? AND version=?", (json.dumps({
                           **receipt["body"], "blob": new_blob,
                       }), receipt["id"], receipt["version"]))
            db.commit()
    else:
        kind = ("source_use" if tamper == "stale_source" else
                "model_dispatch_attempt" if tamper == "dispatch" else
                "capacity_operation" if tamper == "cap_charge" else "agent_call")
        with sqlite3.connect(state / "state.sqlite3") as db:
            record_id, version, body = db.execute(
                "SELECT r.id,r.version,r.body FROM records r "
                "JOIN heads h USING(kind,id,version) WHERE r.kind=? LIMIT 1", (kind,),
            ).fetchone()
            changed = json.loads(body)
            if tamper == "stale_source":
                changed["policy_status"] = "requires_source_policy_resolution"
                changed["allowed_uses"] = []
            elif tamper == "dispatch":
                changed["status"] = "attempted_external_effect"
            elif tamper == "cap_charge":
                changed["attempts"][-1]["charged_tokens"] += 1
            else:
                changed["permitted_tools"]["publish_result"]["schema"]["properties"][
                    "citations"
                ]["type"] = "string"
            db.execute("UPDATE records SET body=? WHERE kind=? AND id=? AND version=?",
                       (json.dumps(changed), kind, record_id, version))
            db.commit()
    with pytest.raises(ValueError, match="recovered rejection"):
        runner._resume_source(tmp_path, bad, request, "offline_test",
                              **{k: v for k, v in _resume_options().items()
                                 if k != "execution_mode"})
