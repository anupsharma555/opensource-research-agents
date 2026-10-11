"""Synthetic, offline context reconstruction and inclusion boundary fixtures."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace

import pytest

from ora.agents.capabilities import content_hash
from ora.coordination.board import ProposalBoard, ref
from ora.coordination.mailbox import PeerMailbox
from ora.evidence import read_source_window
from ora.mission.context import PACKER_VERSION, ContextError, ContextOverflow, WorkingContext
from ora.state import StateStore
from ora.state.store import canonical
from tests.evidence.test_coverage import checked_copy
from tests.models.test_session import select as select_saved_copy
from tests.models.test_session import setup as model_session_fixture


def fixture(store: StateStore) -> tuple[dict, dict]:
    def save(tx):
        peer = tx.put("agent_instance", "peer", {"mission_id": "m"})
        sender = tx.put("agent_instance", "sender", {"mission_id": "m"})
        task = tx.put(
            "board_proposal",
            "task",
            {
                "mission_id": "m",
                "status": "open",
                "objective": "Compare synthetic reports",
                "next_action": "Read the current evidence",
                "actor_ref": ref(peer),
            },
        )
        source = tx.put("source_copy", "copy", {"source_version": "synthetic-v1"})
        body = {
            "mission_id": "m",
            "instance_id": "peer",
            "provider": "groq",
            "endpoint": "https://example.invalid",
            "model_id": "selected-model",
            "settings": {"max_completion_tokens": 100},
            "reasoning_settings": {"reasoning_effort": "high"},
            "instructions": "Synthetic instructions",
        }
        body["configuration_sha256"] = content_hash(body)
        config = tx.put("agent_configuration", "config", body)
        call = tx.put(
            "agent_call",
            "call",
            {
                "mission_id": "m",
                "instance_id": "peer",
                "task_id": "task",
                "configuration_id": "config",
                "configuration_sha256": body["configuration_sha256"],
                "provider": "groq",
                "endpoint": body["endpoint"],
                "model_id": "selected-model",
                "settings": body["settings"],
                "reasoning_settings": body["reasoning_settings"],
            },
        )
        return {
            "call": ref(call),
            "task": ref(task),
            "source": ref(source),
            "sender": ref(sender),
            "config": ref(config),
        }

    saved = store.transact("fixture", {"synthetic": True}, save, mission_id="m")
    return saved["call"], saved["task"]


def bind_no_source_pack(
    store: StateStore, context: WorkingContext, pack, operation_id: str
) -> dict:
    config = store.get(**pack.configuration_ref)
    request = {
        "messages": [
            {"role": "developer", "content": config["body"]["instructions"]},
            *pack.messages,
        ]
    }
    encoded = canonical(request).encode()
    blob = store.put_blob(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    intent_id = f"model-intent:{pack.call_ref['id']}:{pack.call_ref['version']}"

    def save(tx):
        intent = tx.put(
            "model_request_intent",
            intent_id,
            {
                "mission_id": "m",
                "call_ref": pack.call_ref,
                "configuration_ref": pack.configuration_ref,
                "source_refs": [],
                "context_source_dependency_refs": list(context.context_source_dependency_refs(pack)),
                "request_sha256": digest,
                "blob": blob,
            },
        )
        return ref(intent)

    intent_ref = store.transact(f"{operation_id}-intent", {}, save, mission_id="m")
    return context.save_inclusion(f"{operation_id}-inclusion", pack, intent_ref)


def compact_checked_window_fixture(library, saved: dict, window: dict) -> tuple[dict, dict]:
    """Save one real checked-window result and return its compact model projection."""
    store = library.store
    call_ref, task_ref = fixture(store)
    copy_ref = saved["copy_ref"]
    passages = window["passage_refs"]

    def save_control_and_call(tx):
        original_call = tx.get(**call_ref)
        call = tx.put("agent_call", original_call["id"], {
            **original_call["body"], "input_refs": [copy_ref],
        }, expected_version=call_ref["version"])
        run = tx.put("mission_run", "compact-window-run", {
            "mission_id": "m", "question": "Read selected source", "scope": "Saved text",
            "source_refs": [copy_ref],
        })
        control = tx.put("mission_task_control", "compact-window-control", {
            "mission_id": "m", "run_id": run["id"], "actor_id": "peer",
            "task_id": task_ref["id"], "source_refs": [copy_ref],
            "selected_passage_refs": {copy_ref["id"]: passages},
            "read_window_refs": [window["window_ref"]],
        })
        return {"call_ref": ref(call), "control_ref": ref(control)}

    saved_control = store.transact(
        "compact-window-control", {}, save_control_and_call, mission_id="m",
    )
    selected_call, control_ref = saved_control["call_ref"], saved_control["control_ref"]
    result = {
        **window,
        "passages": [{
            **item, "span_id": store.get(**item["passage_ref"])["body"]["span_id"],
        } for item in window["passages"]],
        "model_selection": {"mode": "replace", "passage_refs": passages},
        "returned_chars": sum(len(item["text_preview"]) for item in window["passages"]),
        "next_request": None,
    }
    blob = store.put_blob(canonical({
        "tool_calls": [{
            "id": "window-tool", "type": "function",
            "function": {"name": "read_source_window", "arguments": canonical({
                "source_id": copy_ref["id"], "source_version": copy_ref["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 1600,
            })},
        }],
    }).encode())
    response_ref = store.transact("compact-window-response", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "compact-window-response", {
            "mission_id": "m", "task_id": task_ref["id"], "call_ref": selected_call,
            "status": "tool_requested", "blob": blob,
        },
    )), mission_id="m")
    context = WorkingContext(store)
    context.save_tool_result(
        "compact-window-result", response_ref=response_ref,
        tool_call_id="window-tool", name="read_source_window", content=canonical(result),
    )
    exchange, _ = context._tool_exchange(response_ref)
    projection = context._compact_selected_window_preview(
        response_ref, exchange[1], call=store.get(**selected_call),
        control=store.get(**control_ref), selected_source_refs=(copy_ref,),
    )
    assert projection is not None
    return result, json.loads(projection[0]["content"])


def test_current_task_and_new_message_override_stale_summary(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)

    def revise(tx):
        summary = tx.put(
            "context_summary",
            "old",
            {
                "mission_id": "m",
                "text": "Old interpretation",
                "input_refs": [task],
            },
        )
        current = tx.put(
            "board_proposal",
            "task",
            {
                **tx.get(**task)["body"],
                "next_action": "Review late contradiction",
            },
            expected_version=1,
        )
        return {"summary": ref(summary), "task": ref(current)}

    updated = store.transact("revise", {"reason": "late evidence"}, revise, mission_id="m")
    mailbox = PeerMailbox(store)
    sent = mailbox.send_message(
        "late-message",
        mission_id="m",
        message_id="message",
        sender_id="sender",
        recipient_id="peer",
        correlation_id="late",
        payload={"contradiction": "Synthetic late finding"},
        task_ref=updated["task"],
    )
    assert sent["status"] == "accepted"
    pack = WorkingContext(store).assemble(call, task, summary_ref=updated["summary"])
    view = json.loads(pack.messages[-1]["content"])
    assert view["task_ref"] == updated["task"]
    assert view["task"]["next_action"] == "Review late contradiction"
    assert view["summary"]["status"] == "stale"
    assert task in view["summary"]["stale_input_refs"]
    assert any(
        item["type"] == "message"
        and item["body"]["payload"] == {"contradiction": "Synthetic late finding"}
        for item in view["details"]
    )
    assert {"kind": "mail_message", "id": "message", "version": 1} in pack.included_refs


def test_message_saved_during_prior_work_appears_on_next_assembly(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    context = WorkingContext(store)
    before = context.assemble(call, task)
    assert json.loads(before.messages[-1]["content"]).get("message_refs", []) == []
    PeerMailbox(store).send_message(
        "during-call",
        mission_id="m",
        message_id="during",
        sender_id="sender",
        recipient_id="peer",
        correlation_id="during",
        payload={"update": "new"},
        task_ref=task,
    )
    after = WorkingContext(StateStore(tmp_path)).assemble(call, task)
    assert {"kind": "mail_message", "id": "during", "version": 1} in (
        json.loads(after.messages[-1]["content"])["message_refs"]
    )
    assert before.messages != after.messages


def test_new_shared_critique_is_discoverable_without_explicit_selection(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    dissent = ProposalBoard(store).dissent(
        "dissent",
        mission_id="m",
        dissent_id="critique",
        proposal_ref=task,
        actor_id="sender",
        critique="Synthetic counterevidence needs review",
    )
    assert dissent["status"] == "accepted"
    pack = WorkingContext(store).assemble(call, task)
    view = json.loads(pack.messages[-1]["content"])
    assert dissent["dissent_ref"] in view["shared_update_refs"]
    assert any(
        item["type"] == "shared_update"
        and item["body"]["critique"] == "Synthetic counterevidence needs review"
        for item in view["details"]
    )


def test_routine_own_executor_outcomes_are_exactly_pageable_without_replaying_bodies(
    tmp_path,
) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)

    def seed(tx):
        own_actor = ref(tx.get("agent_instance", "peer"))
        other_actor = ref(tx.get("agent_instance", "sender"))
        scientific = tx.put(
            "report", "own-substantive", {"mission_id": "m", "body": "A synthetic finding"}
        )
        passage = tx.put(
            "source_passage", "current-evidence", {"mission_id": "m", "text": "Evidence"}
        )

        def executor_outcome(index, output_refs=()):
            suffix = f"synthetic-{index}"
            artifact = tx.put(
                "executor_artifact",
                f"executor-artifact:{suffix}",
                {
                    "mission_id": "m",
                    "work_id": suffix,
                    "actor_id": "peer",
                    "handler_id": "mission-tool",
                    "output_refs": list(output_refs),
                },
            )
            receipt = tx.put(
                "executor_receipt",
                f"executor-receipt:{suffix}",
                {"mission_id": "m", "work_id": suffix, "artifact_ref": ref(artifact)},
            )
            trace = tx.put(
                "coordination_trace",
                f"executor-trace:{suffix}",
                {
                    "mission_id": "m",
                    "instance_id": "peer",
                    "proof": "recorded_local_callback",
                    "output_refs": [ref(artifact)],
                },
            )
            return ref(
                tx.put(
                    "board_outcome",
                    f"executor-outcome:{suffix}",
                    {
                        "mission_id": "m",
                        "actor_ref": own_actor,
                        "proposal_ref": task,
                        "result_refs": [ref(artifact), ref(receipt), ref(trace)],
                        "completes_task": False,
                    },
                )
            )

        routine = [executor_outcome(index) for index in range(18)]
        own_scientific = executor_outcome("scientific", [ref(scientific)])
        peer_scientific = ref(
            tx.put(
                "board_outcome",
                "peer-scientific-outcome",
                {
                    "mission_id": "m",
                    "actor_ref": other_actor,
                    "proposal_ref": task,
                    "result_refs": [ref(scientific)],
                    "completes_task": False,
                },
            )
        )
        return {
            "routine": routine,
            "own_scientific": own_scientific,
            "peer_scientific": peer_scientific,
            "passage": ref(passage),
        }

    saved = store.transact("seed-executor-outcomes", {}, seed, mission_id="m")
    dissent = ProposalBoard(store).dissent(
        "dissent-after-routine",
        mission_id="m",
        dissent_id="peer-critique",
        proposal_ref=task,
        actor_id="sender",
        critique="Synthetic contradiction",
    )
    context = WorkingContext(store)
    pack = context.assemble(call, task, evidence_refs=[saved["passage"]])
    view = json.loads(pack.messages[-1]["content"])
    detail_refs = {canonical(item["ref"]) for item in view["details"]}
    for pointer in (
        saved["own_scientific"],
        saved["peer_scientific"],
        dissent["dissent_ref"],
        saved["passage"],
    ):
        assert canonical(pointer) in detail_refs
    assert all(canonical(pointer) not in detail_refs for pointer in saved["routine"])
    assert view["record_counts"]["shared_updates"] == 21
    assert view["record_counts"]["routine_own_outcomes_compacted"] == 18
    assert view["routine_own_outcomes"]["count"] == 18
    assert set(map(canonical, view["routine_own_outcomes"]["recent_refs"])) <= set(
        map(canonical, saved["routine"])
    )
    assert {canonical(item["ref"]) for item in pack.omitted} == set(
        map(canonical, saved["routine"])
    )
    assert all(item["reason"] == "routine_own_executor_outcome_compacted" for item in pack.omitted)

    # A tight turn must apply pageable history compaction before rejecting the
    # required index. The same task and exact fetch path remain available.
    tight_index = {**view, "details": []}
    tight_index["omitted"] = {
        "count": len(pack.omitted) + len(view["details"]),
        "fetch": view["older_records"],
    }
    tight_index["routine_own_outcomes"] = {
        key: value
        for key, value in view["routine_own_outcomes"].items()
        if key != "recent_refs"
    }
    tight_index.pop("summary", None)
    tight_index["record_counts"] = {
        key: count for key, count in view["record_counts"].items() if count
    }
    allowance = len(canonical([{"role": "user", "content": canonical(tight_index)}])) + 120
    old_required_index = {
        **tight_index,
        "omitted": None,
        "summary": view["summary"],
        "record_counts": view["record_counts"],
        "routine_own_outcomes": view["routine_own_outcomes"],
    }
    assert len(canonical([{"role": "user", "content": canonical(old_required_index)}])) > allowance
    tight = context.assemble(
        call,
        task,
        evidence_refs=[saved["passage"]],
        allowance=allowance,
        estimate_tokens=len,
    )
    tight_view = json.loads(tight.messages[-1]["content"])
    assert tight.estimated_tokens <= allowance
    assert tight_view["task"] == view["task"]
    assert tight_view["record_counts"]["routine_own_outcomes_compacted"] == 18
    assert "routine_own_outcomes" not in tight_view
    assert tight_view["older_records"] == view["older_records"]
    assert {canonical(item["ref"]) for item in tight.omitted} >= set(
        map(canonical, saved["routine"])
    )

    # This reconstructs the old inline representation from the same frozen
    # records. The exact original bodies were the large repeated input.
    old_view = {**view, "details": [*view["details"]]}
    old_view.pop("routine_own_outcomes")
    old_view["record_counts"] = dict(old_view["record_counts"])
    old_view["record_counts"].pop("routine_own_outcomes_compacted")
    old_view["omitted"] = None
    old_view["details"].extend(
        {"type": "shared_update", "ref": pointer, "body": store.get(**pointer)["body"]}
        for pointer in saved["routine"]
    )
    old_messages = [*pack.messages[:-1], {"role": "user", "content": canonical(old_view)}]
    assert len(canonical(old_messages).encode()) - len(canonical(pack.messages).encode()) > 10_000

    inclusion_ref = bind_no_source_pack(store, context, pack, "routine-outcome")
    reopened = WorkingContext(StateStore(tmp_path))
    seen, cursor = [], 0
    while True:
        page = reopened.read_omitted_page(
            mission_id="m",
            actor_id="peer",
            task_id="task",
            inclusion_ref=inclusion_ref,
            cursor=cursor,
            limit=25,
        )
        assert page["total"] == 18 and page["returned_count"] > 0
        seen.extend(page["items"])
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]
    assert {canonical(item["ref"]) for item in seen} == set(map(canonical, saved["routine"]))
    window = context.read_omitted_record(
        mission_id="m",
        actor_id="peer",
        task_id="task",
        inclusion_ref=inclusion_ref,
        pointer=saved["routine"][0],
        offset=0,
        limit=2000,
    )
    assert (
        json.loads(window["text"])["result_refs"]
        == store.get(**saved["routine"][0])["body"]["result_refs"]
    )
    with pytest.raises(ContextError, match="another peer"):
        reopened.read_omitted_record(
            mission_id="m",
            actor_id="sender",
            task_id="task",
            inclusion_ref=inclusion_ref,
            pointer=saved["routine"][0],
            offset=0,
            limit=2000,
        )


def test_requester_sees_own_request_and_current_answer_disposition(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    mailbox = PeerMailbox(store)
    sent = mailbox.send_request(
        "ask",
        mission_id="m",
        request_id="request",
        message_id="outgoing",
        requester_id="peer",
        recipient_id="sender",
        question="Check synthetic result",
        expected_artifact="A reviewed source note",
        cause_ref=task,
        payload={"nested": {"criterion": "outcome"}},
        task_ref=task,
    )
    assert sent["status"] == "accepted"
    context = WorkingContext(store)
    open_view = json.loads(context.assemble(call, task).messages[-1]["content"])
    assert open_view.get("message_refs", []) == []
    assert open_view["outgoing_request_message_refs"] == [
        {"kind": "mail_message", "id": "outgoing", "version": 1}
    ]
    assert open_view["request_dispositions"][0]["status"] == "unanswered"
    assert any(
        item["type"] == "request" and item["body"]["question"] == "Check synthetic result"
        for item in open_view["details"]
    )

    artifact = store.transact(
        "artifact",
        {},
        lambda tx: ref(tx.put("source_passage", "answer-passage", {"text": "Synthetic answer"})),
        mission_id="m",
    )
    answered = mailbox.record_answer(
        "answer",
        mission_id="m",
        request_id="request",
        answer_id="answer",
        responder_id="sender",
        artifact_ref=artifact,
    )
    assert answered["status"] == "accepted"
    reviewed = mailbox.review_answer(
        "review",
        mission_id="m",
        request_id="request",
        reviewer_id="peer",
        answer_id="answer",
        disposition="answered",
        reason="Source note resolves the check",
    )
    assert reviewed["status"] == "accepted"
    resumed = json.loads(
        WorkingContext(StateStore(tmp_path)).assemble(call, task).messages[-1]["content"]
    )
    assert resumed["request_dispositions"] == [
        {
            "ref": reviewed["request_ref"],
            "status": "answered",
            "answer_refs": [answered["answer_ref"]],
            "accepted_answer_ref": answered["answer_ref"],
            "replacement_request_ref": None,
        }
    ]
    assert resumed["request_answer_refs"] == [answered["answer_ref"]]
    assert not any(
        item["type"] == "request" and item["body"]["status"] == "unanswered"
        for item in resumed["details"]
    )


def test_recipient_sees_request_about_senders_other_task(tmp_path) -> None:
    store = StateStore(tmp_path)
    recipient_call, recipient_task = fixture(store)
    sender_task = store.transact(
        "sender-task",
        {},
        lambda tx: ref(
            tx.put(
                "board_proposal",
                "sender-task",
                {"mission_id": "m", "status": "open", "objective": "Check a separate branch"},
            )
        ),
        mission_id="m",
    )
    sent = PeerMailbox(store).send_request(
        "cross-task-request",
        mission_id="m",
        request_id="cross-request",
        message_id="cross-message",
        requester_id="sender",
        recipient_id="peer",
        question="Can you check this finding?",
        expected_artifact="A peer answer",
        cause_ref=sender_task,
        payload={"claim": "Synthetic result"},
        task_ref=sender_task,
    )
    assert sent["status"] == "accepted"
    view = json.loads(
        WorkingContext(store).assemble(recipient_call, recipient_task).messages[-1]["content"]
    )
    assert sent["message_ref"] in view["message_refs"]
    assert sent["request_ref"] in [item["ref"] for item in view["request_dispositions"]]
    assert any(
        item["type"] == "request" and item["body"]["question"] == "Can you check this finding?"
        for item in view["details"]
    )
    assert any(
        item["type"] == "message" and item["body"]["task_ref"] == sender_task
        for item in view["details"]
    )


def test_current_source_use_is_visible_alongside_pinned_copy(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    source = {"kind": "source_copy", "id": "copy", "version": 1}

    def restrict(tx):
        old = tx.get(**source)
        updated = tx.put(
            "source_copy", "copy", {**old["body"], "model_use": "restricted"}, expected_version=1
        )
        return ref(updated)

    current = store.transact("restrict", {}, restrict, mission_id="m")
    pack = WorkingContext(store).assemble(call, task, source_refs=[source])
    view = json.loads(pack.messages[-1]["content"])
    assert view["source_refs"] == [source]
    assert view["source_retrieval"][0]["current_ref"] == current
    assert view["source_retrieval"][0]["status"] == "restricted"


def test_tight_source_index_compacts_only_equal_current_status(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    source = {"kind": "source_copy", "id": "copy", "version": 1}

    def permit(tx):
        use = tx.put(
            "source_use", "use",
            {
                "copy_ref": source,
                "allowed_uses": ["model_input"],
                "policy_status": "granted",
            },
        )
        tx.bind("source_use.copy", "copy:1", "source_use", "use")
        return ref(use)

    use = store.transact("permit-source", {}, permit, mission_id="m")
    context = WorkingContext(store)
    full = context.assemble(call, task, source_refs=[source])
    required = len(canonical(full.messages))
    compact = context.assemble(
        call, task, source_refs=[source], allowance=required - 1,
        estimate_tokens=len,
    )
    view = json.loads(compact.messages[-1]["content"])
    assert compact.estimated_tokens <= required - 1
    assert "source_refs" not in view  # Exact pointer remains in source_retrieval.
    selected = view["source_retrieval"][0]
    assert selected["ref"] == source
    assert selected["current_source_use_ref"] == use
    assert selected["status"] == "permitted"
    assert selected.get("selected_and_current_equal", True) is True

    def revise_copy(tx):
        old = tx.get(**source)
        return ref(tx.put(
            "source_copy", "copy", {**old["body"], "model_use": "restricted"},
            expected_version=1,
        ))

    current = store.transact("new-restricted-copy", {}, revise_copy, mission_id="m")
    divergent = context.assemble(call, task, source_refs=[source])
    status = json.loads(divergent.messages[-1]["content"])["source_retrieval"][0]
    assert status["current_ref"] == current
    assert status["selected_model_input_status"] == "permitted"
    assert status["current_model_input_status"] != "permitted"
    tight_divergent = context.assemble(
        call, task, source_refs=[source],
        allowance=len(canonical(divergent.messages)) - 1,
        estimate_tokens=len,
    )
    tight_status = json.loads(tight_divergent.messages[-1]["content"])["source_retrieval"][0]
    assert tight_status == status  # Tight packing must preserve the changed source-use state.


def test_separate_source_use_revision_revokes_old_copy_for_context(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    source = {"kind": "source_copy", "id": "copy", "version": 1}

    def permitted(tx):
        use = tx.put(
            "source_use",
            "use",
            {
                "copy_ref": source,
                "allowed_uses": ["discovery", "model_input"],
                "policy_status": "granted",
            },
        )
        tx.bind("source_use.copy", "copy:1", "source_use", "use")
        return ref(use)

    first = store.transact("policy", {}, permitted, mission_id="m")
    before = json.loads(
        WorkingContext(store).assemble(call, task, source_refs=[source]).messages[-1]["content"]
    )
    assert before["source_retrieval"][0]["status"] == "permitted"
    assert before["source_retrieval"][0]["current_source_use_ref"] == first

    def revoke(tx):
        old = tx.get(**first)
        return ref(
            tx.put(
                "source_use",
                "use",
                {
                    **old["body"],
                    "allowed_uses": ["discovery"],
                    "policy_status": "requires_source_policy_resolution",
                },
                expected_version=1,
            )
        )

    current = store.transact("revoke", {}, revoke, mission_id="m")
    after = json.loads(
        WorkingContext(StateStore(tmp_path))
        .assemble(call, task, source_refs=[source])
        .messages[-1]["content"]
    )
    assert after["source_retrieval"][0]["current_source_use_ref"] == current
    assert after["source_retrieval"][0]["status"] == "restricted"
    with pytest.raises(ContextError, match="source-use policy restricts"):
        WorkingContext(store).require_current_source_use([source])


def test_new_copy_policy_does_not_revoke_frozen_permitted_version(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    old_ref = {"kind": "source_copy", "id": "copy", "version": 1}

    def save(tx):
        old_use = tx.put(
            "source_use",
            "old-use",
            {"copy_ref": old_ref, "allowed_uses": ["model_input"], "policy_status": "granted"},
        )
        tx.bind("source_use.copy", "copy:1", "source_use", old_use["id"])
        latest = tx.put(
            "source_copy", "copy", {"source_version": "synthetic-v2"}, expected_version=1
        )
        new_use = tx.put(
            "source_use",
            "new-use",
            {
                "copy_ref": ref(latest),
                "allowed_uses": ["discovery"],
                "policy_status": "requires_source_policy_resolution",
            },
        )
        tx.bind("source_use.copy", "copy:2", "source_use", new_use["id"])
        return {"copy": ref(latest), "use": ref(new_use)}

    latest = store.transact("new-version", {}, save, mission_id="m")
    view = json.loads(
        WorkingContext(store).assemble(call, task, source_refs=[old_ref]).messages[-1]["content"]
    )
    status = view["source_retrieval"][0]
    assert status["current_ref"] == latest["copy"]
    assert status["current_source_use_ref"] == latest["use"]
    assert status["selected_model_input_status"] == "permitted"
    assert status["current_model_input_status"] == "restricted"
    assert status["status"] == "permitted"


def test_resume_uses_call_manifest_when_defaults_change(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)

    def changed_default(tx):
        return ref(
            tx.put(
                "agent_configuration",
                "new-default",
                {
                    "mission_id": "m",
                    "instance_id": "peer",
                    "provider": "other-provider",
                    "model_id": "different-model",
                    "settings": {"max_completion_tokens": 10},
                },
            )
        )

    store.transact("new-default", {}, changed_default, mission_id="m")
    pack = WorkingContext(StateStore(tmp_path)).assemble(call, task)
    view = json.loads(pack.messages[-1]["content"])
    assert pack.configuration_ref == {
        "kind": "agent_configuration",
        "id": "config",
        "version": 1,
    }
    assert view["selected_model_id"] == "selected-model"
    assert store.get(**pack.configuration_ref)["body"]["settings"] == {"max_completion_tokens": 100}


def test_context_allowance_omits_detail_with_exact_fetch_path(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    PeerMailbox(store).send_message(
        "large",
        mission_id="m",
        message_id="large",
        sender_id="sender",
        recipient_id="peer",
        correlation_id="large",
        payload={"nested": ["x" * 4000]},
        task_ref=task,
    )
    context = WorkingContext(store)
    full = context.assemble(call, task)
    view = json.loads(full.messages[-1]["content"])
    core = {**view, "details": [], "omitted": {"count": 1, "fetch": view["older_records"]}}
    allowance = len(canonical([{"role": "user", "content": canonical(core)}])) + 350
    compact = context.assemble(call, task, allowance=allowance, estimate_tokens=len)
    packed = json.loads(compact.messages[-1]["content"])
    assert compact.estimated_tokens <= allowance
    assert packed["details"] == []
    assert packed["omitted"] == {"count": 1}
    assert compact.omitted[0]["ref"] == {"kind": "mail_message", "id": "large", "version": 1}
    assert compact.omitted[0]["fetch"] == compact.omitted[0]["ref"]
    assert all(item["id"] != "large" for item in compact.included_refs)
    with pytest.raises(ContextOverflow, match="Required task"):
        context.assemble(call, task, allowance=1, estimate_tokens=len)


def test_packing_puts_new_obligations_and_critique_before_old_outcomes(tmp_path) -> None:
    tick = [1_000.0]
    store = StateStore(tmp_path, clock=lambda: tick[0])
    call, task = fixture(store)
    for number in (1, 2):
        tick[0] += 1
        store.transact(
            f"old-outcome-{number}",
            {},
            lambda tx, number=number: ref(
                tx.put(
                    "board_outcome",
                    f"outcome-{number}",
                    {"mission_id": "m", "proposal_ref": task, "historical_detail": "old" * 4000},
                )
            ),
            mission_id="m",
        )
    tick[0] += 1
    sent = PeerMailbox(store).send_request(
        "new-request",
        mission_id="m",
        request_id="new-request",
        message_id="new-message",
        requester_id="sender",
        recipient_id="peer",
        question="Check the discrepancy",
        expected_artifact="A source-backed answer",
        cause_ref=task,
        payload={"question": "Check the discrepancy"},
        task_ref=task,
    )
    assert sent["status"] == "accepted"
    tick[0] += 1
    artifact = store.transact(
        "answer-artifact",
        {},
        lambda tx: ref(
            tx.put("source_passage", "new-answer-artifact", {"text": "Synthetic counterexample"})
        ),
        mission_id="m",
    )
    answered = PeerMailbox(store).record_answer(
        "new-answer",
        mission_id="m",
        request_id="new-request",
        answer_id="new-answer",
        responder_id="peer",
        artifact_ref=artifact,
    )
    assert answered["status"] == "accepted"
    tick[0] += 1
    dissent = ProposalBoard(store).dissent(
        "late-dissent",
        mission_id="m",
        dissent_id="late-dissent",
        proposal_ref=task,
        actor_id="sender",
        critique="Late synthetic contradiction",
    )
    assert dissent["status"] == "accepted"

    context = WorkingContext(store)
    full = json.loads(context.assemble(call, task).messages[-1]["content"])
    ordered = [item["ref"] for item in full["details"]]
    outcome_refs = [{"kind": "board_outcome", "id": f"outcome-{n}", "version": 1} for n in (1, 2)]
    for pointer in (dissent["dissent_ref"], answered["answer_ref"], answered["request_ref"]):
        assert ordered.index(pointer) < min(ordered.index(item) for item in outcome_refs)
    assert ordered[0] == dissent["dissent_ref"]

    core = {
        **full,
        "details": [],
        "omitted": {"count": len(full["details"]), "fetch": full["older_records"]},
    }
    allowance = len(canonical([{"role": "user", "content": canonical(core)}])) + 2500
    compact_pack = context.assemble(call, task, allowance=allowance, estimate_tokens=len)
    compact = json.loads(compact_pack.messages[-1]["content"])
    assert dissent["dissent_ref"] in [item["ref"] for item in compact["details"]]
    omitted = [item for item in compact_pack.omitted if item["ref"] in outcome_refs]
    assert len(omitted) == 2
    assert compact["omitted"]["count"] == len(compact_pack.omitted)
    assert all(item["fetch"] == item["ref"] for item in compact_pack.omitted)


def test_tiny_detail_fits_exact_allowance(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    tiny = store.transact(
        "tiny-evidence", {}, lambda tx: ref(tx.put("source_passage", "tiny", {})), mission_id="m"
    )
    context = WorkingContext(store)
    view = json.loads(context.assemble(call, task, evidence_refs=[tiny]).messages[-1]["content"])
    with_detail = len(canonical([{"role": "user", "content": canonical(view)}]))
    packed = context.assemble(
        call, task, evidence_refs=[tiny], allowance=with_detail, estimate_tokens=len
    )
    assert packed.estimated_tokens <= with_detail
    assert tiny in packed.included_refs
    assert packed.omitted == ()


def test_compact_page_restores_exact_omissions_without_source_text(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)

    def seed(tx):
        report = tx.put(
            "report",
            "linked-report",
            {
                "mission_id": "m",
                "title": "Synthetic output",
                "body": "A reviewed synthetic conclusion",
                "citations": [],
                "limitations": [],
            },
        )
        outcome = tx.put(
            "board_outcome",
            "old-outcome",
            {
                "mission_id": "m",
                "proposal_ref": task,
                "result_refs": [ref(report)],
                "completes_task": False,
                "historical_detail": "old" * 4000,
            },
        )
        passage = tx.put(
            "source_passage", "omitted-passage", {"text": "SYNTHETIC_RESTRICTED_SOURCE_TEXT" * 500}
        )
        return {"outcome": ref(outcome), "passage": ref(passage), "report": ref(report)}

    saved = store.transact("omitted-records", {}, seed, mission_id="m")
    context = WorkingContext(store)
    full = json.loads(
        context.assemble(call, task, evidence_refs=[saved["passage"]]).messages[-1]["content"]
    )
    core = {**full, "details": [], "omitted": {"count": 2, "fetch": full["older_records"]}}
    allowance = len(canonical([{"role": "user", "content": canonical(core)}])) + 100
    pack = context.assemble(
        call, task, evidence_refs=[saved["passage"]], allowance=allowance, estimate_tokens=len
    )
    view = json.loads(pack.messages[-1]["content"])
    assert view["omitted"]["count"] == 2
    assert len(pack.omitted) == 2
    assert [item["fetch"] for item in pack.omitted] == [saved["outcome"], saved["passage"]]
    config = store.get(**pack.configuration_ref)
    request = {
        "messages": [
            {"role": "developer", "content": config["body"]["instructions"]},
            *pack.messages,
        ]
    }
    blob = store.put_blob(canonical(request).encode())
    digest = hashlib.sha256(canonical(request).encode()).hexdigest()

    def intent(tx):
        saved_intent = tx.put(
            "model_request_intent",
            "model-intent:call:1",
            {
                "mission_id": "m",
                "call_ref": call,
                "configuration_ref": pack.configuration_ref,
                "source_refs": [],
                "request_sha256": digest,
                "blob": blob,
            },
        )
        return ref(saved_intent)

    intent_ref = store.transact("paged-intent", {}, intent, mission_id="m")
    inclusion_ref = context.save_inclusion("paged-inclusion", pack, intent_ref)
    reopened = WorkingContext(StateStore(tmp_path))
    first = reopened.read_omitted_page(
        mission_id="m",
        actor_id="peer",
        task_id="task",
        inclusion_ref=inclusion_ref,
        cursor=0,
        limit=1,
    )
    assert first["total"] == 2 and first["next_cursor"] == 1
    assert first["items"][0]["ref"] == saved["outcome"]
    second = reopened.read_omitted_page(
        mission_id="m",
        actor_id="peer",
        task_id="task",
        inclusion_ref=inclusion_ref,
        cursor=1,
        limit=1,
    )
    assert second["items"][0]["ref"] == saved["passage"]
    assert (
        second["items"][0]["summary"]["status"]
        == "exact_reference_only_use_authorized_source_reader"
    )
    assert "SYNTHETIC_RESTRICTED_SOURCE_TEXT" not in canonical(first) + canonical(second)
    chunks, offset = [], 0
    while True:
        window = reopened.read_omitted_record(
            mission_id="m",
            actor_id="peer",
            task_id="task",
            inclusion_ref=inclusion_ref,
            pointer=saved["outcome"],
            offset=offset,
            limit=10,
        )
        chunks.append(window["text"])
        if window["next_offset"] is None:
            break
        offset = window["next_offset"]
    projection = json.loads("".join(chunks))
    assert projection["proposal_ref"] == task
    assert "historical_detail" not in projection
    report_window = reopened.read_omitted_record(
        mission_id="m",
        actor_id="peer",
        task_id="task",
        inclusion_ref=inclusion_ref,
        pointer=saved["report"],
        offset=0,
        limit=2000,
    )
    assert json.loads(report_window["text"])["body"] == "A reviewed synthetic conclusion"
    with pytest.raises(ContextError, match="Raw source"):
        reopened.read_omitted_record(
            mission_id="m",
            actor_id="peer",
            task_id="task",
            inclusion_ref=inclusion_ref,
            pointer=saved["passage"],
            offset=0,
            limit=100,
        )
    with pytest.raises(ContextError, match="another peer"):
        reopened.read_omitted_page(
            mission_id="m",
            actor_id="sender",
            task_id="task",
            inclusion_ref=inclusion_ref,
            cursor=0,
            limit=1,
        )


def test_linked_local_report_read_rechecks_source_use(tmp_path) -> None:
    store = StateStore(tmp_path)
    call_ref, task_ref = fixture(store)
    copy_ref = ref(store.get("source_copy", "copy"))
    config_ref = ref(store.get("agent_configuration", "config"))

    def seed(tx):
        run = tx.put("mission_run", "local-report-run", {
            "mission_id": "m", "source_refs": [copy_ref],
        })
        use = tx.put("source_use", "local-report-use", {
            "copy_ref": copy_ref, "allowed_uses": ["model_input"],
            "policy_status": "granted",
        })
        tx.bind("source_use.copy", "copy:1", "source_use", use["id"])
        report = tx.put("report", "local-source-report", {
            "mission_id": "m", "run_id": run["id"],
            "title": "Synthetic report", "body": "A source-backed synthetic finding",
            "citations": [{"source_id": "copy"}],
            "citation_input_provenance": [{"copy_ref": copy_ref}],
            "limitations": [],
        })
        outcome = tx.put("board_outcome", "local-report-outcome", {
            "mission_id": "m", "proposal_ref": task_ref,
            "result_refs": [ref(report)], "completes_task": False,
        })
        inclusion = tx.put("context_inclusion", "local-report-inclusion", {
            "mission_id": "m", "call_ref": call_ref,
            "configuration_ref": config_ref, "task_ref": task_ref,
            "source_refs": [], "omitted": [{
                "type": "shared_update", "ref": ref(outcome),
                "fetch": ref(outcome),
            }],
        })
        return {"inclusion": ref(inclusion), "report": ref(report), "use": ref(use)}

    saved = store.transact(
        "local-report-source-use", {}, seed, mission_id="m"
    )
    inclusion_ref, report_ref, use_ref = (
        saved["inclusion"], saved["report"], saved["use"]
    )
    context = WorkingContext(store)
    window = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion_ref, pointer=report_ref, offset=0, limit=2000,
    )
    assert json.loads(window["text"])["body"] == "A source-backed synthetic finding"
    page = context.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion_ref, cursor=0, limit=1,
    )
    assert "Synthetic report" not in canonical(page)

    def revoke(tx):
        previous = tx.get(**use_ref)
        return tx.put("source_use", previous["id"], {
            **previous["body"], "policy_status": "requires_source_policy_resolution",
        }, expected_version=previous["version"])

    store.transact("local-report-revoke", {}, revoke, mission_id="m")
    denied = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion_ref, pointer=report_ref, offset=0, limit=2000,
    )
    diagnostic = json.loads(denied["text"])
    assert diagnostic["status"] == "data_unavailable_due_source_use"
    assert diagnostic["source_dependency_refs"] == [copy_ref]
    assert diagnostic["unavailable_source_refs"] == [copy_ref]
    assert "body" not in diagnostic
    revoked_page = context.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion_ref, cursor=0, limit=1,
    )
    assert "Synthetic report" not in canonical(revoked_page)


def test_page_reply_byte_budget_preserves_exact_cursor(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    expected = []
    for number in range(25):
        saved = store.transact(
            f"critique-{number}",
            {},
            lambda tx, number=number: ref(
                tx.put(
                    "board_dissent",
                    f"critique-{number}",
                    {
                        "mission_id": "m",
                        "proposal_ref": task,
                        "critique": f"Synthetic objection {number}: " + "x" * 900,
                    },
                )
            ),
            mission_id="m",
        )
        expected.append(saved)
    context = WorkingContext(store)
    full = json.loads(context.assemble(call, task).messages[-1]["content"])
    core = {**full, "details": [], "omitted": {"count": 25, "fetch": full["older_records"]}}
    allowance = len(canonical([{"role": "user", "content": canonical(core)}])) + 80
    pack = context.assemble(call, task, allowance=allowance, estimate_tokens=len)
    assert len(pack.omitted) == 25
    inclusion_ref = bind_no_source_pack(store, context, pack, "bounded-page")
    cursor, seen, lengths = 0, [], []
    requested_limits = (50, 100)
    page_number = 0
    while True:
        requested = requested_limits[page_number % len(requested_limits)]
        page = WorkingContext(StateStore(tmp_path)).read_omitted_page(
            mission_id="m",
            actor_id="peer",
            task_id="task",
            inclusion_ref=inclusion_ref,
            cursor=cursor,
            limit=requested,
        )
        assert page["requested_limit"] == requested
        assert page["effective_limit"] == 20
        assert page["returned_count"] == len(page["items"])
        assert 1 <= page["returned_count"] <= 20
        lengths.append(len(canonical(page)))
        seen.extend(item["ref"] for item in page["items"])
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
        page_number += 1
    assert set(map(canonical, seen)) == set(map(canonical, expected))
    assert len(lengths) > 1
    assert max(lengths) <= 1800
    with pytest.raises(ContextError, match="another peer"):
        context.read_omitted_page(
            mission_id="m",
            actor_id="sender",
            task_id="task",
            inclusion_ref=inclusion_ref,
            cursor=0,
            limit=100,
        )


def test_oversized_record_window_returns_bounded_text_and_continuation(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    dissent = store.transact(
        "long-dissent",
        {},
        lambda tx: ref(
            tx.put(
                "board_dissent",
                "long-dissent",
                {
                    "mission_id": "m",
                    "proposal_ref": task,
                    "critique": "Synthetic objection " + "x" * 6000,
                },
            )
        ),
        mission_id="m",
    )
    context = WorkingContext(store)
    full = json.loads(context.assemble(call, task).messages[-1]["content"])
    core = {**full, "details": [], "omitted": {"count": 1, "fetch": full["older_records"]}}
    allowance = len(canonical([{"role": "user", "content": canonical(core)}])) + 50
    pack = context.assemble(call, task, allowance=allowance, estimate_tokens=len)
    assert [item["fetch"] for item in pack.omitted] == [dissent]
    inclusion_ref = bind_no_source_pack(store, context, pack, "long-window")
    first = context.read_omitted_record(
        mission_id="m",
        actor_id="peer",
        task_id="task",
        inclusion_ref=inclusion_ref,
        pointer=dissent,
        offset=0,
        limit=50_000,
    )
    assert first["requested_limit"] == 50_000
    assert first["effective_limit"] == 2000
    assert first["returned_chars"] == len(first["text"]) == 2000
    assert first["next_offset"] == 2000
    chunks, offset = [first["text"]], first["next_offset"]
    while offset is not None:
        window = context.read_omitted_record(
            mission_id="m",
            actor_id="peer",
            task_id="task",
            inclusion_ref=inclusion_ref,
            pointer=dissent,
            offset=offset,
            limit=50_000,
        )
        chunks.append(window["text"])
        offset = window["next_offset"]
    assert json.loads("".join(chunks))["critique"].startswith("Synthetic objection ")
    with pytest.raises(ContextError, match="another peer"):
        context.read_omitted_record(
            mission_id="m",
            actor_id="sender",
            task_id="task",
            inclusion_ref=inclusion_ref,
            pointer=dissent,
            offset=0,
            limit=100,
        )
    with pytest.raises(ContextError, match="positive integer"):
        context.read_omitted_page(
            mission_id="m",
            actor_id="peer",
            task_id="task",
            inclusion_ref=inclusion_ref,
            cursor=0,
            limit=0,
        )
    with pytest.raises(ContextError, match="positive integer"):
        context.read_omitted_page(
            mission_id="m",
            actor_id="peer",
            task_id="task",
            inclusion_ref=inclusion_ref,
            cursor=0,
            limit="50",
        )
    with pytest.raises(ContextError, match="positive integer"):
        context.read_omitted_record(
            mission_id="m",
            actor_id="peer",
            task_id="task",
            inclusion_ref=inclusion_ref,
            pointer=dissent,
            offset=0,
            limit=-1,
        )
    with pytest.raises(ContextError, match="positive integer"):
        context.read_omitted_record(
            mission_id="m",
            actor_id="peer",
            task_id="task",
            inclusion_ref=inclusion_ref,
            pointer=dissent,
            offset=0,
            limit="100",
        )


def test_saved_large_page_result_compacts_only_model_input(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    refs = []
    for number in range(10):
        refs.append(
            store.transact(
                f"older-dissent-{number}",
                {},
                lambda tx, number=number: ref(
                    tx.put(
                        "board_dissent",
                        f"older-{number}",
                        {
                            "mission_id": "m",
                            "proposal_ref": task,
                            "critique": f"Synthetic concern {number}" + "x" * 700,
                        },
                    )
                ),
                mission_id="m",
            )
        )
    prior_inclusion_ref = {"kind": "context_inclusion", "id": "context:prior", "version": 1}
    page = {
        "schema": "ora.context_omission_page.v1",
        "inclusion_ref": prior_inclusion_ref,
        "cursor": 0,
        "next_cursor": 10,
        "total": 17,
        "items": [
            {
                "type": "shared_update",
                "ref": pointer,
                "summary": {
                    "kind": "board_dissent",
                    "critique": {
                        "text": "Synthetic preview " + "x" * 280,
                        "total_chars": 700,
                        "truncated": True,
                    },
                },
            }
            for pointer in refs
        ],
    }
    private = store.put_blob(
        canonical(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "tool-page",
                        "type": "function",
                        "function": {"name": "read_context_page", "arguments": "{}"},
                    }
                ],
            }
        ).encode()
    )
    reasoning = store.put_blob(b"PROVIDER_RETURNED_CURRENT_REASONING")

    def save_response(tx):
        reason = tx.put(
            "restricted_reasoning",
            "page-reason",
            {
                "mission_id": "m",
                "origin": "provider_returned",
                "redacted": False,
                "blob": reasoning,
            },
        )
        response = tx.put(
            "model_response_receipt",
            "page-response",
            {
                "mission_id": "m",
                "task_id": "task",
                "call_ref": call,
                "status": "tool_requested",
                "blob": private,
                "reasoning_status": "returned",
                "reasoning_ref": ref(reason),
            },
        )
        return ref(response)

    response_ref = store.transact("page-response", {}, save_response, mission_id="m")
    context = WorkingContext(store)
    result_ref = context.save_tool_result(
        "saved-large-page",
        response_ref=response_ref,
        tool_call_id="tool-page",
        name="read_context_page",
        content=canonical(page),
    )
    original = json.loads(store.read_blob(store.get(**result_ref)["body"]["blob"]["sha256"]))
    assert len(json.loads(original["content"])["items"]) == 10
    pack = context.assemble(
        call,
        task,
        tool_turn_refs=[response_ref],
        allowance=2675,
        estimate_tokens=lambda value: (len(value) * 5 + 15) // 16,
    )
    current = json.loads(pack.messages[-2]["content"])
    assert current["status"] == "saved_result_compacted_for_model_input"
    assert current["original_item_count"] == 10
    assert len(current["included_items"]) < 10
    assert current["omitted_item_count"] == 10 - len(current["included_items"])
    assert current["next_cursor"] == len(current["included_items"])
    assert "reasoning" not in pack.messages[-3]
    assert store.read_blob(reasoning["sha256"]) == b"PROVIDER_RETURNED_CURRENT_REASONING"
    omitted_page = [item for item in pack.omitted if item["type"] == "tool_page_item"]
    assert len(omitted_page) == current["omitted_item_count"]
    inclusion_ref = bind_no_source_pack(store, context, pack, "compacted-page")
    receipt = store.get(**inclusion_ref)["body"]
    assert receipt["returned_reasoning_policy"] == "saved_restricted_not_replayed"
    assert len(receipt["tool_result_compactions"]) == 1
    assert receipt["tool_result_compactions"][0]["original_item_count"] == 10
    assert len(receipt["tool_result_compactions"][0]["omitted_item_refs"]) == len(omitted_page)
    retained = json.loads(store.read_blob(store.get(**result_ref)["body"]["blob"]["sha256"]))
    assert len(json.loads(retained["content"])["items"]) == 10


def test_tool_pair_keeps_saved_result_without_repeating_returned_reasoning(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    assistant = {
        "content": None,
        "tool_calls": [
            {"id": "tool-7", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        ],
    }
    private = store.put_blob(canonical(assistant).encode())
    private_reasoning = b"SYNTHETIC_PRIVATE_REASONING_" + b"x" * 8000
    reasoning = store.put_blob(private_reasoning)

    def save(tx):
        reason = tx.put(
            "restricted_reasoning",
            "reason",
            {
                "mission_id": "m",
                "origin": "provider_returned",
                "redacted": False,
                "blob": reasoning,
            },
        )
        response = tx.put(
            "model_response_receipt",
            "response",
            {
                "mission_id": "m",
                "task_id": "task",
                "call_ref": call,
                "status": "tool_requested",
                "blob": private,
                "reasoning_status": "returned",
                "reasoning_ref": ref(reason),
            },
        )
        return ref(response)

    response = store.transact("response", {}, save, mission_id="m")
    context = WorkingContext(store)
    with pytest.raises(ContextError, match="pending"):
        context.assemble(call, task, tool_turn_refs=[response])
    context.save_tool_result(
        "result",
        response_ref=response,
        tool_call_id="tool-7",
        name="lookup",
        content="Synthetic lookup result",
    )
    pack = context.assemble(call, task, tool_turn_refs=[response])
    assert [item["role"] for item in pack.messages] == ["assistant", "tool", "user"]
    assert "reasoning" not in pack.messages[0]
    assert pack.messages[0]["tool_calls"] == assistant["tool_calls"]
    assert pack.messages[1]["tool_call_id"] == "tool-7"
    assert pack.messages[1]["content"] == "Synthetic lookup result"
    assert len(canonical(pack.messages)) < len(private_reasoning)
    assert store.read_blob(reasoning["sha256"]) == private_reasoning
    archived = context.assemble(call, task, archived_tool_turn_refs=[response])
    assert [item["role"] for item in archived.messages] == ["user"]
    assert archived.omitted == (
        {
            "type": "completed_tool_turn",
            "ref": response,
            "reason": "completed_history_compacted",
            "fetch": response,
        },
    )
    assert (
        json.loads(archived.messages[-1]["content"])["record_counts"]["completed_tool_turns"] == 1
    )
    config = store.get(**archived.configuration_ref)
    request = {
        "messages": [
            {"role": "developer", "content": config["body"]["instructions"]},
            *archived.messages,
        ]
    }
    blob = store.put_blob(canonical(request).encode())
    digest = hashlib.sha256(canonical(request).encode()).hexdigest()
    intent = store.transact(
        "archived-intent",
        {},
        lambda tx: ref(
            tx.put(
                "model_request_intent",
                "model-intent:call:1",
                {
                    "mission_id": "m",
                    "call_ref": call,
                    "configuration_ref": archived.configuration_ref,
                    "source_refs": [],
                    "request_sha256": digest,
                    "blob": blob,
                },
            )
        ),
        mission_id="m",
    )
    inclusion = context.save_inclusion("archived-inclusion", archived, intent)
    page = context.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task", inclusion_ref=inclusion, cursor=0, limit=1
    )
    assert page["items"][0]["summary"]["tool_call_id"] == "tool-7"
    assert page["items"][0]["summary"]["tool_name"] == "lookup"
    assert "Provider returned this current-turn reasoning" not in canonical(page)
    with pytest.raises(ContextError, match="Tool history"):
        context.assemble(call, task, history=[pack.messages[0], pack.messages[1]])


def test_exact_inclusion_receipt_binds_real_request_bytes(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    source = {"kind": "source_copy", "id": "copy", "version": 1}
    context = WorkingContext(store)
    pack = context.assemble(call, task, source_refs=[source])
    source_text = "Synthetic source copy"
    source_packet = {
        "source_packets": [
            {
                "id": "copy",
                "version": 1,
                "sha256": hashlib.sha256(source_text.encode()).hexdigest(),
                "text": source_text,
            }
        ]
    }
    request = {
        "model": "selected-model",
        "messages": [
            {"role": "developer", "content": "Synthetic instructions"},
            {"role": "user", "content": json.dumps(source_packet)},
            *pack.messages,
        ],
    }
    digest = hashlib.sha256(canonical(request).encode()).hexdigest()
    blob = store.put_blob(canonical(request).encode())

    def save(tx):
        intent = tx.put(
            "model_request_intent",
            "model-intent:call:1",
            {
                "mission_id": "m",
                "call_ref": call,
                "configuration_ref": pack.configuration_ref,
                "source_refs": [source],
                "request_sha256": digest,
                "blob": blob,
            },
        )
        return ref(intent)

    intent = store.transact("intent", {}, save, mission_id="m")
    receipt_ref = context.save_inclusion("include", pack, intent)
    receipt = store.get(**receipt_ref)["body"]
    assert receipt["packer_version"] == PACKER_VERSION
    assert receipt["returned_reasoning_policy"] == "saved_restricted_not_replayed"
    assert receipt["request_sha256"] == digest
    assert receipt["source_refs"] == [source]
    assert receipt["status"] == "prepared_input_not_scientific_uptake"
    bad = replace(pack, messages=[{"role": "user", "content": "Different"}])
    with pytest.raises(ContextError, match="messages differ"):
        context.save_inclusion("bad-inclusion", bad, intent)


def test_inclusion_binds_actual_model_session_prepared_intent(tmp_path) -> None:
    store, _registry, bridge, call, config, copy_ref, _captured = model_session_fixture(tmp_path)
    task = store.transact(
        "mission-task",
        {},
        lambda tx: ref(
            tx.put(
                "board_proposal",
                "task",
                {
                    "mission_id": "mission",
                    "status": "open",
                    "objective": "Review synthetic abstract",
                    "next_action": "Inspect saved copy",
                },
            )
        ),
        mission_id="mission",
    )
    context = WorkingContext(store)
    pack = context.assemble(ref(call), task)
    prepared = bridge.prepare(
        call_id=call["id"],
        call_version=call["version"],
        phase="collect",
        messages=pack.messages,
        sources=select_saved_copy(copy_ref),
    )
    inclusion_ref = context.save_inclusion("actual-inclusion", pack, prepared.intent_ref)
    inclusion = store.get(**inclusion_ref)["body"]
    assert inclusion["configuration_ref"] == ref(config)
    assert inclusion["source_refs"] == [copy_ref]
    assert inclusion["request_sha256"] == prepared.request.request_sha256
    assert inclusion["status"] == "prepared_input_not_scientific_uptake"


def test_restarted_context_recaps_prior_search_without_replaying_tool_text(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    private = store.put_blob(canonical({
        "content": None,
        "tool_calls": [{
            "id": "scholarly-search-1",
            "type": "function",
            "function": {
                "name": "search_scholarly",
                "arguments": canonical({
                    "route": "pubmed.pmid", "query": "", "selected_id": "12345", "limit": 5,
                }),
            },
        }],
    }).encode())

    def save(tx):
        response = tx.put("model_response_receipt", "prior-search", {
            "mission_id": "m", "task_id": "task", "call_ref": call,
            "status": "tool_requested", "blob": private,
        })
        return ref(response)

    response_ref = store.transact("prior-search", {}, save, mission_id="m")
    context = WorkingContext(store)
    context.save_tool_result(
        "prior-search-result", response_ref=response_ref,
        tool_call_id="scholarly-search-1", name="search_scholarly",
        content=canonical({
            "route": "pubmed.pmid", "outcome": "complete",
            "hits": [{"hit_id": "hit-12345", "title": "A synthetic study"}],
            "private_source_text": "SYNTHETIC_SOURCE_BODY_MUST_NOT_BE_REPLAYED",
        }),
    )
    reopened = WorkingContext(StateStore(tmp_path))
    pack = reopened.assemble(call, task, archived_tool_turn_refs=[response_ref])
    view = json.loads(pack.messages[-1]["content"])
    recent = view["recent_scholarly_searches"][0]
    assert recent["search"] == {
        "route": "pubmed.pmid", "query_preview": "", "query_chars": 0,
        "query_sha256": hashlib.sha256(b"").hexdigest(),
        "selected_id": "12345", "outcome": "complete", "hit_count": 1,
        "first_hit_ids": ["hit-12345"],
        "status": "saved_tool_observation_not_evidence_use",
    }
    assert "SYNTHETIC_SOURCE_BODY_MUST_NOT_BE_REPLAYED" not in canonical(pack.messages)
    tight = reopened.assemble(
        call, task, archived_tool_turn_refs=[response_ref],
        allowance=len(canonical(pack.messages)) - 200, estimate_tokens=len,
    )
    assert "recent_scholarly_searches" not in json.loads(tight.messages[-1]["content"])
    assert any(item["type"] == "completed_tool_turn" for item in tight.omitted)
    inclusion = bind_no_source_pack(store, reopened, pack, "search-recap")
    page = reopened.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion, cursor=0, limit=1,
    )
    assert page["items"][0]["summary"]["search"]["hit_count"] == 1
    assert "SYNTHETIC_SOURCE_BODY_MUST_NOT_BE_REPLAYED" not in canonical(page)
    record = reopened.read_omitted_record(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion, pointer=response_ref, offset=0, limit=2000,
    )
    assert json.loads(record["text"])["search"]["first_hit_ids"] == ["hit-12345"]
    assert "SYNTHETIC_SOURCE_BODY_MUST_NOT_BE_REPLAYED" not in record["text"]
    with pytest.raises(ContextError, match="another peer"):
        reopened.read_omitted_record(
            mission_id="m", actor_id="sender", task_id="task",
            inclusion_ref=inclusion, pointer=response_ref, offset=0, limit=2000,
        )


@pytest.mark.parametrize("foreign_last_hit", [False, True])
def test_archived_search_recovers_later_saved_hit_ids_without_provider_call(
    tmp_path, foreign_last_hit
) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    hit_ids = [f"saved-hit-{index}" for index in range(5)]
    for index, hit_id in enumerate(hit_ids):
        mission_id = "other-mission" if foreign_last_hit and index == 4 else "m"
        store.transact(
            f"save-hit-{index}", {},
            lambda tx, hit_id=hit_id, mission_id=mission_id: ref(tx.put(
                "retrieval_hit", hit_id,
                {"mission_id": mission_id, "task_id": "task", "route": "pubmed.search"},
            )),
            mission_id=mission_id,
        )
    private = store.put_blob(canonical({
        "content": None,
        "tool_calls": [{
            "id": "saved-search-call", "type": "function",
            "function": {"name": "search_scholarly", "arguments": canonical({
                "route": "pubmed.search", "query": "synthetic trial", "limit": 5,
            })},
        }],
    }).encode())
    response_ref = store.transact(
        "saved-five-hit-search", {},
        lambda tx: ref(tx.put("model_response_receipt", "saved-five-hit-search", {
            "mission_id": "m", "task_id": "task", "call_ref": call,
            "status": "tool_requested", "blob": private,
        })),
        mission_id="m",
    )
    context = WorkingContext(store)
    context.save_tool_result(
        "saved-five-hit-result", response_ref=response_ref,
        tool_call_id="saved-search-call", name="search_scholarly",
        content=canonical({
            "route": "pubmed.search", "outcome": "success",
            "hits": [{"hit_id": hit_id} for hit_id in hit_ids],
            "private_source_text": "SYNTHETIC_BODY_NEVER_EXPOSED",
        }),
    )
    full = context.assemble(call, task, archived_tool_turn_refs=[response_ref])
    tight = context.assemble(
        call, task, archived_tool_turn_refs=[response_ref],
        allowance=len(canonical(full.messages)) - 150, estimate_tokens=len,
    )
    index = json.loads(tight.messages[-1]["content"])
    assert "recent_scholarly_searches" not in index
    navigation = index["latest_search_navigation"]
    assert navigation["first_hit_ids"] == hit_ids[:3]
    assert navigation["hit_count"] == 5
    assert navigation["remaining_hit_ids_fetch"]["id"] == response_ref["id"]
    assert navigation["remaining_hit_ids_fetch"]["tool"] == "read_context_record"
    assert "SYNTHETIC_BODY_NEVER_EXPOSED" not in canonical(tight.messages)
    inclusion = bind_no_source_pack(store, context, tight, "five-hit-inclusion")
    if foreign_last_hit:
        with pytest.raises(ContextError, match="outside this peer's exact task"):
            context.read_omitted_record(
                mission_id="m", actor_id="peer", task_id="task",
                inclusion_ref=inclusion, pointer=response_ref, offset=0, limit=120,
            )
        assert store.records("http_call") == []
        return
    pieces, offset = [], 0
    while True:
        window = context.read_omitted_record(
            mission_id="m", actor_id="peer", task_id="task",
            inclusion_ref=inclusion, pointer=response_ref, offset=offset, limit=120,
        )
        pieces.append(window["text"])
        if window["next_offset"] is None:
            break
        offset = window["next_offset"]
    projection = json.loads("".join(pieces))
    assert projection["search"]["saved_hit_ids"] == hit_ids
    assert projection["search"]["saved_hit_count"] == 5
    assert "SYNTHETIC_BODY_NEVER_EXPOSED" not in canonical(projection)
    assert store.records("http_call") == []

    pending = store.transact(
        "pending-search", {},
        lambda tx: ref(tx.put("model_response_receipt", "pending-search", {
            "mission_id": "m", "task_id": "task", "call_ref": call,
            "status": "tool_requested", "blob": private,
        })),
        mission_id="m",
    )
    with pytest.raises(ContextError, match="unresolved exact result"):
        context.assemble(call, task, archived_tool_turn_refs=[pending])


def test_archived_inspection_outcome_survives_new_search_and_context_paging(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    context = WorkingContext(store)

    def save_turn(number: int, name: str, arguments: dict, outcome: dict) -> dict:
        tool_id = f"tool-{number}"
        private = store.put_blob(canonical({
            "content": None,
            "tool_calls": [{
                "id": tool_id,
                "type": "function",
                "function": {"name": name, "arguments": canonical(arguments)},
            }],
        }).encode())
        response = store.transact(
            f"saved-turn-{number}", {},
            lambda tx: ref(tx.put("model_response_receipt", f"saved-turn-{number}", {
                "mission_id": "m", "task_id": "task", "call_ref": call,
                "status": "tool_requested", "blob": private,
            })),
            mission_id="m",
        )
        context.save_tool_result(
            f"saved-result-{number}", response_ref=response,
            tool_call_id=tool_id, name=name, content=canonical(outcome),
        )
        return response

    old_search = save_turn(1, "search_scholarly", {
        "route": "pubmed.pmid", "query": "", "selected_id": "12345", "limit": 1,
    }, {
        "route": "pubmed.pmid", "outcome": "success",
        "hits": [{"hit_id": "old-hit", "title": "Synthetic old result"}],
    })
    old_inspection = save_turn(2, "inspect_hit", {"hit_id": "old-hit"}, {
        "status": "metadata_only", "hit_id": "old-hit",
        "reason": "PRIVATE_SYNTHETIC_DIAGNOSTIC_NOT_FOR_RECAP",
    })
    new_search = save_turn(3, "search_scholarly", {
        "route": "europe_pmc.search", "query": "synthetic", "limit": 1,
    }, {
        "route": "europe_pmc.search", "outcome": "success",
        "hits": [{"hit_id": "new-hit", "title": "Synthetic new result"}],
    })

    reopened = WorkingContext(StateStore(tmp_path))
    pack = reopened.assemble(
        call, task,
        archived_tool_turn_refs=[old_search, old_inspection],
        tool_turn_refs=[new_search],
    )
    view = json.loads(pack.messages[-1]["content"])
    old = view["recent_scholarly_searches"][0]["search"]
    inspection = view["recent_hit_inspections"][0]
    assert old["first_hit_ids"] == ["old-hit"]
    assert old["known_hit_inspections"] == [inspection]
    assert inspection == {
        "hit_id": "old-hit", "status": "metadata_only",
        "result_ref": {"kind": "context_tool_result",
                       "id": f"tool-result:{old_inspection['id']}", "version": 1},
        "basis": "saved_tool_outcome_not_current_source_permission",
    }
    assert pack.messages[-2]["role"] == "tool"
    assert "new-hit" in pack.messages[-2]["content"]
    assert "PRIVATE_SYNTHETIC_DIAGNOSTIC_NOT_FOR_RECAP" not in canonical(pack.messages)

    inclusion = bind_no_source_pack(store, reopened, pack, "inspection-recap")
    inspection_position = next(
        index for index, item in enumerate(pack.omitted) if item["ref"] == old_inspection
    )
    page = reopened.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task", inclusion_ref=inclusion,
        cursor=inspection_position, limit=1,
    )
    assert page["items"][0]["summary"]["inspection"] == inspection
    record = reopened.read_omitted_record(
        mission_id="m", actor_id="peer", task_id="task", inclusion_ref=inclusion,
        pointer=old_inspection, offset=0, limit=2000,
    )
    assert json.loads(record["text"])["inspection"] == inspection
    assert "PRIVATE_SYNTHETIC_DIAGNOSTIC_NOT_FOR_RECAP" not in canonical(page)
    with pytest.raises(ContextError, match="another peer"):
        reopened.read_omitted_record(
            mission_id="m", actor_id="sender", task_id="task", inclusion_ref=inclusion,
            pointer=old_inspection, offset=0, limit=2000,
        )


def test_archived_successful_inspection_recaps_current_source_use_without_text(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)

    def save_source(tx):
        old = tx.get("source_copy", "copy")
        copy = tx.put("source_copy", "copy", {
            **old["body"], "mission_id": "m",
        }, expected_version=1)
        use = tx.put("source_use", "inspection-use", {
            "copy_ref": ref(copy), "allowed_uses": ["model_input"],
            "policy_status": "granted",
        })
        tx.bind("source_use.copy", "copy:2", "source_use", use["id"])
        return ref(copy)

    source_ref = store.transact("inspection-source", {}, save_source, mission_id="m")
    private = store.put_blob(canonical({
        "content": None,
        "tool_calls": [{
            "id": "inspect-1", "type": "function",
            "function": {"name": "inspect_hit", "arguments": canonical({"hit_id": "hit-1"})},
        }],
    }).encode())
    response = store.transact("inspection-response", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "inspection-response", {
            "mission_id": "m", "task_id": "task", "call_ref": call,
            "status": "tool_requested", "blob": private,
        },
    )), mission_id="m")
    context = WorkingContext(store)
    context.save_tool_result(
        "inspection-result", response_ref=response, tool_call_id="inspect-1",
        name="inspect_hit", effect_ref=source_ref,
        content=canonical({
            "status": "inspected", "source_id": source_ref["id"],
            "source_version": source_ref["version"],
            "preview": {"text": "SYNTHETIC_SOURCE_TEXT_NEVER_IN_RECAP"},
        }),
    )
    before = json.loads(context.assemble(
        call, task, archived_tool_turn_refs=[response],
    ).messages[-1]["content"])["recent_hit_inspections"][0]
    assert before["source_ref"] == source_ref
    assert before["current_model_input_status"] == "permitted"

    def revoke(tx):
        prior = tx.get("source_use", "inspection-use")
        return ref(tx.put("source_use", "inspection-use", {
            **prior["body"], "allowed_uses": [],
            "policy_status": "requires_source_policy_resolution",
        }, expected_version=prior["version"]))

    store.transact("inspection-revoke", {}, revoke, mission_id="m")
    pack = context.assemble(call, task, archived_tool_turn_refs=[response])
    after = json.loads(pack.messages[-1]["content"])["recent_hit_inspections"][0]
    assert after["source_ref"] == source_ref
    assert after["current_model_input_status"] == "restricted"
    assert "SYNTHETIC_SOURCE_TEXT_NEVER_IN_RECAP" not in canonical(pack.messages)


def test_current_source_progress_names_selected_spans_without_claiming_completeness(
    tmp_path,
) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    source_ref = {"kind": "source_copy", "id": "copy", "version": 1}

    def save(tx):
        first = tx.put("source_passage", "first-window", {
            "copy_ref": source_ref, "span_id": "first-window",
            "locator": "text; chars=0:2662; chars=0:1600", "text": "synthetic first",
            "inspection_state": "checked",
        })
        tail = tx.put("source_passage", "tail-window", {
            "copy_ref": source_ref, "span_id": "tail-window",
            "locator": "text; chars=0:2662; chars=1600:2662", "text": "synthetic tail",
            "inspection_state": "checked",
        })
        window = tx.put("source_read_window", "remaining-window", {
            "copy_ref": source_ref, "passage_refs": [ref(tail)],
            "selected_locators": [tail["body"]["locator"]],
            "next_cursor": None, "total_blocks": 1,
        })
        run = tx.put("mission_run", "source-run", {
            "mission_id": "m", "question": "Review synthetic abstract",
            "scope": "Abstract only", "source_refs": [source_ref],
        })
        control = tx.put("mission_task_control", "control", {
            "mission_id": "m", "run_id": run["id"],
            "actor_id": "peer", "task_id": "task",
            "source_refs": [source_ref],
            "selected_passage_refs": {"copy": [ref(first), ref(tail)]},
            "read_window_refs": [ref(window)],
        })
        return ref(control)

    control_ref = store.transact("source-progress", {}, save, mission_id="m")
    reopened = WorkingContext(StateStore(tmp_path))
    pack = reopened.assemble(call, task, source_refs=[source_ref], control_ref=control_ref)
    progress = json.loads(pack.messages[-1]["content"])["source_progress"][0]
    assert progress["selected_passage_count"] == 2
    assert [span["locator"] for span in progress["selected_spans"]] == [
        "text; chars=0:2662; chars=0:1600",
        "text; chars=0:2662; chars=1600:2662",
    ]
    assert progress["latest_window_next_cursor"] is None
    assert progress["status"] == (
        "saved_windows_not_past_model_delivery_or_original_completeness"
    )
    assert progress["extracted_text_coverage"]["status"] == "unknown"
    config = store.get(**pack.configuration_ref)
    request = {"messages": [
        {"role": "developer", "content": config["body"]["instructions"]},
        {"role": "user", "content": canonical({
            "source_packets": [{"id": "copy", "version": 1}],
        })},
        *pack.messages,
    ]}
    blob = store.put_blob(canonical(request).encode())
    intent_ref = store.transact(
        "mismatched-passage-intent", {},
        lambda tx: ref(tx.put("model_request_intent", "model-intent:call:1", {
            "mission_id": "m", "call_ref": call,
            "configuration_ref": pack.configuration_ref,
            "source_refs": [source_ref],
            "source_decisions": [{"copy_ref": source_ref, "passage_refs": []}],
            "request_sha256": hashlib.sha256(canonical(request).encode()).hexdigest(),
            "blob": blob,
        })),
        mission_id="m",
    )
    with pytest.raises(ContextError, match="prepared passage selection"):
        reopened.save_inclusion("mismatched-passage-inclusion", pack, intent_ref)
    store.transact(
        "advance-control", {},
        lambda tx: tx.put(
            "mission_task_control", "control",
            {**tx.get(**control_ref)["body"], "phase": "ready_model"},
            expected_version=control_ref["version"],
        ),
        mission_id="m",
    )
    with pytest.raises(ContextError, match="current task control"):
        reopened.assemble(
            call, task, source_refs=[source_ref], control_ref=control_ref,
        )


def test_source_progress_marks_retained_abstract_gap_then_exact_coverage(tmp_path) -> None:
    text = "DOI: 10.1234/coverage. " + "A" * 2638
    library, saved = checked_copy(tmp_path, text.encode(), selected=[text[:1600]])
    store = library.store
    call, task = fixture(store)
    copy_ref = saved["copy_ref"]

    def save_control(tx):
        run = tx.put("mission_run", "coverage-run", {
            "mission_id": "m", "question": "Review the retained abstract",
            "scope": "Abstract only", "source_refs": [copy_ref],
        })
        control = tx.put("mission_task_control", "coverage-control", {
            "mission_id": "m", "run_id": run["id"], "actor_id": "peer",
            "task_id": "task", "source_refs": [copy_ref],
            "read_window_refs": [],
        })
        return ref(control)

    control_ref = store.transact("coverage-control", {}, save_control, mission_id="m")
    context = WorkingContext(store)
    first = context.assemble(call, task, source_refs=[copy_ref], control_ref=control_ref)
    progress = json.loads(first.messages[-1]["content"])["source_progress"][0]
    coverage = progress["extracted_text_coverage"]
    assert coverage["status"] == "incomplete_extracted_text"
    assert coverage["scope"] == "retained_extracted_text_only"
    assert (coverage["covered_chars"], coverage["total_chars"]) == (1600, 2661)
    assert coverage["next_unread_cursor"] == {"block_offset": 0, "char_offset": 1600}
    selected = progress["selected_input_coverage"]
    assert (selected["covered_chars"], selected["total_chars"]) == (1600, 2661)
    assert selected["next_unread_cursor"] == {"block_offset": 0, "char_offset": 1600}
    assert "A" * 100 not in canonical(first.messages)

    remainder = read_source_window(
        library, "coverage-remainder", copy_ref,
        block_offset=0, char_offset=1600, block_limit=1, char_limit=2000,
    )

    def add_window(tx):
        prior = tx.get(**control_ref)
        updated = tx.put("mission_task_control", prior["id"], {
            **prior["body"], "read_window_refs": [remainder["window_ref"]],
        }, expected_version=prior["version"])
        return ref(updated)

    current_ref = store.transact("coverage-window", {}, add_window, mission_id="m")
    complete = context.assemble(call, task, source_refs=[copy_ref], control_ref=current_ref)
    progress = json.loads(complete.messages[-1]["content"])["source_progress"][0]
    coverage = progress["extracted_text_coverage"]
    assert coverage["status"] == "complete_extracted_text"
    assert coverage["uncovered_chars"] == 0
    assert coverage["next_unread_cursor"] is None
    assert progress["selected_input_coverage"]["covered_chars"] == 1600
    assert progress["selected_input_coverage"]["uncovered_chars"] == 1061


def test_replaced_window_keeps_saved_coverage_distinct_from_current_input(tmp_path) -> None:
    from ora.evidence import checked_model_text

    prefix = "DOI: 10.1234/coverage. Simple randomisation was used. "
    text = prefix + "A" * (2661 - len(prefix))
    library, saved = checked_copy(tmp_path, text.encode(), selected=[text[:1600]])
    store = library.store
    call, task = fixture(store)
    copy_ref = saved["copy_ref"]
    first = read_source_window(
        library, "coverage-first", copy_ref,
        block_offset=0, char_offset=0, block_limit=1, char_limit=1600,
    )
    tail = read_source_window(
        library, "coverage-tail", copy_ref,
        block_offset=0, char_offset=1600, block_limit=1, char_limit=2000,
    )
    first_ref, tail_ref = first["passage_refs"][0], tail["passage_refs"][0]
    assert "Simple randomisation" in store.get(**first_ref)["body"]["text"]
    assert "Simple randomisation" not in store.get(**tail_ref)["body"]["text"]
    assert "Simple randomisation" not in checked_model_text(
        library, copy_ref, [tail_ref]
    )["text"]

    def save_control(tx):
        run = tx.put("mission_run", "replaced-coverage-run", {
            "mission_id": "m", "question": "Review the retained abstract",
            "scope": "Abstract only", "source_refs": [copy_ref],
        })
        control = tx.put("mission_task_control", "replaced-coverage-control", {
            "mission_id": "m", "run_id": run["id"], "actor_id": "peer",
            "task_id": "task", "source_refs": [copy_ref],
            "selected_passage_refs": {copy_ref["id"]: [tail_ref]},
            "read_window_refs": [first["window_ref"], tail["window_ref"]],
        })
        return ref(control)

    replaced_ref = store.transact("replaced-coverage-control", {}, save_control, mission_id="m")
    replaced = WorkingContext(store).assemble(
        call, task, source_refs=[copy_ref], control_ref=replaced_ref,
    )
    view = json.loads(replaced.messages[-1]["content"])["source_progress"][0]
    assert view["selected_spans"] == [{
        "ref": tail_ref, "locator": store.get(**tail_ref)["body"]["locator"],
    }]
    assert view["extracted_text_coverage"]["status"] == "complete_extracted_text"
    assert view["extracted_text_coverage"]["covered_chars"] == 2661
    selected = view["selected_input_coverage"]
    assert selected["status"] == "incomplete_extracted_text"
    assert (selected["covered_chars"], selected["total_chars"]) == (1061, 2661)
    assert selected["next_unread_cursor"] == {"block_offset": 0, "char_offset": 0}
    assert "whole retained source" in view["whole_copy_absence_claim_guidance"]
    def estimate(value):
        return (len(value) * 5 + 15) // 16
    unbounded = WorkingContext(store).assemble(
        call, task, source_refs=[copy_ref], control_ref=replaced_ref,
        estimate_tokens=estimate,
    )
    tight = WorkingContext(store).assemble(
        call, task, source_refs=[copy_ref], control_ref=replaced_ref,
        allowance=unbounded.estimated_tokens - 100, estimate_tokens=estimate,
    )
    tight_index = json.loads(tight.messages[-1]["content"])
    assert "source_progress" not in tight_index
    required_scope = tight_index["source_retrieval"][0]["selected_input_scope"]
    assert (required_scope["covered_chars"], required_scope["total_chars"]) == (1061, 2661)
    assert required_scope["first_omitted_cursor"] == {"block_offset": 0, "char_offset": 0}
    assert "whole-source absence" in tight_index["source_retrieval"][0][
        "absence_claim_scope"
    ]

    def append_first(tx):
        prior = tx.get(**replaced_ref)
        return ref(tx.put("mission_task_control", prior["id"], {
            **prior["body"],
            "selected_passage_refs": {copy_ref["id"]: [first_ref, tail_ref]},
        }, expected_version=prior["version"]))

    appended_ref = store.transact("append-first-window", {}, append_first, mission_id="m")
    reopened = WorkingContext(StateStore(tmp_path / "state"))
    appended = reopened.assemble(call, task, source_refs=[copy_ref], control_ref=appended_ref)
    restored = json.loads(appended.messages[-1]["content"])["source_progress"][0]
    assert restored["selected_input_coverage"]["status"] == "complete_extracted_text"
    assert restored["selected_input_coverage"]["covered_chars"] == 2661
    assert "whole_copy_absence_claim_guidance" not in restored
    assert [item["ref"] for item in restored["selected_spans"]] == [first_ref, tail_ref]
    assert "Simple randomisation" in checked_model_text(
        library, copy_ref, [first_ref, tail_ref]
    )["text"]


def test_compacted_checked_window_uses_quality_for_original_partial_status(tmp_path) -> None:
    html = (
        b"<html><h1>Abstract</h1><p>DOI: 10.1234/coverage.</p>"
        b"<h2>Results</h2><p>A synthetic outcome was reported.</p>"
        b"<h2>Discussion</h2><p>Interpretation was limited.</p></html>"
    )
    library, saved = checked_copy(
        tmp_path, html, mime="text/html", surface="full_text", truncated=False,
    )
    window = read_source_window(
        library, "partial-surface-window", saved["copy_ref"], block_limit=1,
    )
    assert not any(item.get("reason") == "original_source_is_partial"
                   for item in window["omissions"])
    _result, compact = compact_checked_window_fixture(library, saved, window)
    assert compact["source_surface"] == "passage"
    assert compact["original_source_partial"] is True
    assert compact["source_text_status"] == "exact_checked_passages_in_this_model_input"


def test_compacted_pdf_window_preserves_exact_rendered_check_and_caution(
    tmp_path, monkeypatch,
) -> None:
    from tests.evidence.test_windows import checked_copy as checked_pdf_copy

    monkeypatch.setattr("ora.evidence.intake.subprocess.run", lambda *a, **k:
        subprocess.CompletedProcess(a[0], 0,
            b"DOI: 10.1234/window.\nResults\nTable 1.\nGroup n=40; value -3 mg.\f",
            b""))
    table_check = {
        "page": 1, "row": "Group", "header": "Group n=40", "units": "mg",
        "footnote": "none present", "rendered_original_sha256": "a" * 64,
        "reviewer_basis": "Synthetic rendered-page fixture",
    }
    library, saved = checked_pdf_copy(
        tmp_path, b"%PDF-1.4\nsynthetic", mime="application/pdf",
        table_check=table_check,
    )
    window = read_source_window(
        library, "pdf-context-window", saved["copy_ref"], block_offset=0,
        block_limit=1,
    )
    result, compact = compact_checked_window_fixture(library, saved, window)
    assert compact["table_context"] == result["table_context"]
    assert compact["table_context"]["rendered_original_check"] == table_check
    assert "does not validate a table cell" in compact["table_context"]["caution"]
    assert "table_context_in_saved_result" not in compact
    assert "text_preview" not in canonical(compact)


def test_source_free_pressure_keeps_exact_saved_reading_cursor_for_next_selected_call(
    tmp_path,
) -> None:
    from ora.evidence import checked_model_text

    prefix = "DOI: 10.1234/coverage. FIRST: verified methods. "
    middle = "RESULTS: synthetic primary outcome was measured. "
    text = prefix + "A" * (1600 - len(prefix)) + middle + "B" * (1600 - len(middle))
    text += "CONCLUSION: synthetic retained ending. " + "C" * 1000
    assert len(text) > 3200
    library, saved = checked_copy(tmp_path, text.encode(), selected=[text[:1600]])
    store = library.store
    call, task = fixture(store)
    copy_ref = saved["copy_ref"]
    first = read_source_window(
        library, "read-first", copy_ref,
        block_offset=0, char_offset=0, block_limit=1, char_limit=1600,
    )
    middle_window = read_source_window(
        library, "read-middle", copy_ref,
        block_offset=0, char_offset=1600, block_limit=1, char_limit=1600,
    )
    repeated = read_source_window(
        library, "read-again", copy_ref,
        block_offset=0, char_offset=0, block_limit=1, char_limit=800,
    )

    def save_control(tx):
        run = tx.put("mission_run", "reading-run", {
            "mission_id": "m", "question": "Review the retained abstract",
            "scope": "Abstract only", "source_refs": [copy_ref],
        })
        control = tx.put("mission_task_control", "reading-control", {
            "mission_id": "m", "run_id": run["id"], "actor_id": "peer",
            "task_id": "task", "source_refs": [copy_ref],
            "selected_passage_refs": {copy_ref["id"]: repeated["passage_refs"]},
            "read_window_refs": [
                first["window_ref"], middle_window["window_ref"], repeated["window_ref"],
            ],
        })
        return ref(control)

    control_ref = store.transact("reading-control", {}, save_control, mission_id="m")
    context = WorkingContext(store)

    def estimate(value):
        return (len(value) * 5 + 15) // 16

    unbounded = context.assemble(
        call, task, source_refs=[], control_ref=control_ref,
        estimate_tokens=estimate,
    )
    tight = context.assemble(
        call, task, source_refs=[], control_ref=control_ref,
        allowance=unbounded.estimated_tokens - 100, estimate_tokens=estimate,
    )
    index = json.loads(tight.messages[-1]["content"])
    assert "retained_reading_progress" not in index
    navigation = index["source_free_reading_navigation"][0]
    assert navigation["copy_ref"] == copy_ref
    assert navigation["saved_coverage"] == {
        "status": "incomplete_extracted_text",
        "scope": "retained_extracted_text_only",
        "basis": "checked_selected_passages_and_saved_windows",
        "original_source_partial": False,
        "covered_chars": 3200,
        "total_chars": len(text),
        "first_uncovered_cursor": {"block_offset": 0, "char_offset": 3200},
    }
    assert navigation["latest_window_ref"] == repeated["window_ref"]
    assert navigation["latest_window_next_cursor"] == {
        "block_offset": 0, "char_offset": 800,
    }
    assert navigation["furthest_start_saved_window"] == {
        "window_ref": middle_window["window_ref"],
        "start_cursor": {"block_offset": 0, "char_offset": 1600},
        "char_limit": 1600,
        "next_cursor": {"block_offset": 0, "char_offset": 3200},
    }
    assert navigation["fetch_first_uncovered"] == {
        "tool": "read_source_window", "source_id": copy_ref["id"],
        "source_version": copy_ref["version"],
        "block_offset": 0, "char_offset": 3200, "char_limit": 1900,
    }
    assert navigation["source_text_in_this_call"] is False
    assert navigation["past_model_delivery"] == "unverified"
    assert middle not in canonical(tight.messages)

    # A later model-selected read can admit exact checked passages. Saved
    # coverage in the source-free call did not itself select or cite their text.
    remainder = read_source_window(
        library, "read-remainder", copy_ref,
        block_offset=0, char_offset=3200, block_limit=1, char_limit=1600,
    )

    def select_saved_windows(tx):
        prior = tx.get(**control_ref)
        return ref(tx.put("mission_task_control", prior["id"], {
            **prior["body"],
            "selected_passage_refs": {copy_ref["id"]: [
                *first["passage_refs"], *middle_window["passage_refs"],
                *remainder["passage_refs"],
            ]},
            "read_window_refs": [
                *prior["body"]["read_window_refs"], remainder["window_ref"],
            ],
        }, expected_version=control_ref["version"]))

    selected_ref = store.transact(
        "select-saved-reading", {}, select_saved_windows, mission_id="m",
    )
    selected = context.assemble(
        call, task, source_refs=[copy_ref], control_ref=selected_ref,
        estimate_tokens=estimate,
    )
    selected = context.assemble(
        call, task, source_refs=[copy_ref], control_ref=selected_ref,
        allowance=selected.estimated_tokens, estimate_tokens=estimate,
    )
    selected_index = json.loads(selected.messages[-1]["content"])
    selected_scope = selected_index["source_retrieval"][0]["selected_input_scope"]
    assert selected_scope["status"] == "complete_extracted_text"
    assert selected_scope["covered_chars"] == len(text)
    checked = checked_model_text(
        library, copy_ref,
        [*first["passage_refs"], *middle_window["passage_refs"],
         *remainder["passage_refs"]],
    )
    assert middle in checked["text"]
    config = store.get(**selected.configuration_ref)
    request = {"messages": [
        {"role": "developer", "content": config["body"]["instructions"]},
        {"role": "user", "content": canonical({"source_packets": [{
            "id": copy_ref["id"], "version": copy_ref["version"],
            "text": checked["text"],
            "sha256": hashlib.sha256(checked["text"].encode()).hexdigest(),
        }]})},
        *selected.messages,
    ]}
    encoded = canonical(request).encode()
    blob = store.put_blob(encoded)

    def save_intent(tx):
        return ref(tx.put("model_request_intent", "model-intent:call:1", {
            "mission_id": "m", "call_ref": call,
            "configuration_ref": selected.configuration_ref,
            "source_refs": [copy_ref],
            "source_decisions": [{
                "copy_ref": copy_ref,
                "passage_refs": [
                    *first["passage_refs"], *middle_window["passage_refs"],
                    *remainder["passage_refs"],
                ],
            }],
            "request_sha256": hashlib.sha256(encoded).hexdigest(), "blob": blob,
        }))

    intent_ref = store.transact("selected-reading-intent", {}, save_intent, mission_id="m")
    inclusion_ref = context.save_inclusion("selected-reading-inclusion", selected, intent_ref)
    assert store.get(**inclusion_ref)["body"]["source_refs"] == [copy_ref]

    def revoke_use(tx):
        prior = tx.get(**saved["source_use_ref"])
        return ref(tx.put("source_use", prior["id"], {
            **prior["body"], "allowed_uses": [],
            "policy_status": "requires_source_policy_resolution",
        }, expected_version=prior["version"]))

    store.transact("revoke-reading-use", {}, revoke_use, mission_id="m")
    restricted = context.assemble(
        call, task, source_refs=[], control_ref=selected_ref,
    )
    assert "source_free_reading_navigation" not in json.loads(
        restricted.messages[-1]["content"]
    )


def test_supplied_unmapped_passage_keeps_only_independently_verified_window_navigation(
    tmp_path,
) -> None:
    text = "DOI: 10.1234/coverage. " + "A" * 1600 + "B" * 1600 + "C" * 1000
    library, saved = checked_copy(tmp_path, text.encode())
    store = library.store
    copy_ref = saved["copy_ref"]
    call, task = fixture(store)
    first = read_source_window(
        library, "supplied-first", copy_ref,
        block_offset=0, char_offset=0, block_limit=1, char_limit=1600,
    )
    later = read_source_window(
        library, "supplied-later", copy_ref,
        block_offset=0, char_offset=1600, block_limit=1, char_limit=1600,
    )

    def save_control(tx):
        old_passage = tx.put("source_passage", "unmapped-supplied-passage", {
            "copy_ref": copy_ref, "inspection_state": "checked",
            "locator": "user-supplied-original-without-character-offset",
            "text": text[:200],
        })
        run = tx.put("mission_run", "supplied-navigation-run", {
            "mission_id": "m", "question": "Read later saved result",
            "scope": "Synthetic supplied text", "source_refs": [copy_ref],
        })
        control = tx.put("mission_task_control", "supplied-navigation-control", {
            "mission_id": "m", "run_id": run["id"], "actor_id": "peer",
            "task_id": "task", "source_refs": [copy_ref],
            "selected_passage_refs": {copy_ref["id"]: [ref(old_passage)]},
            "read_window_refs": [first["window_ref"], later["window_ref"]],
        })
        return ref(control)

    control_ref = store.transact("supplied-navigation-control", {}, save_control, mission_id="m")
    context = WorkingContext(store)
    view = json.loads(context.assemble(
        call, task, source_refs=[], control_ref=control_ref,
    ).messages[-1]["content"])["source_free_reading_navigation"][0]
    assert view["saved_coverage"]["basis"] == "verified_saved_windows_only"
    assert view["saved_coverage"]["status"] == "incomplete_extracted_text"
    assert view["saved_coverage"]["covered_chars"] == 3200
    assert view["saved_coverage"]["first_uncovered_cursor"] == {
        "block_offset": 0, "char_offset": 3200,
    }
    assert view["latest_window_ref"] == later["window_ref"]
    assert view["latest_window_next_cursor"] == {
        "block_offset": 0, "char_offset": 3200,
    }
    assert view["source_text_in_this_call"] is False

    def add_invalid_window(tx):
        prior = tx.get(**control_ref)
        forged = tx.put("source_read_window", "forged-window", {
            "copy_ref": copy_ref, "next_cursor": {"block_offset": 0, "char_offset": 9999},
            "content_sha256": "0" * 64,
        })
        return ref(tx.put("mission_task_control", prior["id"], {
            **prior["body"],
            "read_window_refs": [*prior["body"]["read_window_refs"], ref(forged)],
        }, expected_version=control_ref["version"]))

    forged_control_ref = store.transact(
        "forged-navigation-window", {}, add_invalid_window, mission_id="m",
    )
    guarded = json.loads(context.assemble(
        call, task, source_refs=[], control_ref=forged_control_ref,
    ).messages[-1]["content"])["source_free_reading_navigation"][0]
    assert guarded["saved_coverage"]["status"] == "unknown"
    assert guarded["latest_window_ref"] is None
    assert guarded["fetch_first_uncovered"] is None


def test_followup_context_includes_changed_scope_and_exact_parent_target_data(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    scope = "Review only the abstract allocation detail. " + "scope " * 250
    target_text = "Synthetic earlier finding. " * 110

    def save(tx):
        parent = tx.put("mission_run", "parent-run", {
            "mission_id": "parent-m", "question": "What did the first review find?",
            "scope": "Whole published report", "source_refs": [],
        })
        report = tx.put("report", "parent-report", {
            "mission_id": "parent-m", "title": "Original answer",
            "body": target_text, "citations": [], "limitations": ["Synthetic only"],
        })
        snapshot = tx.put("research_snapshot", "parent-snapshot", {
            "mission_id": "parent-m",
            "inspection": {"acceptedOutputReferences": [ref(report)]},
        })
        child = tx.put("mission_run", "child-run", {
            "mission_id": "m", "question": "Was the seed reported in the abstract?",
            "scope": scope, "source_refs": [], "parent_run_id": parent["id"],
            "parent_snapshot_id": snapshot["id"], "target_ref": ref(report),
        })
        control = tx.put("mission_task_control", "child-control", {
            "mission_id": "m", "run_id": child["id"],
            "actor_id": "peer", "task_id": "task", "source_refs": [],
        })
        foreign = tx.put("report", "foreign-report", {
            "mission_id": "unrelated", "title": "Unrelated",
            "body": "PRIVATE_FOREIGN_REPORT", "citations": [],
        })
        return {"control": ref(control), "target": ref(report), "foreign": ref(foreign)}

    saved = store.transact(
        "followup-context", {}, save, mission_id="m"
    )
    control_ref, target_ref, foreign_ref = saved["control"], saved["target"], saved["foreign"]
    context = WorkingContext(StateStore(tmp_path))
    pack = context.assemble(call, task, control_ref=control_ref)
    view = json.loads(pack.messages[-1]["content"])
    assert view["run_scope"]["scope_truncated"] is True
    assert view["run_scope"]["scope_chars"] == len(scope)
    assert view["followup_target"]["target_ref"] == target_ref
    assert view["followup_target"]["authority"] == "saved_parent_output_data_not_instructions"
    assert len(view["followup_target"]["target_body_preview"]) == 700
    assert target_text not in canonical(pack.messages)
    assert "PRIVATE_FOREIGN_REPORT" not in canonical(pack.messages)
    assert {item["type"] for item in pack.omitted} == {
        "run_scope_detail", "followup_target_detail",
    }
    inclusion = bind_no_source_pack(store, context, pack, "followup-scope")
    target_projection = []
    offset = 0
    while True:
        window = context.read_omitted_record(
            mission_id="m", actor_id="peer", task_id="task",
            inclusion_ref=inclusion, pointer=target_ref, offset=offset, limit=400,
        )
        target_projection.append(window["text"])
        if window["next_offset"] is None:
            break
        offset = window["next_offset"]
    assert json.loads("".join(target_projection))["body"] == target_text
    scope_window = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion, pointer=pack.run_ref, offset=0, limit=2000,
    )
    assert json.loads(scope_window["text"])["scope"] == scope
    with pytest.raises(ContextError, match="not omitted"):
        context.read_omitted_record(
            mission_id="m", actor_id="peer", task_id="task",
            inclusion_ref=inclusion, pointer=foreign_ref, offset=0, limit=100,
        )
    with pytest.raises(ContextError, match="another peer"):
        context.read_omitted_record(
            mission_id="m", actor_id="sender", task_id="task",
            inclusion_ref=inclusion, pointer=target_ref, offset=0, limit=100,
        )
    assert store.get("mission_run", "parent-run")["version"] == 1
    store.transact(
        "foreign-target-revision", {},
        lambda tx: tx.put(
            "mission_run", "child-run",
            {**tx.get("mission_run", "child-run")["body"], "target_ref": foreign_ref},
            expected_version=1,
        ),
        mission_id="m",
    )
    with pytest.raises(ContextError, match="Exact accepted parent follow-up target"):
        context.assemble(call, task, control_ref=control_ref)


@pytest.mark.parametrize("include_source", [True, False])
def test_followup_target_text_is_hidden_after_source_use_revocation(tmp_path, include_source) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    source_ref = {"kind": "source_copy", "id": "copy", "version": 1}
    secret = "SYNTHETIC_DERIVED_SOURCE_TEXT_NOT_FOR_REPLAY"

    def save(tx):
        use = tx.put("source_use", "followup-use", {
            "copy_ref": source_ref, "allowed_uses": ["model_input"],
            "policy_status": "granted",
        })
        tx.bind("source_use.copy", "copy:1", "source_use", use["id"])
        parent = tx.put("mission_run", "source-parent", {
            "mission_id": "parent-m", "question": "Synthetic source question?",
            "scope": "Source-grounded", "source_refs": [source_ref],
        })
        exact_input = {**source_ref, "sha256": content_hash(tx.get(**source_ref)["body"])}
        producer_call = tx.put("agent_call", "source-parent-call", {
            "mission_id": "parent-m", "task_id": "parent-task",
            "input_refs": [exact_input],
        })
        producer = tx.put("agent_attempt", "source-parent-attempt", {
            "mission_id": "parent-m", "call_ref": ref(producer_call),
        })
        report = tx.put("report", "source-parent-report", {
            "mission_id": "parent-m", "title": secret, "body": secret,
            "citations": [{"sourceId": "copy", "spanId": "span"}],
            "citation_input_provenance": [{"copy_ref": source_ref}],
            "producer_attempt_ref": ref(producer),
            "limitations": [],
        })
        forged_call = tx.put("agent_call", "forged-parent-call", {
            "mission_id": "parent-m", "task_id": "parent-task",
            "input_refs": [{**source_ref, "sha256": "0" * 64}],
        })
        forged_attempt = tx.put("agent_attempt", "forged-parent-attempt", {
            "mission_id": "parent-m", "call_ref": ref(forged_call),
        })
        forged_report = tx.put("report", "forged-parent-report", {
            **report["body"], "producer_attempt_ref": ref(forged_attempt),
        })
        snapshot = tx.put("research_snapshot", "source-parent-snapshot", {
            "mission_id": "parent-m", "inspection": {
                "acceptedOutputReferences": [ref(report)],
                "sources": [{"copyReference": source_ref}],
            },
        })
        child = tx.put("mission_run", "source-child", {
            "mission_id": "m", "question": "Revise the cited finding?",
            "scope": "Follow up", "source_refs": [source_ref],
            "parent_run_id": parent["id"], "parent_snapshot_id": snapshot["id"],
            "target_ref": ref(report),
        })
        control = tx.put("mission_task_control", "source-child-control", {
            "mission_id": "m", "run_id": child["id"],
            "actor_id": "peer", "task_id": "task", "source_refs": [source_ref],
        })
        return {"control": ref(control), "target": ref(report),
                "snapshot": ref(snapshot), "forged_target": ref(forged_report)}

    saved = store.transact("source-followup", {}, save, mission_id="m")
    context = WorkingContext(store)
    assert context._target_text_access(
        store.get(**saved["target"]), store.get(**saved["snapshot"]), [source_ref],
    )["status"] == "available"
    assert context._target_text_access(
        store.get(**saved["forged_target"]), store.get(**saved["snapshot"]), [source_ref],
    )["status"] == "data_unavailable_due_source_use"
    omitted_access = context._target_text_access(
        store.get(**saved["target"]), store.get(**saved["snapshot"]), [],
    )
    assert omitted_access["status"] == "available"
    assert omitted_access["source_dependencies_omitted_from_input"] == [source_ref]
    assert omitted_access["unavailable_source_refs"] == []
    assert context._target_text_access(
        store.get(**saved["forged_target"]), store.get(**saved["snapshot"]), [],
    )["status"] == "data_unavailable_due_source_use"
    chosen_sources = [source_ref] if include_source else []
    pack = context.assemble(
        call, task, control_ref=saved["control"], source_refs=chosen_sources
    )
    assert secret[:20] in canonical(pack.messages)

    config = store.get(**pack.configuration_ref)
    request = {"messages": [
        {"role": "developer", "content": config["body"]["instructions"]},
        *([{"role": "user", "content": canonical({"source_packets": [
            {"id": item["id"], "version": item["version"]} for item in chosen_sources
        ]})}] if chosen_sources else []),
        *pack.messages,
    ]}
    encoded = canonical(request).encode()
    intent = store.transact("source-followup-intent", {}, lambda tx: ref(tx.put(
        "model_request_intent", "model-intent:call:1", {
            "mission_id": "m", "call_ref": call,
            "configuration_ref": pack.configuration_ref,
            "source_refs": chosen_sources,
            "context_source_dependency_refs": list(
                context.context_source_dependency_refs(pack)
            ),
            "source_decisions": [{"copy_ref": item, "passage_refs": []} for item in chosen_sources],
            "request_sha256": hashlib.sha256(encoded).hexdigest(),
            "blob": store.put_blob(encoded),
        },
    )), mission_id="m")
    inclusion = context.save_inclusion("source-followup-inclusion", pack, intent)
    position = next(index for index, item in enumerate(pack.omitted)
                    if item["type"] == "followup_target_detail")
    allowed_page = context.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion, cursor=position, limit=1,
    )
    assert allowed_page["items"][0]["summary"]["title"]["text"] == secret
    page_response_blob = store.put_blob(canonical({"content": None, "tool_calls": [{
        "id": "read-parent-page", "type": "function", "function": {
            "name": "read_context_page", "arguments": canonical({
                "inclusion_id": inclusion["id"], "cursor": position, "limit": 1,
            }),
        },
    }]}).encode())
    page_response = store.transact("source-page-response", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "source-page-response", {
            "mission_id": "m", "call_ref": call,
            "status": "tool_requested", "blob": page_response_blob,
        },
    )), mission_id="m")
    page_result = context.save_tool_result(
        "source-page-result", response_ref=page_response,
        tool_call_id="read-parent-page", name="read_context_page",
        content=canonical(allowed_page),
    )
    allowed_exchange, _ = context._tool_exchange(page_response)
    assert json.loads(allowed_exchange[-1]["content"])["items"][0]["summary"]["title"][
        "text"
    ] == secret
    page_continued = context.assemble(
        call, task, control_ref=saved["control"], source_refs=chosen_sources,
        tool_turn_refs=[page_response],
    )
    assert context.context_source_dependency_refs(page_continued) == (source_ref,)
    allowed_window = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion, pointer=saved["target"], offset=0, limit=2000,
    )
    assert json.loads(allowed_window["text"])["body"] == secret
    assert allowed_window["report_text_status"] == "available"
    assert allowed_window["source_dependencies_omitted_from_input"] == (
        [] if include_source else [source_ref]
    )
    with pytest.raises(ContextError, match="Exact parent follow-up target"):
        context._followup_target_text_access(
            store.get(**inclusion), store.get(**saved["forged_target"])
        )

    response_blob = store.put_blob(canonical({"content": None, "tool_calls": [{
        "id": "read-parent", "type": "function", "function": {
            "name": "read_context_record", "arguments": canonical({
                "inclusion_id": inclusion["id"], **saved["target"],
                "offset": 100, "limit": 2000,
            }),
        },
    }]}).encode())
    response = store.transact("source-report-response", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "source-report-response", {
            "mission_id": "m", "call_ref": call,
            "status": "tool_requested", "blob": response_blob,
        },
    )), mission_id="m")
    original_window = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion, pointer=saved["target"], offset=100, limit=2000,
    )
    result = context.save_tool_result(
        "source-report-result", response_ref=response, tool_call_id="read-parent",
        content=canonical(original_window), name="read_context_record",
    )
    exchange, _ = context._tool_exchange(response)
    assert json.loads(exchange[-1]["content"])["offset"] == 100
    continued = context.assemble(
        call, task, control_ref=saved["control"], source_refs=chosen_sources,
        tool_turn_refs=[response],
    )
    assert any(message.get("name") == "read_context_record"
               for message in continued.messages)
    assert context.context_source_dependency_refs(continued) == (source_ref,)

    def revoke(tx):
        prior = tx.get("source_use", "followup-use")
        updated = tx.put("source_use", "followup-use", {
            **prior["body"], "allowed_uses": [],
            "policy_status": "requires_source_policy_resolution",
        }, expected_version=prior["version"])
        return ref(updated)

    store.transact("source-followup-revoke", {}, revoke, mission_id="m")
    after = context.assemble(call, task, control_ref=saved["control"])
    target = json.loads(after.messages[-1]["content"])["followup_target"]
    assert target["target_text_status"] == "data_unavailable_due_source_use"
    assert target["source_dependency_refs"] == [source_ref]
    assert "target_body_preview" not in target and "target_title" not in target
    assert secret not in canonical(after.messages)

    page = context.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion, cursor=position, limit=1,
    )
    assert page["items"][0]["summary"]["target_text_status"] == "data_unavailable_due_source_use"
    assert secret not in canonical(page)
    window = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id="task",
        inclusion_ref=inclusion, pointer=saved["target"], offset=0, limit=2000,
    )
    assert json.loads(window["text"])["status"] == "data_unavailable_due_source_use"
    assert secret not in window["text"]
    exchange, changed = context._tool_exchange(response)
    replayed = json.loads(exchange[-1]["content"])
    assert replayed["report_text_status"] == "data_unavailable_due_source_use"
    assert replayed["offset"] == 0  # A prior text cursor must not hide the revocation diagnostic.
    assert secret not in canonical(exchange)
    assert changed["status"] == "report_current_use_rechecked"
    stored_payload = json.loads(store.read_blob(store.get(**result)["body"]["blob"]["sha256"]))
    assert stored_payload["content"] == canonical(original_window)  # Immutable historical result.
    page_exchange, page_changed = context._tool_exchange(page_response)
    page_summary = json.loads(page_exchange[-1]["content"])["items"][0]["summary"]
    assert page_summary["target_text_status"] == "data_unavailable_due_source_use"
    assert "title" not in page_summary and secret not in canonical(page_exchange)
    assert page_changed["status"] == "report_current_use_rechecked"
    page_after = context.assemble(
        call, task, control_ref=saved["control"], tool_turn_refs=[page_response],
    )
    assert context.context_source_dependency_refs(page_after) == ()
    saved_page = json.loads(store.read_blob(store.get(**page_result)["body"]["blob"]["sha256"]))
    assert saved_page["content"] == canonical(allowed_page)


def test_report_with_unselected_unknown_source_policy_remains_unavailable(tmp_path):
    store = StateStore(tmp_path)
    fixture(store)
    source = {"kind": "source_copy", "id": "copy", "version": 1}
    access = WorkingContext(store)._target_text_access(
        {"body": {"citations": [], "citation_input_provenance": []}},
        {"body": {"inspection": {"sources": [{"copyReference": source}]}}}, [],
    )
    assert access["dependency_lineage_complete"]
    assert access["source_dependencies_omitted_from_input"] == [source]
    assert access["unavailable_source_refs"] == [source]
    assert access["status"] == "data_unavailable_due_source_use"


def test_rejected_report_read_replays_its_diagnostic_without_reexecuting(tmp_path):
    store = StateStore(tmp_path)
    fixture(store)
    context = WorkingContext(store)
    raw = store.put_blob(canonical({"content": None, "tool_calls": [{
        "id": "malformed-read", "type": "function", "function": {
            "name": "read_context_record", "arguments": canonical({"kind": "report"}),
        },
    }]}).encode())
    response = store.transact("rejected-report-response", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "rejected-report-response", {
            "mission_id": "m", "status": "tool_requested", "blob": raw,
        },
    )), mission_id="m")
    diagnostic = canonical({"status": "rejected", "reason": "Required arguments missing"})
    context.save_tool_result(
        "rejected-report-result", response_ref=response, tool_call_id="malformed-read",
        content=diagnostic, name="read_context_record",
    )
    exchange, changed = context._tool_exchange(response)
    assert exchange[-1]["content"] == diagnostic
    assert changed is None


def test_source_fit_recovery_separates_saved_read_from_current_delivery(tmp_path):
    from tests.mission.test_context_window_compaction import _window_turn

    store, context, task, copy_ref, window, refs, result_ref = _window_turn(tmp_path)
    original = store.read_blob(store.get(**result_ref)["body"]["blob"]["sha256"])
    history = [{"role": "user", "content": canonical({
        "source_context_mode": "source_free_recovery",
        "reason": "exact_source_text_exceeds_allowance",
    })}]
    # A fitting source call still carries the selected checked text through its
    # source packet. The subsequent recovery does not claim that call is memory.
    fitted = context.assemble(
        refs["next_call"], task, source_refs=[copy_ref],
        control_ref=refs["control"], tool_turn_refs=[refs["response"]],
    )
    assert "source_read_delivery" not in json.loads(fitted.messages[-1]["content"])
    assert json.loads(fitted.messages[-2]["content"])["source_text_status"] == (
        "exact_checked_passages_in_this_model_input"
    )
    pack = context.assemble(
        refs["source_free_call"], task, control_ref=refs["control"], history=history,
        tool_turn_refs=[refs["response"]],
    )
    delivered = json.loads(pack.messages[-2]["content"])
    assert delivered["schema"] == "ora.source_window_delivery.v1"
    assert delivered["saved_read_succeeded"]
    assert delivered["source_text_in_this_call"] is False
    assert delivered["past_model_delivery"] == "not_established_by_saved_read"
    assert delivered["status"] == "saved_text_omitted_for_input_fit"
    assert delivered["window_ref"] == window["window_ref"]
    assert delivered["result_ref"] == result_ref
    assert delivered["requested_window"] == {
        "block_offset": 0, "char_offset": 1600, "char_limit": 800,
    }
    assert delivered["returned_chars"] == 800
    assert "selection_mode=replace" in delivered["next_action"]
    assert "source-linked notes" in delivered["next_action"]
    assert "A" * 100 not in canonical(pack.messages)
    assert pack.messages[-3]["tool_calls"][0]["id"] == pack.messages[-2]["tool_call_id"]
    assert store.read_blob(store.get(**result_ref)["body"]["blob"]["sha256"]) == original


def test_source_fit_feedback_survives_archiving_and_later_peer_tool(tmp_path):
    from tests.mission.test_context_window_compaction import _window_turn

    store, context, task, copy_ref, window, refs, result_ref = _window_turn(tmp_path)
    raw = store.put_blob(canonical({"content": None, "tool_calls": [{
        "id": "peer-step", "type": "function", "function": {
            "name": "peer_request", "arguments": canonical({"question": "Review source"}),
        },
    }]}).encode())
    response = store.transact("later-peer-step", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "later-peer-step", {
            "mission_id": "m", "task_id": task["id"], "call_ref": refs["source_free_call"],
            "status": "tool_requested", "blob": raw,
        },
    )), mission_id="m")
    context.save_tool_result(
        "later-peer-result", response_ref=response, tool_call_id="peer-step",
        name="peer_request", content=canonical({"status": "accepted"}),
    )
    history = [{"role": "user", "content": canonical({
        "source_context_mode": "source_free_recovery",
    })}]
    pack = context.assemble(
        refs["source_free_call"], task, control_ref=refs["control"], history=history,
        archived_tool_turn_refs=[refs["response"]], tool_turn_refs=[response],
    )
    index = json.loads(pack.messages[-1]["content"])
    feedback = index["source_read_delivery"]
    assert feedback["copy_ref"] == copy_ref
    assert feedback["window_ref"] == window["window_ref"]
    assert feedback["result_ref"] == result_ref
    assert feedback["source_text_in_this_call"] is False
    assert json.loads(pack.messages[-2]["content"]) == {"status": "accepted"}
    assert any(item["ref"] == refs["response"] for item in pack.omitted)
    assert "A" * 100 not in canonical(pack.messages)


def test_source_fit_feedback_does_not_turn_revocation_into_size_recovery(tmp_path):
    from tests.mission.test_context_window_compaction import _window_turn

    store, context, task, copy_ref, _window, refs, result_ref = _window_turn(tmp_path)
    use_ref = context._source_status(store.get(**copy_ref))["current_source_use_ref"]
    def revoke(tx):
        prior = tx.get(**use_ref)
        return ref(tx.put(prior["kind"], prior["id"], {
            **prior["body"], "allowed_uses": [],
            "policy_status": "requires_source_policy_resolution",
        }, expected_version=prior["version"]))
    store.transact("revoke-window-source", {}, revoke, mission_id="m")
    pack = context.assemble(
        refs["source_free_call"], task, control_ref=refs["control"],
        history=[{"role": "user", "content": canonical({
            "source_context_mode": "source_free_recovery",
        })}], tool_turn_refs=[refs["response"]],
    )
    feedback = json.loads(pack.messages[-2]["content"])
    assert feedback["status"] == "saved_text_unavailable_under_current_source_policy"
    assert "smaller" not in feedback["next_action"]
    assert "A" * 100 not in canonical(pack.messages)
    assert store.get(**result_ref) is not None


@pytest.mark.parametrize("same_run", [True, False])
def test_pending_outgoing_request_names_only_same_run_recipient_control(tmp_path, same_run):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    sent = PeerMailbox(store).send_request(
        "ask-context-peer", mission_id="m", request_id="context-request",
        message_id="context-message", requester_id="peer", recipient_id="sender",
        question="Review the saved source", expected_artifact="Independent review",
        cause_ref=task, payload={}, task_ref=task,
    )
    def save(tx):
        run = tx.put("mission_run", "current-run", {
            "mission_id": "m", "question": "Compare evidence", "scope": "Retained text",
            "source_refs": [],
        })
        own = tx.put("mission_task_control", "requester-control", {
            "mission_id": "m", "run_id": run["id"], "actor_id": "peer",
            "task_id": task["id"], "source_refs": [],
        })
        recipient = tx.put("mission_task_control", "recipient-control", {
            "mission_id": "m", "run_id": run["id"] if same_run else "other-run",
            "actor_id": "sender", "task_id": "recipient-task",
        })
        return {"control": ref(own), "recipient": ref(recipient)}
    saved = store.transact("controls-for-peer-context", {}, save, mission_id="m")
    control, recipient = saved["control"], saved["recipient"]
    pack = WorkingContext(store).assemble(call, task, control_ref=control)
    disposition = json.loads(pack.messages[-1]["content"])["request_dispositions"][0]
    assert disposition["ref"] == sent["request_ref"]
    if same_run:
        assert disposition["recipient_control_ref"] == recipient
        assert "Continue independent work" in disposition["next_action"]
        assert "wait_for_dependencies" in disposition["next_action"]
        assert "review_peer_answer" in disposition["next_action"]
    else:
        assert "recipient_control_ref" not in disposition
        assert "next_action" not in disposition


def test_source_fit_feedback_preserves_first_gap_separately_from_latest_cursor(tmp_path):
    from tests.mission.test_context_window_compaction import _window_turn

    store, context, task, _copy_ref, window, refs, _result_ref = _window_turn(tmp_path)
    def select_later_window(tx):
        prior = tx.get(**refs["control"])
        return ref(tx.put(prior["kind"], prior["id"], {
            **prior["body"], "selected_passage_refs": {
                window["copy_ref"]["id"]: window["passage_refs"],
            },
        }, expected_version=prior["version"]))
    control = store.transact("later-window-only", {}, select_later_window, mission_id="m")
    pack = context.assemble(
        refs["source_free_call"], task, control_ref=control,
        archived_tool_turn_refs=[refs["response"]],
        history=[{"role": "user", "content": canonical({
            "source_context_mode": "source_free_recovery",
            "reason": "exact_source_text_exceeds_allowance",
            "inventory": {"inventory_ref": {"kind": "source_inventory", "id": "saved", "version": 1}},
            "next_action": "Generic recovery instructions replaced by exact window feedback",
        })}],
    )
    index = json.loads(pack.messages[-1]["content"])
    feedback = index["source_read_delivery"]
    assert feedback["first_uncovered_cursor"] == {"block_offset": 0, "char_offset": 0}
    assert feedback["latest_window_next_cursor"] == {"block_offset": 0, "char_offset": 2400}
    assert feedback["coverage_basis"] == "saved_reading_not_model_delivery"
    marker = json.loads(pack.messages[0]["content"])
    assert marker["source_context_mode"] == "source_free_recovery"
    assert marker["reason"] == "exact_source_text_exceeds_allowance"
    assert marker["inventory"]["inventory_ref"]["id"] == "saved"
    assert "next_action" not in marker
