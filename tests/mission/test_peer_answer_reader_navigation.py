"""An exact answer read can carry a bounded, currently permitted report window."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextError
from ora.mission.report_lineage import report_text_access
from ora.retrieval.providers import HTTPResult
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack
from tests.mission.test_peer_answer_preview import _fixture, _pack
from tests.mission.test_service import _search_spec, answer, service


@pytest.mark.parametrize("report_body,complete", [
    ("A short synthetic assessment.", True),
    ("Long assessment. " * 180, False),
])
def test_answer_reader_supplies_permitted_report_without_another_pointer_hunt(
    tmp_path, monkeypatch, report_body, complete,
):
    store = StateStore(tmp_path)
    saved = _fixture(store, body=report_body)
    context, pack, _index = _pack(store, saved, allowance=1800)
    inclusion_ref = bind_no_source_pack(store, context, pack, "answer-navigation")
    scope = {"mission_id": "m", "actor_id": "requester", "task_id": "requester-task",
             "inclusion_ref": inclusion_ref}

    answer = context.read_omitted_record(
        **scope, pointer=saved["answer_ref"], offset=0, limit=2000,
    )
    assert answer["schema"] == "ora.context_record_window.v1"
    assert answer["ref"] == saved["answer_ref"]
    assert json.loads(answer["text"])["artifact_ref"] == saved["report_ref"]
    assert answer["returned_chars"] == len(answer["text"])
    with monkeypatch.context() as without_sidecar:
        without_sidecar.setattr(context, "_peer_answer_material", lambda *_a, **_kw: None)
        baseline = context.read_omitted_record(
            **scope, pointer=saved["answer_ref"], offset=0, limit=2000,
        )
    assert {key: answer[key] for key in baseline} == baseline
    assert answer["peer_review"] == {
        "action": "review_peer_answer", "request_ref": saved["request_ref"],
        "answer_ref": saved["answer_ref"],
        "authority": "model_authored_peer_data_not_verified_evidence",
        "question": "Please review this synthetic claim.",
    }
    sidecar = answer["related_report_window"]
    assert sidecar["ref"] == saved["report_ref"]
    assert sidecar["report_text_status"] == "available"
    assert len(answer["text"]) + len(sidecar["text"]) <= answer["effective_limit"]
    assert answer["related_report_complete"] is complete
    if complete:
        assert "related_report_read_request" not in answer
    else:
        assert answer["related_report_read_request"] == sidecar["next_request"]
        assert {key: answer["related_report_read_request"][key]
                for key in ("kind", "id", "version")} == saved["report_ref"]
    parts = [sidecar["text"]]
    while sidecar["next_offset"] is not None:
        next_read = sidecar["next_request"]
        sidecar = context.read_omitted_record(
            **scope, pointer=saved["report_ref"], offset=next_read["offset"],
            limit=next_read["limit"],
        )
        parts.append(sidecar["text"])
    assert json.loads("".join(parts))["body"] == report_body

    with pytest.raises(ContextError, match="outside this peer|another peer"):
        context.read_omitted_record(
            **{**scope, "actor_id": "other"}, pointer=saved["answer_ref"],
            offset=0, limit=2000,
        )
    with pytest.raises(ContextError, match="outside this peer|another peer"):
        context.read_omitted_record(
            **{**scope, "task_id": saved["peer_task_ref"]["id"]},
            pointer=saved["answer_ref"], offset=0, limit=2000,
        )
    with pytest.raises(ContextError, match="not omitted"):
        context.read_omitted_record(
            **scope, pointer={**saved["answer_ref"], "version": 2},
            offset=0, limit=2000,
        )


def test_reactivated_shared_report_without_producer_lineage_stays_denied(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store)
    context, pack, _index = _pack(store, saved, allowance=1800)
    inclusion_ref = bind_no_source_pack(store, context, pack, "reactivated-legacy-report")
    assert any(item.get("type") == "shared_material"
               and item.get("fetch") == saved["report_ref"]
               for item in pack.omitted)
    control = store.get("mission_task_control", "responder-control")
    proposal = store.get("board_proposal", saved["peer_task_ref"]["id"])
    def reactivate(tx):
        tx.put("mission_task_control", control["id"], {
            **control["body"], "phase": "ready_model", "report_ref": None,
        }, expected_version=control["version"])
        tx.put("board_proposal", proposal["id"], {
            **proposal["body"], "status": "open",
        }, expected_version=proposal["version"])
        return {"status": "reactivated"}

    store.transact("reactivate-legacy-producer", {}, reactivate, mission_id="m")
    card = context.read_omitted_page(
        mission_id="m", actor_id="requester", task_id="requester-task",
        inclusion_ref=inclusion_ref,
        cursor=next(index for index, item in enumerate(pack.omitted)
                    if item.get("fetch") == saved["report_ref"]),
        limit=1,
    )["items"][0]["summary"]
    assert card == {"kind": "report", "status": "shared_material_unavailable"}
    with pytest.raises(ContextError, match="Shared material is no longer available"):
        context.read_omitted_record(
            mission_id="m", actor_id="requester", task_id="requester-task",
            inclusion_ref=inclusion_ref, pointer=saved["report_ref"],
            offset=0, limit=2000,
        )


def test_long_peer_question_has_exact_reader_without_a_prefix(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store, body="A short synthetic assessment.")
    request = store.get(**saved["request_ref"])
    store.transact("long-reader-question", {}, lambda tx: tx.put(
        "mail_request", request["id"], {
            **request["body"], "question": "q" * 601,
        }, expected_version=request["version"]), mission_id="m")
    current_request = store.get("mail_request", request["id"])
    context, pack, _index = _pack(store, saved, allowance=1800)
    inclusion_ref = bind_no_source_pack(store, context, pack, "long-reader-question")
    scope = {"mission_id": "m", "actor_id": "requester", "task_id": "requester-task",
             "inclusion_ref": inclusion_ref}
    answer = context.read_omitted_record(
        **scope, pointer=saved["answer_ref"], offset=0, limit=2000,
    )
    review = answer["peer_review"]
    assert "question" not in review
    assert review["question_status"] == "not_in_input_question_exceeds_window"
    assert review["question_chars"] == 601
    assert "q" * 100 not in canonical(answer)
    question_read = review["read_full_question"]
    assert {key: question_read[key] for key in ("kind", "id", "version")} == ref(
        current_request
    )
    question_window = context.read_omitted_record(
        **scope, pointer=ref(current_request), offset=question_read["offset"],
        limit=question_read["limit"],
    )
    assert json.loads(question_window["text"])["question"] == "q" * 601


def test_answer_reader_withholds_report_if_current_source_access_is_unavailable(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store, citations=[{"sourceId": "unchecked", "spanId": "unread"}])
    context, pack, _index = _pack(store, saved, allowance=1800)
    inclusion_ref = bind_no_source_pack(store, context, pack, "withheld-answer-navigation")
    answer = context.read_omitted_record(
        mission_id="m", actor_id="requester", task_id="requester-task",
        inclusion_ref=inclusion_ref, pointer=saved["answer_ref"],
        offset=0, limit=2000,
    )
    assert json.loads(answer["text"])["artifact_ref"] == saved["report_ref"]
    assert "related_report_window" not in answer
    assert "related_report_read_request" not in answer


def test_answer_reader_keeps_parent_window_when_no_report_room(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store)
    context, pack, _index = _pack(store, saved, allowance=1800)
    inclusion_ref = bind_no_source_pack(store, context, pack, "no-report-room")
    answer = context.read_omitted_record(
        mission_id="m", actor_id="requester", task_id="requester-task",
        inclusion_ref=inclusion_ref, pointer=saved["answer_ref"],
        offset=0, limit=1,
    )
    assert answer["text"] == "{"
    assert answer["next_offset"] == 1
    assert "related_report_window" not in answer
    assert answer["related_report_read_request"]["offset"] == 0
    full_answer = context.read_omitted_record(
        mission_id="m", actor_id="requester", task_id="requester-task",
        inclusion_ref=inclusion_ref, pointer=saved["answer_ref"],
        offset=0, limit=2000,
    )
    narrow = context.read_omitted_record(
        mission_id="m", actor_id="requester", task_id="requester-task",
        inclusion_ref=inclusion_ref, pointer=saved["answer_ref"],
        offset=0, limit=len(full_answer["text"]) + 1,
    )
    assert narrow["text"] == full_answer["text"]
    assert narrow["related_report_window"]["returned_chars"] == 1
    assert narrow["related_report_window"]["next_request"]["limit"] == 2000
    assert narrow["related_report_read_request"] == narrow["related_report_window"][
        "next_request"
    ]


def test_visible_sidecar_is_exactly_rechecked_and_binds_report_dependencies(
    tmp_path, monkeypatch,
):
    store = StateStore(tmp_path)
    saved = _fixture(store)
    context, pack, index = _pack(store, saved, allowance=1800)
    inclusion_ref = bind_no_source_pack(store, context, pack, "visible-answer-sidecar")
    source_ref = {"kind": "source_copy", "id": "synthetic-source", "version": 1}
    monkeypatch.setattr("ora.mission.context.report_text_access", lambda _store, _report: {
        "status": "available", "source_dependency_refs": [source_ref],
        "unavailable_source_refs": [], "dependency_lineage_complete": True,
    })
    answer = context.read_omitted_record(
        mission_id="m", actor_id="requester", task_id="requester-task",
        inclusion_ref=inclusion_ref, pointer=saved["answer_ref"],
        offset=0, limit=2000,
    )
    arguments = {"inclusion_id": inclusion_ref["id"], **saved["answer_ref"],
                 "offset": 0, "limit": 2000}
    assistant = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "answer-read", "type": "function", "function": {
            "name": "read_context_record", "arguments": canonical(arguments),
        },
    }]}
    tool = {"role": "tool", "tool_call_id": "answer-read",
            "name": "read_context_record", "content": canonical(answer)}
    # Isolate the delivered tool result from the pack's optional peer preview.
    index.pop("peer_answer_previews", None)
    index.pop("peer_answer_review_cues", None)
    index.pop("shared_materials", None)
    visible = replace(pack, messages=[*pack.messages[:-1], assistant, tool, {
        **pack.messages[-1], "content": canonical(index),
    }])
    assert context.context_source_dependency_refs(visible) == (source_ref,)

    altered = {**answer, "related_report_window": {
        **answer["related_report_window"], "text": "Forged report text",
    }}
    forged = replace(visible, messages=[*visible.messages[:-2], {
        **tool, "content": canonical(altered),
    }, visible.messages[-1]])
    with pytest.raises(ContextError, match="peer answer window changed"):
        context.context_source_dependency_refs(forged)

    monkeypatch.setattr("ora.mission.context.report_text_access", lambda _store, _report: {
        "status": "data_unavailable_due_source_use", "source_dependency_refs": [source_ref],
        "unavailable_source_refs": [source_ref], "dependency_lineage_complete": True,
    })
    with pytest.raises(ContextError, match="peer answer window changed"):
        context.context_source_dependency_refs(visible)


def test_saved_answer_sidecar_is_removed_on_access_loss_before_replay(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    saved = _fixture(store)
    context, pack, _index = _pack(store, saved, allowance=1800)
    inclusion_ref = bind_no_source_pack(store, context, pack, "replay-answer-sidecar")
    arguments = {"inclusion_id": inclusion_ref["id"], **saved["answer_ref"],
                 "offset": 0, "limit": 2000}
    answer = context.read_omitted_record(
        mission_id="m", actor_id="requester", task_id="requester-task",
        inclusion_ref=inclusion_ref, pointer=saved["answer_ref"],
        offset=0, limit=2000,
    )
    assert "related_report_window" in answer
    response_blob = store.put_blob(canonical({"content": None, "tool_calls": [{
        "id": "saved-answer-read", "type": "function", "function": {
            "name": "read_context_record", "arguments": canonical(arguments),
        },
    }]}).encode())
    response_ref = store.transact("saved-answer-read", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "saved-answer-read", {
            "mission_id": "m", "task_id": "requester-task",
            "call_ref": saved["call_ref"], "status": "tool_requested",
            "blob": response_blob,
        },
    )), mission_id="m")
    context.save_tool_result(
        "saved-answer-result", response_ref=response_ref,
        tool_call_id="saved-answer-read", name="read_context_record",
        content=canonical(answer),
    )
    before, _ = context._tool_exchange(response_ref)
    assert json.loads(before[-1]["content"])["related_report_window"]["text"]

    monkeypatch.setattr("ora.mission.context.report_text_access", lambda _store, _report: {
        "status": "data_unavailable_due_source_use", "source_dependency_refs": [],
        "unavailable_source_refs": [], "dependency_lineage_complete": False,
    })
    after, receipt = context._tool_exchange(response_ref)
    replayed = json.loads(after[-1]["content"])
    assert replayed["ref"] == saved["answer_ref"]
    assert replayed["text"] == answer["text"]
    assert "related_report_window" not in replayed
    assert receipt["status"] == "report_current_use_rechecked"


@pytest.mark.parametrize("policy_status", [
    "revoked", "requires_source_policy_resolution",
])
def test_current_source_use_loss_removes_saved_report_sidecar(
    tmp_path, monkeypatch, policy_status,
):
    report_body = "A synthetic source-backed peer assessment."
    host = service(tmp_path, monkeypatch, lambda _request, **_kw: answer(report_body, 1))
    host.discovery_transport = lambda *_args, **_kw: HTTPResult(200, json.dumps({
        "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
            "source": "MED", "id": "9876", "pmid": "9876",
            "title": "Synthetic checked source",
            "abstractText": "Synthetic source passage. " * 8,
        }]},
    }).encode(), {})
    try:
        run = host.start(LiveStartRequest(
            request_id="source-backed-answer-reader", question="Check a synthetic claim",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        requester = host._control(run.run_id, "agent-synthesis")
        responder = host._control(run.run_id, "agent-critique")
        mission_id = requester["body"]["mission_id"]
        spec = replace(
            _search_spec(host, run.run_id, suffix="answer-reader"),
            actor_id=responder["body"]["actor_id"],
            proposal_ref=responder["body"]["proposal_ref"],
        )
        found, _ = host._search(spec, {"route": "europe_pmc.search",
                                       "query": "synthetic", "limit": 1})
        inspected, copies = host._inspect_hit(spec, {"hit_id": found["hits"][0]["hit_id"]})
        assert inspected["status"] == "inspected" and len(copies) == 1
        copy_ref = copies[0]
        host._update_control(responder, source_refs=[copy_ref])
        request = host.mailbox.send_request(
            "send-source-backed-answer", mission_id=mission_id,
            request_id="source-backed-answer", message_id="source-backed-message",
            requester_id=requester["body"]["actor_id"],
            recipient_id=responder["body"]["actor_id"],
            question="Check the synthetic source.", expected_artifact="A report",
            cause_ref=requester["body"]["proposal_ref"],
            payload={"question": "Check the synthetic source."},
            task_ref=requester["body"]["proposal_ref"],
        )
        assert request["status"] == "accepted"
        host.mailbox.mark_delivered(
            "deliver-source-backed-answer", mission_id=mission_id,
            message_id="source-backed-message",
        )
        assert host._schedule_model(host._control(run.run_id, "agent-critique"))
        asyncio.run(host.executor.run_ready(mission_id))
        assert host._advance(host._control(run.run_id, "agent-critique"))
        report = host.store.records("report")[0]
        assert report_text_access(host.store, report)["source_dependency_refs"] == [copy_ref]
        recorded = host.mailbox.record_answer(
            "record-source-backed-answer", mission_id=mission_id,
            request_id="source-backed-answer", answer_id="source-backed-answer-record",
            responder_id=responder["body"]["actor_id"], artifact_ref=ref(report),
        )
        assert recorded["status"] == "accepted"
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        requester = host._control(run.run_id, "agent-synthesis")
        call_ref = requester["body"]["call_ref"]
        inclusion_ref = {"kind": "context_inclusion",
                         "id": f"context:model-intent:{call_ref['id']}:{call_ref['version']}",
                         "version": 1}
        read_scope = {"mission_id": mission_id,
                      "actor_id": requester["body"]["actor_id"],
                      "task_id": requester["body"]["task_id"],
                      "inclusion_ref": inclusion_ref, "pointer": recorded["answer_ref"],
                      "offset": 0, "limit": 2000}
        first = host.context.read_omitted_record(**read_scope)
        assert first["related_report_window"]["report_text_status"] == "available"
        assert report_body in first["related_report_window"]["text"]
        assert first["related_report_window"]["source_dependencies_omitted_from_input"] == [
            copy_ref
        ]

        use = host.store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
        host.store.transact("revoke-answer-reader-source", {}, lambda tx: tx.put(
            "source_use", use["id"], {
                **use["body"], "policy_status": policy_status,
                "allowed_uses": (["model_input"] if policy_status ==
                                 "requires_source_policy_resolution" else []),
            }, expected_version=use["version"]), mission_id=mission_id)
        after = host.context.read_omitted_record(**read_scope)
        assert after["text"] == first["text"]
        assert "related_report_window" not in after
        assert report_body not in canonical(after)
        assert report_text_access(host.store, report)["status"] == (
            "data_unavailable_due_source_use"
        )

        safe_note = host.notes.write(
            "unrelated-safe-reader-note", mission_id=mission_id,
            actor_id=requester["body"]["actor_id"],
            task_ref=requester["body"]["proposal_ref"],
            note_id="unrelated-safe-reader-note", category="note",
            text="UNRELATED_SOURCE_FREE_READER_NOTE",
        )
        current = host.context.assemble(
            requester["body"]["call_ref"], requester["body"]["proposal_ref"],
            control_ref=ref(requester), note_refs=[safe_note],
        )
        serialized = canonical(current.messages)
        assert report_body not in serialized
        assert "UNRELATED_SOURCE_FREE_READER_NOTE" in serialized
    finally:
        host.close()
