"""A model summary keeps exact task-memory revisions as freshness inputs."""

from __future__ import annotations

import json

from ora.coordination.board import ref
from ora.mission.context import WorkingContext
from ora.mission.conversation import save_compaction
from ora.mission.memory import WorkingNotes
from ora.mission.planning import planning_note_id
from ora.state import StateStore
from tests.mission.test_context import fixture
from tests.mission.test_tool_result_paging import save_result


def test_compaction_marks_changed_task_note_and_peer_request_stale_after_restart(tmp_path):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    note = WorkingNotes(store).write(
        "freshness-note", mission_id="m", actor_id="peer", task_ref=task,
        note_id="freshness-note", category="next_action", text="Check the alternative.",
    )
    plan = store.transact("freshness-plan", {}, lambda tx: ref(tx.put(
        "working_note", planning_note_id("m", "peer", "task"), {
            "mission_id": "m", "actor_id": "peer", "task_id": "task",
            "task_ref": task, "category": "checkpoint",
            "text": '{"goal":"Compare both interpretations"}',
            "input_refs": [], "planning": {"schema": "ora.runtime_plan.v1"},
        },
    )), mission_id="m")
    def peer_request(tx):
        message = tx.put("mail_message", "freshness-peer-message", {
            "mission_id": "m", "sender_ref": {"kind": "agent_instance",
                                              "id": "peer", "version": 1},
            "recipient_ref": {"kind": "agent_instance", "id": "sender", "version": 1},
            "task_ref": task, "request_id": "freshness-peer-request",
            "payload": {"question": "Check the alternative"},
        })
        return ref(tx.put("mail_request", "freshness-peer-request", {
            "mission_id": "m", "requester_id": "peer", "recipient_id": "sender",
            "cause_ref": task, "message_ref": ref(message),
            "status": "unanswered", "answer_refs": [],
        }))

    request = store.transact("freshness-peer-request", {}, peer_request, mission_id="m")
    response, _ = save_result(
        store, call, "compact_conversation", {"status": "saved"},
        arguments={"summary": "Check the alternative and await peer review."},
        suffix="freshness",
    )
    store.transact("freshness-intent", {}, lambda tx: ref(tx.put(
        "model_request_intent", f"model-intent:{call['id']}:{call['version']}", {
            "mission_id": "m", "call_ref": call,
            "source_refs": [], "context_source_dependency_refs": [],
        },
    )), mission_id="m")
    saved = save_compaction(
        WorkingContext(store), control={"body": {
            "mission_id": "m", "actor_id": "peer", "task_id": "task",
            "response_ref": response, "tool_turn_refs": [],
        }}, response_ref=response, summary="Check the alternative and await peer review.",
        operation_id="save-freshness-summary",
    )
    summary = store.get(**saved["summary_ref"])
    assert {tuple(item[k] for k in ("kind", "id", "version"))
            for item in summary["body"]["input_refs"]} == {
        ("board_proposal", task["id"], task["version"]),
        ("working_note", note["id"], note["version"]),
        ("working_note", plan["id"], plan["version"]),
        ("mail_request", request["id"], request["version"]),
    }

    def revise(tx):
        for pointer, changes in ((task, {"next_action": "Check a newer question."}),
                                 (note, {"text": "A revised private note."}),
                                 (plan, {"text": '{"goal":"Revise the plan"}'}),
                                 (request, {"status": "answered"})):
            prior = tx.get(**pointer)
            tx.put(prior["kind"], prior["id"], {**prior["body"], **changes},
                   expected_version=prior["version"])
        return {}

    store.transact("revise-after-compaction", {}, revise, mission_id="m")
    reopened = StateStore(tmp_path, read_only=True)
    pack = WorkingContext(reopened).assemble(call, task, summary_ref=saved["summary_ref"])
    index = json.loads(pack.messages[-1]["content"])
    assert index["summary"]["status"] == "stale"
    assert set(tuple(item[k] for k in ("kind", "id", "version"))
               for item in index["summary"]["stale_input_refs"]) == {
        ("board_proposal", task["id"], task["version"]),
        ("working_note", note["id"], note["version"]),
        ("working_note", plan["id"], plan["version"]),
        ("mail_request", request["id"], request["version"]),
    }


def test_legacy_summary_without_declared_versions_remains_readable(tmp_path):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    summary = store.transact("legacy-summary", {}, lambda tx: ref(tx.put(
        "context_summary", "legacy-summary", {
            "mission_id": "m", "actor_id": "peer", "task_id": "task",
            "authority": "model_authored_continuation_aid",
            "text": "Historical working aid, not verified evidence.",
            "source_dependency_refs": [], "source_refs": [], "input_refs": [],
            "archived_note_refs": [], "archive_ref": {"kind": "conversation_archive",
                                                   "id": "legacy-archive", "version": 1},
        },
    )), mission_id="m")
    index = json.loads(WorkingContext(store).assemble(
        call, task, summary_ref=summary,
    ).messages[-1]["content"])
    assert index["summary"]["status"] == "current"
    assert index["summary"]["text"] == "Historical working aid, not verified evidence."
