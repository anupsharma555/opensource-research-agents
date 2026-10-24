"""An omitted peer payload remains recoverable only for its saved input owner."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.coordination.board import ref
from ora.coordination.mailbox import PeerMailbox
from ora.mission.context import ContextError, WorkingContext
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack, fixture


def _saved_message(store: StateStore, *, attachment: dict | None, payload: dict) -> dict:
    mailbox = PeerMailbox(store)
    sent = mailbox.send_request(
        "send-nested-message", mission_id="m", request_id="nested-request",
        message_id="nested-message", requester_id="sender", recipient_id="peer",
        question="Review the nested comparison.", expected_artifact="Synthetic review",
        cause_ref=ref(store.get("board_proposal", "task")), payload=payload,
        task_ref=ref(store.get("board_proposal", "task")),
        evidence_refs=[attachment] if attachment else [],
    )
    assert sent["status"] == "accepted"
    mailbox.mark_delivered("deliver-nested-message", mission_id="m",
                           message_id="nested-message")
    return ref(store.get("mail_message", "nested-message"))


def _read_all(reader: WorkingContext, *, inclusion: dict, pointer: dict) -> dict:
    args = {"mission_id": "m", "actor_id": "peer", "task_id": "task",
            "inclusion_ref": inclusion, "pointer": pointer}
    parts, offset = [], 0
    while True:
        page = reader.read_omitted_record(**args, offset=offset, limit=307)
        assert page["ref"] == pointer
        assert page["returned_chars"] <= 307
        parts.append(page["text"])
        offset = page["next_offset"]
        if offset is None:
            return json.loads("".join(parts))
        assert page["next_request"]["offset"] == offset


def test_omitted_nested_peer_payload_roundtrips_after_reopen_and_revocation(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    attachment = ref(store.get("source_copy", "copy"))

    def grant(tx):
        use = tx.put("source_use", "message-attachment-use", {
            "copy_ref": attachment, "allowed_uses": ["model_input"],
            "policy_status": "granted",
        })
        tx.bind("source_use.copy", "copy:1", "source_use", use["id"])
        return ref(use)

    store.transact("grant-message-attachment", {}, grant, mission_id="m")
    nested = {"comparison": {"claim": "SYNTHETIC_NESTED_CLAIM",
                             "rows": [{"site": number, "estimate": number % 7}
                                      for number in range(180)]},
              "attachment_refs": [attachment]}
    message_ref = _saved_message(store, attachment=attachment, payload=nested)
    context = WorkingContext(store)
    pack = context.assemble(call, task, allowance=4000, estimate_tokens=len)
    inclusion = bind_no_source_pack(store, context, pack, "nested-message-inclusion")
    intent = store.get("model_request_intent", "model-intent:call:1")
    raw = store.read_blob(intent["body"]["blob"]["sha256"])
    assert hashlib.sha256(raw).hexdigest() == intent["body"]["request_sha256"]
    index = json.loads(json.loads(raw)["messages"][-1]["content"])
    assert nested["comparison"]["claim"] not in canonical(index)
    assert message_ref in [item["ref"] for item in pack.omitted]

    reopened = StateStore(tmp_path)
    reader = WorkingContext(reopened)
    page = reader.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task", inclusion_ref=inclusion,
        cursor=0, limit=20,
    )
    card = next(item for item in page["items"] if item["ref"] == message_ref)
    assert card["summary"]["payload_status"] == "available"
    assert card["summary"]["attachment_count"] == 1
    assert card["read_request"] == {
        "tool": "read_context_record", "inclusion_id": inclusion["id"],
        **message_ref, "offset": 0, "limit": 2000,
    }
    projected = _read_all(reader, inclusion=inclusion, pointer=message_ref)
    assert projected["payload"] == nested
    assert projected["evidence_refs"] == [attachment]
    assert projected["request_id"] == "nested-request"
    assert reopened.get(**message_ref)["body"]["payload"] == nested

    for override in ({"actor_id": "sender"}, {"task_id": "foreign-task"},
                     {"mission_id": "foreign-mission"},
                     {"pointer": {**message_ref, "version": message_ref["version"] + 1}}):
        with pytest.raises(ContextError):
            reader.read_omitted_record(**{
                "mission_id": "m", "actor_id": "peer", "task_id": "task",
                "inclusion_ref": inclusion, "pointer": message_ref,
                "offset": 0, "limit": 2000, **override,
            })

    use = reopened.get("source_use", "message-attachment-use")
    reopened.transact("revoke-message-attachment", {}, lambda tx: tx.put(
        "source_use", use["id"],
        {**use["body"], "allowed_uses": [], "policy_status": "revoked"},
        expected_version=use["version"],
    ), mission_id="m")
    restricted = _read_all(reader, inclusion=inclusion, pointer=message_ref)
    assert restricted["status"] == "data_unavailable_due_source_use"
    assert restricted["unavailable_source_refs"] == [attachment]
    assert "payload" not in restricted
    assert "SYNTHETIC_NESTED_CLAIM" not in canonical(restricted)
    assert reopened.get(**message_ref)["body"]["payload"] == nested
    restricted_card = next(item for item in reader.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task", inclusion_ref=inclusion,
        cursor=0, limit=20,
    )["items"] if item["ref"] == message_ref)
    assert restricted_card["summary"]["payload_status"] == "data_unavailable_due_source_use"
    assert "read_request" not in restricted_card
    # A fresh, roomy input must recheck the same attachment instead of
    # inlining the now restricted peer body from saved message history.
    previous_call = reopened.get(**call)
    revoked_call = reopened.transact("new-call-after-revocation", {}, lambda tx: ref(
        tx.put("agent_call", "call-after-revocation", previous_call["body"])
    ), mission_id="m")
    after_revoke = reader.assemble(
        revoked_call, task, allowance=20_000, estimate_tokens=len,
    )
    assert message_ref in [item["ref"] for item in after_revoke.omitted]
    assert nested["comparison"]["claim"] not in canonical(after_revoke.messages)


@pytest.mark.parametrize("attachment_state", [
    "no_policy", "discovery_only", "missing", "unsupported",
    "scalar", "nonref", "not_list",
])
def test_unlisted_nested_attachment_never_leaks_without_model_use(
    tmp_path, attachment_state,
) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    attachment = ref(store.get("source_copy", "copy"))
    if attachment_state == "discovery_only":
        def grant_discovery(tx):
            use = tx.put("source_use", "discovery-only-use", {
                "copy_ref": attachment, "allowed_uses": ["discovery"],
                "policy_status": "granted",
            })
            tx.bind("source_use.copy", "copy:1", "source_use", use["id"])
            return ref(use)
        store.transact("grant-discovery-only", {}, grant_discovery, mission_id="m")
    elif attachment_state == "missing":
        attachment = {**attachment, "id": "missing-copy"}
    elif attachment_state == "unsupported":
        attachment = {"kind": "private_record", "id": "private-id", "version": 1}
    attachment_refs: object = [attachment]
    if attachment_state == "scalar":
        attachment_refs = ["untyped-private-id"]
    elif attachment_state == "nonref":
        attachment_refs = [{"id": "untyped-private-id"}]
    elif attachment_state == "not_list":
        attachment_refs = attachment
    _saved_message(store, attachment=None,
                   payload={"attachment_refs": attachment_refs,
                            "claim": "SYNTHETIC_UNPERMITTED_CLAIM" * 300})
    context = WorkingContext(store)
    pack = context.assemble(call, task, allowance=4000, estimate_tokens=len)
    inclusion = bind_no_source_pack(store, context, pack, "unpermitted-message")
    pointer = ref(store.get("mail_message", "nested-message"))
    result = _read_all(context, inclusion=inclusion, pointer=pointer)
    assert result["status"] == "data_unavailable_due_source_use"
    assert "SYNTHETIC_UNPERMITTED_CLAIM" not in canonical(result)
    sent = canonical(pack.messages)
    assert "SYNTHETIC_UNPERMITTED_CLAIM" not in sent
    assert "read_request" not in next(item for item in context.read_omitted_page(
        mission_id="m", actor_id="peer", task_id="task", inclusion_ref=inclusion,
        cursor=0, limit=20,
    )["items"] if item["ref"] == pointer)


@pytest.mark.parametrize("derived_kind", ["report", "working_note"])
def test_source_derived_attachment_requires_valid_lineage(tmp_path, derived_kind) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    source = ref(store.get("source_copy", "copy"))

    def save_attachment(tx):
        if derived_kind == "report":
            record = tx.put("report", "unverified-report", {
                "mission_id": "m", "title": "Synthetic report",
                "body": "Source-derived synthetic assessment", "citations": ["source"],
                "citation_input_provenance": [], "limitations": [],
            })
        else:
            record = tx.put("working_note", "unverified-note", {
                "mission_id": "m", "actor_id": "sender", "task_id": task["id"],
                "category": "analysis", "text": "Source-derived synthetic note",
                "input_refs": [source],
            })
        return ref(record)

    attachment = store.transact("save-derived-attachment", {}, save_attachment,
                                mission_id="m")
    _saved_message(store, attachment=None,
                   payload={"attachment_refs": [attachment], "claim": "DERIVED_CLAIM" * 500})
    context = WorkingContext(store)
    pack = context.assemble(call, task, allowance=4000, estimate_tokens=len)
    inclusion = bind_no_source_pack(store, context, pack, "derived-message")
    pointer = ref(store.get("mail_message", "nested-message"))
    result = _read_all(context, inclusion=inclusion, pointer=pointer)
    assert result["status"] == "data_unavailable_due_source_use"
    assert "DERIVED_CLAIM" not in canonical(result)
    assert "DERIVED_CLAIM" not in canonical(pack.messages)


def test_scientific_kind_id_version_data_is_preserved(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    nested = {"comparison": {
        "kind": "analysis_group", "id": "cohort-a", "version": 1,
        "rows": [{"kind": "observation", "id": str(n), "version": 1,
                  "estimate": n % 7} for n in range(180)],
    }, "metadata": {"kind": ["scientific", "classification"],
                    "id": "ordinary-data", "version": 1}}
    message_ref = _saved_message(store, attachment=None, payload=nested)
    context = WorkingContext(store)
    pack = context.assemble(call, task, allowance=4000, estimate_tokens=len)
    inclusion = bind_no_source_pack(store, context, pack, "scientific-ref-shaped-data")
    assert _read_all(context, inclusion=inclusion, pointer=message_ref)["payload"] == nested


def test_same_mission_hit_attachment_is_navigation_only(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    hit_ref = store.transact("save-hit-pointer", {}, lambda tx: ref(tx.put(
        "retrieval_hit", "synthetic-hit", {
            "mission_id": "m", "title": "Synthetic hit",
            "snippet": "HIT_SNIPPET_NOT_READ_BY_MESSAGE_READER",
        }
    )), mission_id="m")
    payload = {"hit_ref": hit_ref, "comparison": "PEER_AUTHORED_CLAIM" * 300}
    message_ref = _saved_message(store, attachment=hit_ref, payload=payload)
    context = WorkingContext(store)
    pack = context.assemble(call, task, allowance=4000, estimate_tokens=len)
    inclusion = bind_no_source_pack(store, context, pack, "hit-navigation-message")
    projected = _read_all(context, inclusion=inclusion, pointer=message_ref)
    assert projected["payload"] == payload
    assert projected["evidence_refs"] == [hit_ref]
    assert "HIT_SNIPPET_NOT_READ_BY_MESSAGE_READER" not in canonical(projected)


@pytest.mark.parametrize("hit_mission", ["another-mission", None])
def test_foreign_hit_pointer_cannot_authorize_peer_payload(tmp_path, hit_mission) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    foreign_hit = store.transact("save-foreign-hit", {}, lambda tx: ref(tx.put(
        "retrieval_hit", "foreign-hit", {"mission_id": hit_mission}
    )), mission_id=hit_mission or "")
    message_ref = _saved_message(
        store, attachment=None,
        payload={"attachment_refs": [foreign_hit], "claim": "FOREIGN_HIT_CLAIM" * 300},
    )
    context = WorkingContext(store)
    pack = context.assemble(call, task, allowance=4000, estimate_tokens=len)
    inclusion = bind_no_source_pack(store, context, pack, "foreign-hit-message")
    result = _read_all(context, inclusion=inclusion, pointer=message_ref)
    assert result["status"] == "data_unavailable_due_source_use"
    assert "FOREIGN_HIT_CLAIM" not in canonical(result)
    assert "FOREIGN_HIT_CLAIM" not in canonical(pack.messages)
