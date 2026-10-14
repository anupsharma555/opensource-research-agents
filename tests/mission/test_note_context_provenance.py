"""A note derived from visible model context retains that context's source use."""

from __future__ import annotations

import json
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import service, tool

SOURCE_TEXT = "Synthetic source-linked proposition for an offline provenance check."


@pytest.mark.parametrize("source_linked,revoke_during_model", [
    (True, False), (True, True), (False, False),
])
def test_write_note_carries_visible_context_source_ancestry(
    tmp_path, monkeypatch, source_linked, revoke_during_model,
) -> None:
    note_text = "Derived from visible context: " + SOURCE_TEXT if source_linked else "Source-free task plan."
    calls = []
    host = None
    source = None
    mission_id = None

    def revoke_source() -> None:
        def revoke(tx):
            prior = tx.get(**source["use"])
            return ref(tx.put("source_use", prior["id"], {
                **prior["body"], "allowed_uses": [], "policy_status": "revoked",
            }, expected_version=prior["version"]))

        host.store.transact("note-context-revoke", {}, revoke, mission_id=mission_id)

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        if source_linked:
            assert SOURCE_TEXT in request.data.decode()
        if len(calls) == 1:
            return tool("request_capability", {"tool_id": "write_note"}, 1)
        assert len(calls) == 2
        if revoke_during_model:
            revoke_source()
        return tool("write_note", {
            "note_id": "derived-note", "category": "hypothesis",
            "text": note_text, "expected_version": 0,
        }, 2)

    host = service(tmp_path, monkeypatch, transport)
    host.credential_profiles.is_run_enabled = lambda _profile, _run: True
    host.session.adapter.credential_profiles.is_enabled = lambda _profile: True
    try:
        run = host.start(LiveStartRequest(
            request_id="note-context-provenance", question="Record a synthetic note.",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        mission_id, actor_id = control["body"]["mission_id"], control["body"]["actor_id"]
        if source_linked:
            blob = host.store.put_blob(SOURCE_TEXT.encode("utf-8"))

            def save_source(tx):
                publication = tx.put("publication", "note-context-publication", {
                    "title": "Synthetic local note source",
                })
                encounter = tx.put("source_encounter", "note-context-encounter", {
                    "publication": ref(publication), "route": "user_supplied",
                    "status": "accepted",
                })
                copy = tx.put("source_copy", "note-context-copy", {
                    "mission_id": mission_id, "rights_status": "permitted",
                    "publication": ref(publication), "encounter": ref(encounter),
                    "mime_type": "text/plain", "blob": blob,
                    "content_sha256": blob["sha256"], "retain_bytes": True,
                    "inspection_state": "checked", "validation_record": "synthetic-local-check",
                    "passage_ids": [],
                })
                use = tx.put("source_use", "note-context-use", {
                    "copy_ref": ref(copy), "allowed_uses": ["model_input"],
                    "policy_status": "granted",
                })
                tx.bind("source_use.copy", "note-context-copy:1", "source_use", use["id"])
                return {"copy": ref(copy), "use": ref(use)}

            source = host.store.transact(
                "note-context-source", {}, save_source, mission_id=mission_id,
            )
            host.notes.write(
                "source-note", mission_id=mission_id, actor_id=actor_id,
                task_ref=control["body"]["proposal_ref"], note_id="source-note",
                category="hypothesis", text=SOURCE_TEXT,
                input_refs=[source["copy"]],
            )

        deadline = time.monotonic() + 12
        note = None
        while time.monotonic() < deadline:
            note = host.store.get("working_note", "derived-note")
            if note:
                break
            host._drive_run(host._run(run.run_id))
            time.sleep(.01)
        assert note is not None and len(calls) == 2
        write_response = next(
            item for item in host.store.records("model_response_receipt")
            if json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))[
                "tool_calls"
            ][0]["function"]["name"] == "write_note"
        )
        tool_work = next(
            item for item in host.store.records("executor_work")
            if item["body"].get("spec", {}).get("handler_id") == "mission-tool"
            and item["body"].get("spec", {}).get("call_id") ==
            write_response["body"]["call_ref"]["id"]
        )
        spec = host.executor._spec_from(tool_work)
        intent = host.store.get("model_request_intent", f"model-intent:{spec.call_id}:1")
        call = host.store.get("agent_call", spec.call_id, 1)
        expected = [source["copy"]] if source else []
        assert call["body"]["input_refs"] == []
        assert spec.input_refs == ()
        assert intent["body"]["context_source_dependency_refs"] == expected
        assert note["body"]["input_refs"] == expected

        if source:
            if not revoke_during_model:
                revoke_source()
            hidden = host.notes.read(
                mission_id=mission_id, actor_id=actor_id,
                task_id=control["body"]["task_id"], note_id="derived-note",
            )
            assert "text" not in hidden["body"]
            assert hidden["body"]["source_dependency_refs"] == expected
            assert host.notes.write(
                f"mission-note:{spec.work_id}", mission_id=mission_id,
                actor_id=actor_id, task_ref=spec.proposal_ref,
                note_id="derived-note", category="hypothesis", text=note_text,
                input_refs=expected, expected_version=0,
            ) == ref(note)
            assert host.store.get("working_note", "derived-note")["version"] == 1
        else:
            visible = host.notes.read(
                mission_id=mission_id, actor_id=actor_id,
                task_id=control["body"]["task_id"], note_id="derived-note",
            )
            assert visible["body"]["text"] == note_text
    finally:
        host.close()
