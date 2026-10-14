"""A permitted pasted copy can enter the first model turn with its exact span."""

from __future__ import annotations

import json

from ora.coordination.board import ref
from ora.evidence import EvidenceLibrary
from ora.evidence.intake import default_exact_submitted_passage
from ora.mission.source_context import plan_source_turn
from ora.state import StateStore
from tests.mission.test_revoked_source_preview import A_TEXT, B_TEXT, _followup
from tests.mission.test_service import answer, service
from tests.mission.test_source_context import control


def test_revoked_a_first_input_has_citable_permitted_b(tmp_path, monkeypatch):
    requests = []

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        response = answer("Earlier synthetic report for follow-up.", 1)
        response.headers = {"x-request-id": "offline-fixture", "x-ratelimit-limit-tokens": "8000"}
        return response

    host = service(tmp_path, monkeypatch, transport)
    try:
        run_id, a, b, _ = _followup(host)
        assert len(requests) == 1  # Only the parent used the injected transport.
        control_record = host._control(run_id, "agent-synthesis")
        passage = host.store.get("source_passage", f"{b['id']}:supplied-1")
        passage_ref = ref(passage)
        assert default_exact_submitted_passage(EvidenceLibrary(host.store), b) == passage_ref
        assert host._schedule_model(control_record)

        mission_id = host._run(run_id)["body"]["mission_id"]
        intents = [item for item in host.store.records("model_request_intent")
                   if item["body"]["mission_id"] == mission_id]
        assert len(intents) == 1
        intent = intents[0]
        assert intent["body"]["source_decisions"][0]["passage_refs"] == [passage_ref]
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        source_packets = [json.loads(message["content"])["source_packets"]
                          for message in packet["messages"]
                          if message["role"] == "user" and "source_packets" in message["content"]]
        assert len(source_packets) == 1
        assert len(source_packets[0]) == 1
        supplied = source_packets[0][0]
        assert supplied["id"] == b["id"]
        assert supplied["text_basis"] == "checked_passages"
        assert supplied["passage_refs"] == [passage_ref]
        assert f"{b['id']}:supplied-1" in supplied["text"]
        assert B_TEXT in supplied["text"]
        assert A_TEXT not in json.dumps(packet)
        budget = host.store.get("mission_call_budget", intent["body"]["call_ref"]["id"])
        assert budget["body"]["exact_complete_request_estimate_tokens"] <= 8000
        assert len(requests) == 1  # Preparing a child request never dispatches it.
    finally:
        host.close()


def test_historical_submission_without_use_keeps_raw_route_and_empty_choice(tmp_path):
    store = StateStore(tmp_path)
    submitted = "Older synthetic pasted evidence."
    blob = store.put_blob(submitted.encode("utf-8"))

    def save(tx):
        publication = tx.put("publication", "historical-publication", {"title": "Synthetic"})
        encounter = tx.put("source_encounter", "historical-encounter", {
            "publication": ref(publication), "route": "user_supplied", "status": "accepted",
        })
        copy = tx.put("source_copy", "historical-copy", {
            "publication": ref(publication), "encounter": ref(encounter),
            "mime_type": "text/plain", "rights_status": "permitted", "blob": blob,
            "content_sha256": blob["sha256"], "retain_bytes": True,
            "inspection_state": "checked", "validation_record": "exact-user-submission",
            "passage_ids": ["historical-copy:supplied-1"],
        })
        tx.put("source_passage", "historical-copy:supplied-1", {
            "copy_ref": ref(copy), "inspection_state": "checked",
            "span_id": "supplied-1", "locator": f"submitted text; chars=0:{len(submitted)}",
            "start": 0, "end": len(submitted), "text": submitted,
        })
        return ref(copy)

    copy_ref = store.transact("historical-source", {}, save, mission_id="m")
    assert default_exact_submitted_passage(EvidenceLibrary(store), copy_ref) is None
    control_ref = control(store, [copy_ref])
    normal = plan_source_turn(store, control_ref,
                              source_input_allowance_tokens=1000)
    assert normal["source_inputs"] == [{"copy_ref": copy_ref, "passage_refs": []}]

    def select_empty(tx):
        current = tx.get(**control_ref)
        changed = tx.put("mission_task_control", current["id"], {
            **current["body"], "selected_passage_refs": {copy_ref["id"]: []},
        }, expected_version=current["version"])
        return ref(changed)

    empty_ref = store.transact("select-empty", {}, select_empty, mission_id="m")
    explicit_empty = plan_source_turn(
        store, empty_ref,
        source_input_allowance_tokens=1000,
    )
    assert explicit_empty["mode"] == "source_free_recovery"
    assert explicit_empty["reason"] == "empty_selected_source_window"
