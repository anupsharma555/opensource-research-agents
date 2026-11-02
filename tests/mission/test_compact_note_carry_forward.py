"""A source-linked peer note can survive a tight, checked-source continuation.

These are injected host mechanics, not evidence of natural note writing or use.
"""

from __future__ import annotations

import json

from ora.coordination.board import ref
from ora.coordination.mailbox import PeerMailbox
from ora.evidence import read_source_window
from ora.mission.context import WorkingContext
from ora.mission.source_context import plan_source_turn
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_selected_window_admission import _saved_tool_request
from tests.mission.test_source_rich_preparation import _three_source_host


# Leave room for token-count variation in generated record identities while
# remaining below the observed 8,000-token request window.
def _setup(tmp_path, monkeypatch, *, with_peer=False, items=55, limit=7000):
    host, run, sources, _ = _three_source_host(tmp_path, monkeypatch, items=items, limit=limit)
    control = host._control(run.run_id, "agent-critique")
    selected, windows = {}, []
    for number, pointer in enumerate(sources):
        window = read_source_window(
            host.evidence, f"synthetic-note-window-{number}", pointer,
            block_offset=0, block_limit=1, char_limit=16000,
        )
        selected[pointer["id"]] = window["passage_refs"]
        windows.append(window["window_ref"])
    host._update_control(control, selected_passage_refs=selected, read_window_refs=windows)
    control = host._control(run.run_id, "agent-critique")
    proposal = host.store.get("board_proposal", control["body"]["task_id"])
    text = "Synthetic peer-authored comparison note. " + "X" * 300
    note_ref = host.notes.write(
        "synthetic-source-linked-checkpoint", mission_id=control["body"]["mission_id"],
        actor_id=control["body"]["actor_id"], task_ref=ref(proposal),
        note_id="synthetic-source-linked-checkpoint", category="checkpoint",
        text=text, input_refs=sources,
    )
    if with_peer:
        recipient = host._control(run.run_id, "agent-synthesis")
        sent = PeerMailbox(host.store).send_request(
            "synthetic-note-peer-request", mission_id=control["body"]["mission_id"],
            request_id="synthetic-note-peer-request", message_id="synthetic-note-peer-message",
            requester_id=control["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"],
            question="Check one source-dependent design point.",
            expected_artifact="A source-grounded reply", cause_ref=ref(proposal),
            payload={"question": "Check one source-dependent design point."},
            task_ref=ref(proposal),
        )
        assert sent["status"] == "accepted"
        PeerMailbox(host.store).mark_delivered(
            "synthetic-note-peer-delivery", mission_id=control["body"]["mission_id"],
            message_id="synthetic-note-peer-message",
        )
    return host, control, proposal, sources, selected, note_ref, text


def _preview(host, control, proposal, sources, selected):
    plan = plan_source_turn(host.store, ref(control), source_input_allowance_tokens=1)
    one = sources[-1]
    candidate = {
        **plan, "mode": "requested_window_recovery",
        "source_inputs": [{"copy_ref": one, "passage_refs": selected[one["id"]]}],
    }
    captured, packs = [], []
    original_preview = host.session.preview_request
    original_assemble = host.context.assemble

    def capture_preview(**kwargs):
        prepared = original_preview(**kwargs)
        captured.append(prepared)
        return prepared

    def capture_pack(*args, **kwargs):
        pack = original_assemble(*args, **kwargs)
        packs.append(pack)
        return pack

    host.session.preview_request = capture_preview
    host.context.assemble = capture_pack
    try:
        _, excluded = host._source_policy_decisions(control["body"]["source_refs"])
        fit = host._preview_soft_source_plan(
            control, proposal, candidate, total_target=7000,
            sequence=control["body"]["sequence"] + 1,
            excluded_sources=excluded, defer_core_tools=True,
            defer_recoverable_schemas=True, require_peer_input=True,
        )
    finally:
        host.session.preview_request = original_preview
        host.context.assemble = original_assemble
    assert fit is not None
    prepared = next(item for item in captured if item.request_sha256 == fit["request_sha256"])
    pack = next(item for item in packs
                if prepared.body["messages"][-len(item.messages):] == item.messages)
    index = json.loads(pack.messages[-1]["content"])
    return fit, prepared, pack, index


