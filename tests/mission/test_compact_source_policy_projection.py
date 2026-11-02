"""Tight model views can omit opaque use IDs while durable policy checks stay exact."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.coordination.board import ref
from ora.mission.context import PACKER_VERSION, ContextError, WorkingContext
from ora.state import StateStore
from ora.state.store import canonical
from tests.evidence.test_coverage import checked_copy
from tests.mission.test_context import fixture


def _complete_source_context(tmp_path):
    text = "DOI: 10.1234/coverage. Synthetic participants received exercise or comparison."
    library, saved = checked_copy(tmp_path, text.encode(), selected=[text])
    store = library.store
    call, task = fixture(store)
    copy_ref = saved["copy_ref"]

    def save(tx):
        tx.put("mission_run", "run", {
            "mission_id": "m", "question": "Compare synthetic reports",
            "scope": "Retained abstract only", "source_refs": [copy_ref],
        })
        return ref(tx.put("mission_task_control", "control", {
            "mission_id": "m", "run_id": "run", "actor_id": "peer",
            "task_id": task["id"], "source_refs": [copy_ref],
            "selected_passage_refs": {copy_ref["id"]: saved["passage_refs"]},
            "read_window_refs": [],
        }))

    control = store.transact("complete-source-control", {}, save, mission_id="m")
    return store, WorkingContext(store), call, task, copy_ref, control


def _status(pack):
    return json.loads(pack.messages[-1]["content"])["source_retrieval"][0]


def test_roomy_keeps_policy_ref_while_tight_keeps_copy_status_and_scope(tmp_path):
    store, context, call, task, copy_ref, control = _complete_source_context(tmp_path)
    policy_ref = context._source_status(store.get(**copy_ref))["current_source_use_ref"]
    roomy = context.assemble(call, task, source_refs=[copy_ref], control_ref=control)
    full = _status(roomy)
    assert full["current_source_use_ref"] == policy_ref
    assert full["selected_input_scope"] == {
        "basis": "checked_passages_in_this_call_not_saved_windows",
        "status": "complete_extracted_text", "covered_chars": 78,
        "total_chars": 78, "first_omitted_cursor": None,
        "saved_read_status": "complete_extracted_text",
    }

    tight = context.assemble(
        call, task, source_refs=[copy_ref], control_ref=control,
        allowance=1750, estimate_tokens=len,
    )
    selected = _status(tight)
    assert tight.estimated_tokens <= 1750
    assert tuple(tight.source_refs) == (copy_ref,)
    assert selected == {
        "ref": copy_ref, "status": "permitted", "selected_and_current_equal": True,
        "selected_input_scope": {
            "basis": "checked_passages_in_this_call_not_saved_windows",
            "status": "complete_extracted_text", "total_chars": 78,
        },
    }
    assert PACKER_VERSION == "ora.working_context.packer.v95"


def test_revoked_unknown_and_divergent_policy_keep_current_ref(tmp_path):
    store, context, call, task, copy_ref, control = _complete_source_context(tmp_path)
    use_ref = context._source_status(store.get(**copy_ref))["current_source_use_ref"]

    def revoke(tx):
        use = tx.get(**use_ref)
        return ref(tx.put(use["kind"], use["id"], {
            **use["body"], "allowed_uses": [],
            "policy_status": "requires_source_policy_resolution",
        }, expected_version=use["version"]))

    revoked_ref = store.transact("revoke-source-projection", {}, revoke, mission_id="m")
    restricted = _status(context.assemble(
        call, task, source_refs=[copy_ref], control_ref=control,
        allowance=1750, estimate_tokens=len,
    ))
    assert restricted["status"] == "restricted"
    assert restricted["current_source_use_ref"] == revoked_ref

    unknown_store = StateStore(tmp_path / "unknown")
    unknown_call, unknown_task = fixture(unknown_store)
    unknown_ref = {"kind": "source_copy", "id": "copy", "version": 1}
    unknown = _status(WorkingContext(unknown_store).assemble(
        unknown_call, unknown_task, source_refs=[unknown_ref],
        allowance=1600, estimate_tokens=len,
    ))
    assert unknown["status"] == "unknown"
    assert "current_source_use_ref" in unknown
    assert unknown["current_source_use_ref"] is None

    divergent_store, divergent_context, divergent_call, divergent_task, old, current = (
        _complete_source_context(tmp_path / "divergent")
    )

    def revise(tx):
        prior = tx.get(**old)
        return ref(tx.put(prior["kind"], prior["id"], {
            **prior["body"], "model_use": "restricted",
        }, expected_version=prior["version"]))

    head = divergent_store.transact("revise-copy-projection", {}, revise, mission_id="m")
    stale = _status(divergent_context.assemble(
        divergent_call, divergent_task, source_refs=[old], control_ref=current,
        allowance=1750, estimate_tokens=len,
    ))
    assert stale["ref"] == old
    assert stale["current_ref"] == head
    assert stale["status"] == "restricted"
    assert "current_source_use_ref" in stale


def test_opaque_policy_ref_remains_in_durable_source_state(tmp_path):
    store, context, call, task, copy_ref, control = _complete_source_context(tmp_path)
    policy_ref = context._source_status(store.get(**copy_ref))["current_source_use_ref"]
    packed = context.assemble(
        call, task, source_refs=[copy_ref], control_ref=control,
        allowance=1750, estimate_tokens=len,
    )
    assert "current_source_use_ref" not in _status(packed)
    assert context.require_current_source_use([copy_ref])[0]["current_source_use_ref"] == (
        policy_ref
    )
    assert store.get(**policy_ref) is not None

    passage_ref = store.get(**control)["body"]["selected_passage_refs"][copy_ref["id"]][0]
    text = store.get(**passage_ref)["body"]["text"]
    request = {"messages": [
        {"role": "developer", "content": "Synthetic instructions"},
        {"role": "user", "content": canonical({"source_packets": [{
            "id": copy_ref["id"], "version": copy_ref["version"],
            "passage_refs": [passage_ref], "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }]})},
        *packed.messages,
    ]}
    encoded = canonical(request).encode()
    blob = store.put_blob(encoded)

    def save_intent(tx):
        return ref(tx.put("model_request_intent", "model-intent:call:1", {
            "mission_id": "m", "call_ref": call,
            "configuration_ref": packed.configuration_ref,
            "source_refs": [copy_ref],
            "source_decisions": [{"copy_ref": copy_ref,
                                  "passage_refs": [passage_ref]}],
            "context_source_dependency_refs": list(
                context.context_source_dependency_refs(packed)
            ),
            "request_sha256": hashlib.sha256(encoded).hexdigest(), "blob": blob,
        }))

    intent = store.transact("compact-source-intent", {}, save_intent, mission_id="m")
    inclusion_ref = context.save_inclusion("compact-source-inclusion", packed, intent)
    inclusion = store.get(**inclusion_ref)["body"]
    assert inclusion["source_refs"] == [copy_ref]
    assert inclusion["request_sha256"] == hashlib.sha256(encoded).hexdigest()

    def revoke(tx):
        use = tx.get(**policy_ref)
        return ref(tx.put(use["kind"], use["id"], {
            **use["body"], "allowed_uses": [],
            "policy_status": "requires_source_policy_resolution",
        }, expected_version=use["version"]))

    store.transact("revoke-after-inclusion", {}, revoke, mission_id="m")
    with pytest.raises(ContextError, match="source-use policy restricts"):
        context.save_inclusion("compact-source-inclusion-after-revoke", packed, intent)
