"""Requester access to an exact reply while its producing peer is still active."""

from __future__ import annotations

import json
import socket
import time
from dataclasses import replace

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextError, WorkingContext
from ora.mission.report_lineage import report_text_access
from ora.retrieval.providers import HTTPResult
from ora.state import StateStore
from tests.mission.test_service import _search_spec, serialized_peer_name, service, tool


def _drive_until(host, run_id: str, predicate) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Provisional answer did not reach its saved boundary")


def test_continuing_peer_answer_is_previewed_and_read_only_by_its_requester(
    tmp_path, monkeypatch,
) -> None:
    def no_socket(*_args, **_kwargs):
        raise AssertionError("Offline peer access test must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", no_socket)
    host = None
    run_id = None
    turns = {"Sage": 0, "Mxra": 0}
    text = "A provisional synthetic reply; another peer review is still open."
    seen: dict = {}

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        role = serialized_peer_name(packet)
        turns[role] += 1
        step = turns[role]
        index = json.loads(packet["messages"][-1]["content"])
        if step == 1:
            incoming = "to-mxra" if role == "Mxra" else "to-sage"
            assert index["incoming_request"]["ref"]["id"] == incoming
            return tool("publish_result", {
                "title": f"{role} provisional reply", "text": text,
                "citations": [], "limitations": ["Synthetic evidence only"],
                "answer_request_id": incoming,
            }, 100 + step if role == "Mxra" else step)
        if role == "Mxra" and step == 2:
            answer = host.store.get("mail_request", "to-sage")["body"]["answer_refs"][0]
            report_ref = host.store.get(**answer)["body"]["artifact_ref"]
            card = next(item for item in index["peer_answer_previews"]
                        if item["answer_ref"] == answer)
            assert card["question"] == "Mxra asks for an independent synthetic check."
            assert card["report_ref"] == report_ref
            assert card["report"]["body"] == text
            assert card["report_delivery"] == "complete_in_input"
            inclusion_id = index["older_records"]["inclusion_id"]
            seen.update(inclusion_id=inclusion_id,
                        report_ref=report_ref, answer_ref=answer)
            return tool("read_context_record", {
                "inclusion_id": inclusion_id, **report_ref, "offset": 0, "limit": 2000,
            }, 102)
        if role == "Mxra" and step == 3:
            result = next(json.loads(message["content"])
                          for message in reversed(packet["messages"])
                          if message["role"] == "tool"
                          and message.get("name") == "read_context_record")
            assert result["report_text_status"] == "available"
            assert json.loads(result["text"])["body"] == text
            seen["read_received"] = True
            answer = host.store.get("mail_request", "to-sage")["body"]["answer_refs"][0]
            return tool("review_peer_answer", {
                "request_id": "to-sage", "answer_id": answer["id"],
                "disposition": "answered", "reason": "I read the linked provisional reply.",
            }, 103)
        raise AssertionError(f"Unexpected {role} turn {step}")

    host = service(tmp_path, monkeypatch, transport)
    host.discovery_transport = lambda *_args, **_kw: HTTPResult(200, json.dumps({
        "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
            "source": "MED", "id": "9876", "pmid": "9876",
            "title": "Synthetic checked source",
            "abstractText": "Synthetic evidence for the provisional reply. " * 4,
        }]},
    }).encode(), {})
    try:
        run = host.start(LiveStartRequest(
            request_id="provisional-peer-answer-access",
            question="Exchange two synthetic independent checks.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        run_id = run.run_id
        sage = host._control(run_id, "agent-synthesis")
        mxra = host._control(run_id, "agent-critique")
        mission_id = sage["body"]["mission_id"]
        spec = replace(_search_spec(host, run_id, suffix="provisional-answer"),
                       actor_id=sage["body"]["actor_id"],
                       proposal_ref=sage["body"]["proposal_ref"])
        found, _ = host._search(spec, {"route": "europe_pmc.search",
                                       "query": "synthetic", "limit": 1})
        inspected, outputs = host._inspect_hit(spec, {"hit_id": found["hits"][0]["hit_id"]})
        assert inspected["status"] == "inspected" and len(outputs) == 1
        source_ref = outputs[0]
        host._update_control(sage, source_refs=[source_ref])
        for request_id, sender, recipient, question in (
            ("to-mxra", sage, mxra, "Sage asks for an independent synthetic check."),
            ("to-sage", mxra, sage, "Mxra asks for an independent synthetic check."),
        ):
            result = host.mailbox.send_request(
                f"send-{request_id}", mission_id=mission_id,
                request_id=request_id, message_id=f"message-{request_id}",
                requester_id=sender["body"]["actor_id"],
                recipient_id=recipient["body"]["actor_id"], question=question,
                expected_artifact="A short report", cause_ref=sender["body"]["proposal_ref"],
                payload={"question": question}, task_ref=sender["body"]["proposal_ref"],
            )
            assert result["status"] == "accepted"
            host.mailbox.mark_delivered(f"deliver-{request_id}", mission_id=mission_id,
                                        message_id=f"message-{request_id}")
        sage = host._control(run_id, "agent-synthesis")
        host._update_control(sage, phase="retry_at", retry_at=time.time() + 3600,
                             waiting_reason="Fixture lets Mxra reply first")
        _drive_until(host, run_id, lambda: bool(host.store.get("mail_request", "to-mxra")[
            "body"]["answer_refs"]))
        host._update_control(host._control(run_id, "agent-synthesis"),
                             phase="ready_model", waiting_reason=None, retry_at=None)
        _drive_until(host, run_id, lambda: bool(host.store.get("mail_request", "to-sage")[
            "body"]["answer_refs"]))
        _drive_until(host, run_id, lambda: seen.get("read_received") is True)
        assert host._control(run_id, "agent-synthesis")["body"].get("report_ref") is None
        report = host.store.get(**seen["report_ref"])
        assert report_text_access(host.store, report)["source_dependency_refs"] == [source_ref]
        assert host.store.get("mail_request", "to-sage")["body"]["answer_refs"] == [
            seen["answer_ref"]
        ]
        inclusion_ref = {"kind": "context_inclusion", "id": seen["inclusion_id"],
                         "version": 1}
        inclusion = host.store.get(**inclusion_ref)
        assert seen["answer_ref"] in inclusion["body"]["included_refs"]
        # This normal packet also happens to have an older omitted-answer route.
        # Isolate the included-answer route so its current-link guard is tested.
        included_only_ref = ref(host.store.transact(
            "included-answer-route", {}, lambda tx: tx.put(
                "context_inclusion", f"{inclusion['id']}:included-only", {
                    **inclusion["body"],
                    "omitted": [item for item in inclusion["body"]["omitted"]
                                if item.get("fetch") != seen["answer_ref"]],
                }), mission_id=mission_id,
        ))
        mxra = host._control(run_id, "agent-critique")
        sage = host._control(run_id, "agent-synthesis")
        scope = {"mission_id": mission_id, "actor_id": mxra["body"]["actor_id"],
                 "task_id": mxra["body"]["task_id"], "inclusion_ref": included_only_ref,
                 "offset": 0, "limit": 2000}
        window = host.context.read_omitted_record(
            **scope, pointer=seen["report_ref"],
        )
        assert window["report_text_status"] == "available"
        assert json.loads(window["text"])["body"] == text
        assert not any(item.get("fetch") == seen["report_ref"]
                       for item in inclusion["body"]["omitted"])
        shared_ref = ref(host.store.transact(
            "reactivated-shared-answer-route", {}, lambda tx: tx.put(
                "context_inclusion", f"{inclusion['id']}:reactivated-shared", {
                    **inclusion["body"],
                    "omitted": [*inclusion["body"]["omitted"], {
                        "type": "shared_material", "ref": seen["report_ref"],
                        "fetch": seen["report_ref"],
                    }],
                }), mission_id=mission_id,
        ))
        shared_scope = {**scope, "inclusion_ref": shared_ref}
        # This report was accepted, then its producer's current control woke
        # and cleared report_ref. The exact mailbox answer still authorizes
        # this requester's saved read, including after a read-only reopen.
        reopened = WorkingContext(StateStore(tmp_path, read_only=True))
        shared_cursor = len(inclusion["body"]["omitted"])
        shared_page_scope = {key: shared_scope[key] for key in (
            "mission_id", "actor_id", "task_id", "inclusion_ref",
        )}
        shared_card = reopened.read_omitted_page(
            **shared_page_scope, cursor=shared_cursor, limit=1,
        )["items"][0]["summary"]
        assert shared_card["status"] == "peer_answer_text_available"
        assert shared_card["read_request"]["id"] == seen["report_ref"]["id"]
        first_shared = reopened.read_omitted_record(
            **shared_scope, pointer=seen["report_ref"],
        )
        assert first_shared["report_text_status"] == "available"
        assert json.loads(first_shared["text"])["body"] == text
        assert reopened.read_omitted_record(
            **shared_scope, pointer=seen["report_ref"],
        ) == first_shared
        with pytest.raises(ContextError):
            reopened.read_omitted_record(
                **{**shared_scope, "actor_id": sage["body"]["actor_id"]},
                pointer=seen["report_ref"],
            )
        with pytest.raises(ContextError):
            reopened.read_omitted_record(
                **{**shared_scope, "task_id": sage["body"]["task_id"]},
                pointer=seen["report_ref"],
            )
        with pytest.raises(ContextError):
            reopened.read_omitted_record(
                **shared_scope, pointer={**seen["report_ref"], "version": 2},
            )
        with pytest.raises(ContextError, match="another peer or task"):
            host.context.read_omitted_record(
                **{**scope, "actor_id": sage["body"]["actor_id"]},
                pointer=seen["report_ref"],
            )
        with pytest.raises(ContextError, match="another peer or task"):
            host.context.read_omitted_record(
                **{**scope, "task_id": sage["body"]["task_id"]},
                pointer=seen["report_ref"],
            )
        other_answer_ref = host.store.get("mail_request", "to-mxra")["body"][
            "answer_refs"
        ][0]
        other_report_ref = host.store.get(**other_answer_ref)["body"]["artifact_ref"]
        assert host.context.read_omitted_record(
            **scope, pointer=other_report_ref,
        )["report_text_status"] == "available"
        unlinked_report = host.store.transact(
            "unlinked-provisional-report", {}, lambda tx: tx.put(
                "report", "unlinked-provisional-report", {
                    "mission_id": mission_id, "title": "Unlinked synthetic report",
                    "body": "This record has no route through the saved inclusion.",
                    "citations": [], "limitations": [],
                }), mission_id=mission_id,
        )
        with pytest.raises(ContextError, match="not omitted"):
            host.context.read_omitted_record(**scope, pointer=ref(unlinked_report))
        with pytest.raises(ContextError, match="exact omitted record reference"):
            host.context.read_omitted_record(
                **scope, pointer={**seen["report_ref"], "version": "1"},
            )
        use = host.store.resolve("source_use.copy", f"{source_ref['id']}:{source_ref['version']}")
        host.store.transact("unknown-provisional-source", {}, lambda tx: tx.put(
            "source_use", use["id"], {
                **use["body"], "policy_status": "unknown",
            }, expected_version=use["version"]), mission_id=mission_id)
        unknown_shared = reopened.read_omitted_record(
            **shared_scope, pointer=seen["report_ref"],
        )
        assert unknown_shared["report_text_status"] == "data_unavailable_due_source_use"
        assert text not in unknown_shared["text"]
        unknown_card = reopened.read_omitted_page(
            **shared_page_scope, cursor=shared_cursor, limit=1,
        )["items"][0]["summary"]
        assert unknown_card["status"] == "peer_answer_text_withheld"
        assert unknown_card.get("title") is None
        use = host.store.resolve("source_use.copy", f"{source_ref['id']}:{source_ref['version']}")
        host.store.transact("revoke-provisional-source", {}, lambda tx: tx.put(
            "source_use", use["id"], {
                **use["body"], "policy_status": "revoked", "allowed_uses": [],
            }, expected_version=use["version"]), mission_id=mission_id)
        revoked_window = host.context.read_omitted_record(
            **scope, pointer=seen["report_ref"],
        )
        assert revoked_window["report_text_status"] == "data_unavailable_due_source_use"
        assert text not in revoked_window["text"]
        revoked_shared = reopened.read_omitted_record(
            **shared_scope, pointer=seen["report_ref"],
        )
        assert revoked_shared["report_text_status"] == "data_unavailable_due_source_use"
        assert text not in revoked_shared["text"]
        revoked_card = reopened.read_omitted_page(
            **shared_page_scope, cursor=shared_cursor, limit=1,
        )["items"][0]["summary"]
        assert revoked_card["status"] == "peer_answer_text_withheld"
        assert revoked_card.get("title") is None
        request = host.store.get("mail_request", "to-sage")
        answer = host.store.get(**seen["answer_ref"])
        assert host.context._peer_answer_preview(
            request, answer, mission_id=mission_id,
            actor_id=mxra["body"]["actor_id"], task_id=mxra["body"]["task_id"],
            inclusion_id=seen["inclusion_id"],
        ) is None
        current = host.store.get("mail_request", "to-sage")
        host.store.transact("remove-synthetic-answer-link", {}, lambda tx: tx.put(
            "mail_request", current["id"], {
                **current["body"], "answer_refs": [],
            }, expected_version=current["version"]), mission_id=mission_id)
        with pytest.raises(ContextError, match="not omitted"):
            host.context.read_omitted_record(**scope, pointer=seen["report_ref"])
        with pytest.raises(ContextError, match="Shared material is no longer available"):
            reopened.read_omitted_record(
                **shared_scope, pointer=seen["report_ref"],
            )
        unlinked_card = reopened.read_omitted_page(
            **shared_page_scope, cursor=shared_cursor, limit=1,
        )["items"][0]["summary"]
        assert unlinked_card == {"kind": "report", "status": "shared_material_unavailable"}
    finally:
        host.close()