def test_compact_note_carries_three_exact_dependencies_with_source_and_peer_route(
    tmp_path, monkeypatch,
):
    host, control, proposal, sources, selected, note_ref, text = _setup(
        tmp_path, monkeypatch, with_peer=True,
    )
    try:
        fit, prepared, pack, index = _preview(host, control, proposal, sources, selected)
        card = index["working_note_continuation"]
        assert fit["estimated_request_tokens"] <= 7000
        assert card == {
            "ref": note_ref, "category": "checkpoint",
            "status": "peer_draft_not_accepted_evidence",
            "authority": "own_draft_not_source_evidence",
            "text": text, "source_dependency_refs": sources,
        }
        assert pack.note_text_refs == (note_ref,)
        assert host.context.context_source_dependency_refs(pack) == tuple(sources)
        assert index["request_dispositions"][0]["recipient_control_ref"]
        assert index["request_dispositions"][0]["status"] == "unanswered"
        assert index["older_records"]["tool"] == "read_context_page"
        packets = json.loads(prepared.body["messages"][1]["content"])["source_packets"]
        assert len(packets) == 1 and packets[0]["id"] == sources[-1]["id"]
        assert packets[0]["passage_refs"] == selected[sources[-1]["id"]]
        assert all(item["function"]["description"] for item in prepared.body["tools"])
        assert not host.store.records("model_dispatch_attempt")

        # A new process can resolve the exact saved note and its dependencies.
        reopened = WorkingContext(StateStore(tmp_path, read_only=True))
        assert reopened.working_note_text_access(host.store.get(**note_ref))[
            "source_dependency_refs"
        ] == sources
    finally:
        host.close()


def test_compact_note_uses_latest_revision_and_hides_revoked_dependency(
    tmp_path, monkeypatch,
):
    host, control, proposal, sources, selected, note_ref, text = _setup(
        tmp_path, monkeypatch,
    )
    try:
        revised_text = "Revised synthetic checkpoint. " + "Y" * 300
        revised = host.notes.write(
            "synthetic-note-revision", mission_id=control["body"]["mission_id"],
            actor_id=control["body"]["actor_id"], task_ref=ref(proposal),
            note_id=note_ref["id"], category="checkpoint", text=revised_text,
            input_refs=sources, expected_version=note_ref["version"],
        )
        _, prepared, pack, index = _preview(host, control, proposal, sources, selected)
        assert index["working_note_continuation"]["ref"] == revised
        assert index["working_note_continuation"]["text"] == revised_text
        assert text not in canonical(prepared.body)
        assert pack.note_text_refs == (revised,)

        use = host.store.resolve(
            "source_use.copy", f"{sources[0]['id']}:{sources[0]['version']}",
        )
        host.store.transact(
            "synthetic-note-revoke", {},
            lambda tx: tx.put(
                "source_use", use["id"],
                {**use["body"], "allowed_uses": [], "policy_status": "revoked"},
                expected_version=use["version"],
            ), mission_id=control["body"]["mission_id"],
        )
        assert host.context.restricted_working_notes([revised])[0][
            "unavailable_source_refs"
        ] == [sources[0]]
        _, prepared, pack, index = _preview(host, control, proposal, sources, selected)
        assert "working_note_continuation" not in index
        assert revised_text not in canonical(prepared.body)
        assert revised not in pack.note_text_refs
    finally:
        host.close()


def test_compact_note_survives_archived_tool_history_and_reopen(tmp_path, monkeypatch):
    host, control, proposal, sources, selected, note_ref, text = _setup(
        tmp_path, monkeypatch, with_peer=True, items=45, limit=8000,
    )
    try:
        assert host._schedule_model(control)
        control = host._control(control["body"]["run_id"], "agent-critique")
        turns = []
        for number in range(4):
            response, _ = _saved_tool_request(
                host, control, response_id=f"synthetic-note-history-{number}",
                tool_id=f"synthetic-note-history-tool-{number}",
                name="discover_capabilities", arguments={"offset": 0, "limit": 1000},
                result={"schema": "ora.capability_catalog.v1", "items": [],
                        "next_request": None},
            )
            turns.append(response)
        host._update_control(
            control, phase="ready_model", response_ref=turns[-1], tool_turn_refs=turns,
        )
        control = host._control(control["body"]["run_id"], "agent-critique")
        fit, prepared, pack, index = _preview(
            host, control, proposal, sources, selected,
        )
        assert fit["estimated_request_tokens"] <= 7000
        assert index["working_note_continuation"]["ref"] == note_ref
        assert index["working_note_continuation"]["text"] == text
        assert index["working_note_continuation"]["status"] == (
            "peer_draft_not_accepted_evidence"
        )
        assert len(pack.omitted) >= 4
        assert index["older_records"]["tool"] == "read_context_page"
        assert index["request_dispositions"][0]["status"] == "unanswered"
        assert host.context.context_source_dependency_refs(pack) == tuple(sources)
        assert all(item["function"]["description"] for item in prepared.body["tools"])
        reopened = WorkingContext(StateStore(tmp_path, read_only=True))
        assert reopened.working_note_text_access(host.store.get(**note_ref))[
            "source_dependency_refs"
        ] == sources
    finally:
        host.close()
