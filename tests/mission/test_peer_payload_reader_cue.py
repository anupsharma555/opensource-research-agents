"""An omitted peer payload has an exact, permission-aware cue beside its request."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.coordination.mailbox import PeerMailbox
from ora.mission.context import ContextError, WorkingContext
from ora.models.token_estimate import estimate_context_tokens, estimate_request_tokens
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack, fixture
from tests.mission.test_service import service


def _setup(store: StateStore) -> tuple[dict, dict, dict, dict, dict]:
    call, task = fixture(store)
    attachment = ref(store.get("source_copy", "copy"))

    def grant(tx):
        use = tx.put("source_use", "cue-attachment-use", {
            "copy_ref": attachment, "allowed_uses": ["model_input"],
            "policy_status": "granted",
        })
        tx.bind("source_use.copy", "copy:1", "source_use", use["id"])
        return ref(use)

    store.transact("grant-cue-attachment", {}, grant, mission_id="m")
    payload = {
        "attachment_refs": [attachment],
        "comparison": {"qualification": "UNVERIFIED_PEER_PROPOSAL",
                       "rows": [{"stratum": n, "estimate": n % 7}
                                for n in range(180)]},
    }
    mailbox = PeerMailbox(store)
    sent = mailbox.send_request(
        "send-cue-request", mission_id="m", request_id="cue-request",
        message_id="cue-message", requester_id="sender", recipient_id="peer",
        question="Please review the saved comparison.",
        expected_artifact="Synthetic review", cause_ref=task,
        payload=payload, task_ref=task, evidence_refs=[attachment],
    )
    assert sent["status"] == "accepted"
    mailbox.mark_delivered("deliver-cue-message", mission_id="m",
                           message_id="cue-message")
    return call, task, ref(store.get("mail_request", "cue-request")), \
        ref(store.get("mail_message", "cue-message")), payload


def _call(store: StateStore, original: dict, name: str, *, exposed: list[str],
          permitted: list[str]) -> dict:
    body = {**store.get(**original)["body"],
            "exposed_tools": exposed, "permitted_tools": permitted}
    return store.transact(f"create-{name}", {}, lambda tx: ref(
        tx.put("agent_call", name, body)
    ), mission_id="m")


def _serialized_index(store: StateStore, call: dict, task: dict, *, label: str) -> tuple:
    context = WorkingContext(store)
    pack = context.assemble(call, task, allowance=4000, estimate_tokens=len)
    inclusion = bind_no_source_pack(store, context, pack, label)
    intent = store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
    raw = store.read_blob(intent["body"]["blob"]["sha256"])
    assert hashlib.sha256(raw).hexdigest() == intent["body"]["request_sha256"]
    packet = json.loads(raw)
    return context, pack, inclusion, json.loads(packet["messages"][-1]["content"]), packet


def test_omitted_peer_cue_uses_delivered_version_and_recovers_after_reopen(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task, request_ref, message_ref, payload = _setup(store)
    context, pack, inclusion, index, packet = _serialized_index(
        store, call, task, label="cue-original",
    )
    assert request_ref in [item["ref"] for item in pack.omitted]
    assert message_ref in [item["ref"] for item in pack.omitted]
    card = index["incoming_request"]
    assert card["ref"] == request_ref
    assert card["question"] == "Please review the saved comparison."
    cue = card["peer_payload"]
    assert cue["status"] == "omitted_from_input"
    assert cue["authority"] == "unverified_peer_data"
    assert cue["availability"] == (
        "This peer message is saved and available to read. "
        "Its text is not included in this input."
    )
    assert cue["read_request"] == {
        "tool": "read_context_record", "inclusion_id": inclusion["id"],
        **message_ref, "offset": 0, "limit": 2000,
    }
    assert message_ref not in index.get("message_refs", [])
    assert payload["comparison"]["qualification"] not in canonical(packet)
    assert context.store.get(**message_ref)["body"]["payload"] == payload

    reopened = WorkingContext(StateStore(tmp_path))
    parts, offset = [], 0
    while True:
        window = reopened.read_omitted_record(
            mission_id="m", actor_id="peer", task_id=task["id"],
            inclusion_ref=inclusion, pointer=message_ref, offset=offset, limit=800,
        )
        parts.append(window["text"])
        offset = window["next_offset"]
        if offset is None:
            break
    assert json.loads("".join(parts))["payload"] == payload
    with pytest.raises(ContextError):
        reopened.read_omitted_record(
            mission_id="m", actor_id="sender", task_id=task["id"],
            inclusion_ref=inclusion, pointer=message_ref, offset=0, limit=800,
        )
    with pytest.raises(ContextError):
        reopened.read_omitted_record(
            mission_id="m", actor_id="peer", task_id="another-task",
            inclusion_ref=inclusion, pointer=message_ref, offset=0, limit=800,
        )
    declined = PeerMailbox(reopened.store).decline(
        "close-cue-request", mission_id="m", request_id=request_ref["id"],
        recipient_id="peer", reason="Synthetic check complete",
        expected_version=request_ref["version"],
    )
    assert declined["status"] == "accepted"
    closed_call = _call(
        reopened.store, call, "cue-closed-call", exposed=["read_context_record"],
        permitted=["read_context_record"],
    )
    _, _, _, closed_index, _ = _serialized_index(
        reopened.store, closed_call, task, label="cue-closed-request",
    )
    assert "incoming_request" not in closed_index


def test_cue_tracks_reader_attachment_and_current_source_use(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task, _, message_ref, payload = _setup(store)
    selected = _call(
        store, call, "cue-selected-call", exposed=["request_capability"],
        permitted=["request_capability", "read_context_record"],
    )
    _, pack, _, index, _ = _serialized_index(
        store, selected, task, label="cue-select-reader",
    )
    assert message_ref in [item["ref"] for item in pack.omitted]
    assert index["incoming_request"]["peer_payload"]["reader_setup"] == (
        "request_capability(tool_id=read_context_record) first"
    )

    unavailable = _call(
        store, call, "cue-unavailable-call", exposed=["request_capability"],
        permitted=["request_capability"],
    )
    _, _, _, index, _ = _serialized_index(
        store, unavailable, task, label="cue-no-reader",
    )
    assert "peer_payload" not in index["incoming_request"]
    assert "available to read" not in canonical(index)

    use = store.get("source_use", "cue-attachment-use")
    store.transact("revoke-cue-source", {}, lambda tx: ref(tx.put(
        "source_use", use["id"],
        {**use["body"], "allowed_uses": [], "policy_status": "revoked"},
        expected_version=use["version"],
    )), mission_id="m")
    revoked = _call(
        store, call, "cue-revoked-call", exposed=["read_context_record"],
        permitted=["read_context_record"],
    )
    _, revoked_pack, _, revoked_index, packet = _serialized_index(
        store, revoked, task, label="cue-revoked-source",
    )
    assert message_ref in [item["ref"] for item in revoked_pack.omitted]
    assert "peer_payload" not in revoked_index["incoming_request"]
    assert "available to read" not in canonical(revoked_index)
    assert payload["comparison"]["qualification"] not in canonical(packet)


@pytest.mark.parametrize("payload", [None, {}, [], "", {"question": "Visible question?"}])
def test_routine_or_empty_peer_message_has_no_redundant_cue(tmp_path, payload) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    mailbox = PeerMailbox(store)
    sent = mailbox.send_request(
        "send-routine-request", mission_id="m", request_id="routine-request",
        message_id="routine-message", requester_id="sender", recipient_id="peer",
        question="Visible question?", expected_artifact="Synthetic reply",
        cause_ref=task, payload=payload, task_ref=task,
    )
    assert sent["status"] == "accepted"
    mailbox.mark_delivered("deliver-routine-message", mission_id="m",
                           message_id="routine-message")
    _, _, _, index, _ = _serialized_index(
        store, call, task, label="routine-cue-negative",
    )
    assert index["incoming_request"]["question"] == "Visible question?"
    assert "peer_payload" not in index["incoming_request"]


def test_fully_included_peer_body_has_no_omission_cue(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task = fixture(store)
    mailbox = PeerMailbox(store)
    sent = mailbox.send_request(
        "send-short-distinct-request", mission_id="m", request_id="distinct-request",
        message_id="distinct-message", requester_id="sender", recipient_id="peer",
        question="Visible question?", expected_artifact="Synthetic reply",
        cause_ref=task, payload={"note": "Distinct short peer observation"}, task_ref=task,
    )
    assert sent["status"] == "accepted"
    mailbox.mark_delivered("deliver-distinct-message", mission_id="m",
                           message_id="distinct-message")
    pack = WorkingContext(store).assemble(
        call, task, allowance=20_000, estimate_tokens=len,
    )
    index = json.loads(pack.messages[-1]["content"])
    assert "peer_payload" not in index["incoming_request"]
    assert any(detail.get("ref", {}).get("id") == "distinct-message"
               for detail in index["details"])


def test_cue_preserves_another_pending_request(tmp_path) -> None:
    store = StateStore(tmp_path)
    call, task, request_ref, message_ref, _ = _setup(store)
    mailbox = PeerMailbox(store)
    sent = mailbox.send_request(
        "send-second-request", mission_id="m", request_id="later-request",
        message_id="later-message", requester_id="peer", recipient_id="sender",
        question="A separate question?", expected_artifact="Synthetic reply",
        cause_ref=task, payload={"question": "A separate question?"},
        task_ref=task,
    )
    assert sent["status"] == "accepted"
    mailbox.mark_delivered("deliver-later-message", mission_id="m",
                           message_id="later-message")
    context = WorkingContext(store)
    pack = context.assemble(call, task, allowance=4500, estimate_tokens=len)
    index = json.loads(pack.messages[-1]["content"])
    assert index["incoming_request"]["ref"] == request_ref
    assert index["incoming_request"]["peer_payload"]["read_request"]["id"] == (
        message_ref["id"]
    )
    later = ref(store.get("mail_request", "later-request"))
    assert later in [item["ref"] for item in index["request_dispositions"]]


@pytest.mark.parametrize("defer_core_tools", [False, True])
def test_service_serializes_cue_under_observed_input_pressure(
    tmp_path, monkeypatch, defer_core_tools,
) -> None:
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Offline reader-cue test attempted model transport")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="cue-service-start",
            question="Compare the synthetic site counts and colleague audit; report limits.",
            evidence_text=("Synthetic count sheet. Site A: 82 observed responses among "
                           "100 invited. Site B: 86 observed responses among 100 invited. "
                           "These counts are unadjusted and lack an identity audit."),
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis"],
        ))
        recipient = host._control(run.run_id, "agent-synthesis")
        sender = host._control(run.run_id, "agent-critique")
        task = host.store.get("board_proposal", recipient["body"]["task_id"])
        attachment = recipient["body"]["source_refs"][0]
        payload = {"attachment_refs": [attachment], "comparison": {
            "claim": "UNVERIFIED_PEER_PROPOSAL",
            "rows": [{
                "stratum": n, "estimate": n % 7,
                "note": (f"Synthetic row {n} is provisional pending detailed identity "
                         "reconciliation and should not be treated as observed."),
            } for n in range(80)],
        }}
        sent = host.mailbox.send_request(
            "cue-service-send", mission_id=recipient["body"]["mission_id"],
            request_id="cue-service-request", message_id="cue-service-message",
            requester_id=sender["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"],
            question="Review the saved comparison.",
            expected_artifact="Synthetic reply", cause_ref=ref(task),
            payload=payload, task_ref=ref(task), evidence_refs=[attachment],
        )
        assert sent["status"] == "accepted"
        host.mailbox.mark_delivered("cue-service-delivery",
                                    mission_id=recipient["body"]["mission_id"],
                                    message_id="cue-service-message")
        host._update_control(host._control(run.run_id, "agent-synthesis"),
                             input_budget={
                                 "observed_limit_tokens": 8000,
                                 "target_fraction": .75,
                                 "basis": "qualification_imposed_limit",
                                 "actual_provider_observation": False,
                             })
        control = host._control(run.run_id, "agent-synthesis")
        assert host._prepare_model(control, defer_core_tools=defer_core_tools)
        call = host._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
        assert hashlib.sha256(raw).hexdigest() == intent["body"]["request_sha256"]
        packet = json.loads(raw)
        index = json.loads(packet["messages"][-1]["content"])
        message_ref = ref(host.store.get("mail_message", "cue-service-message"))
        inclusion = host.store.get("context_inclusion", f"context:{intent['id']}")
        assert message_ref in [item["ref"] for item in inclusion["body"]["omitted"]]
        cue = index["incoming_request"]["peer_payload"]
        assert cue["availability"] == (
            "This peer message is saved and available to read. "
            "Its text is not included in this input."
        )
        assert cue["read_request"] == {
            "tool": "read_context_record", "inclusion_id": inclusion["id"],
            **message_ref, "offset": 0, "limit": 2000,
        }
        attached = {tool.get("function", {}).get("name") for tool in packet["tools"]}
        if "read_context_record" in attached:
            assert "reader_setup" not in cue
        else:
            assert "request_capability" in attached
            assert cue["reader_setup"] == (
                "request_capability(tool_id=read_context_record) first"
            )
        assert payload["comparison"]["claim"] not in canonical(packet)
        # Replace the cue with the exact redundant reference/disposition fields
        # it supersedes to bound the net change under the same saved packet.
        without = deepcopy(index)
        without["incoming_request"].pop("peer_payload")
        without["incoming_request"]["recipient_id"] = recipient["body"]["actor_id"]
        without["message_refs"] = [message_ref]
        without["request_dispositions"] = [{
            "ref": ref(host.store.get("mail_request", "cue-service-request")),
            "status": "unanswered", "answer_refs": [],
            "accepted_answer_ref": None, "replacement_request_ref": None,
        }]
        baseline = estimate_context_tokens(canonical(without))
        assert estimate_context_tokens(canonical(index)) - baseline <= 101
        counterfactual = deepcopy(packet)
        counterfactual["messages"][-1]["content"] = canonical(without)
        before = estimate_request_tokens(canonical(counterfactual).encode()).tokens
        after = estimate_request_tokens(raw).tokens
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        target = budget["request_stage_token_estimate"]["admission_target_tokens"]
        assert after <= target <= 8000
        assert after - before <= 101
        (tmp_path / "packet.json").write_bytes(raw)
        (tmp_path / "proof.json").write_text(json.dumps({
            "schema": "ora.peer_payload_cue_probe.v1",
            "scope": "synthetic_normal_schedule_with_imposed_input_limit",
            "request_sha256": hashlib.sha256(raw).hexdigest(),
            "saved_intent_request_sha256": intent["body"]["request_sha256"],
            "message_ref": message_ref,
            "cue": cue,
            "reader_attached": "read_context_record" in attached,
            "request_capability_attached": "request_capability" in attached,
            "defer_core_tools": defer_core_tools,
            "message_omitted": True,
            "peer_claim_not_in_request": True,
            "full_request_estimate_counterfactual_redundant_refs": before,
            "full_request_estimate_with_cue": after,
            "full_request_estimate_delta": after - before,
            "counterfactual_note": (
                "Same saved packet with cue replaced by redundant self-recipient, "
                "message_refs, and empty request disposition; not a prior runtime replay."
            ),
        }, indent=2, sort_keys=True) + "\n")
    finally:
        host.close()
