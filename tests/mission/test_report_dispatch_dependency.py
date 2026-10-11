"""Derived report text keeps its source permission through dispatch."""

from __future__ import annotations

import json

import pytest

from ora.agents import COMMON_TOOL_IDS, AgentRegistry
from ora.agents.capabilities import content_hash
from ora.coordination.board import ref
from ora.mission.context import WorkingContext
from ora.models import SessionError
from tests.models.test_session import MODEL, ROOT, URL, setup


def _prepared_report(tmp_path, *, source_linked: bool,
                     legacy_supplied: bool = False):
    store, registry, bridge, _old_call, old_config, source_ref, captured = setup(tmp_path)
    mission_id = "mission"
    parent_mission_id = "parent-mission"
    actor = AgentRegistry(store, ROOT).register(
        "report-followup-actor", mission_id=mission_id, run_id="report-child",
        role_id="literature_acquisition", display_name="Synthetic follow-up peer",
    )
    actor_id = actor["id"]
    if source_linked:
        original = store.get(**source_ref)
        def parent_source(tx):
            encounter_ref = original["body"]["encounter"]
            if legacy_supplied:
                old_encounter = tx.get(**encounter_ref)
                encounter = tx.put("source_encounter", "legacy-supplied-encounter", {
                    **old_encounter["body"], "route": "user_supplied",
                })
                encounter_ref = ref(encounter)
            return tx.put("source_copy", original["id"], {
                **original["body"], "mission_id": parent_mission_id,
                "encounter": encounter_ref,
                **({"rights_status": "permitted"} if legacy_supplied else {}),
            }, expected_version=original["version"])

        source = store.transact(
            "parent-owned-source", {}, parent_source, mission_id=parent_mission_id,
        )
        source_ref = ref(source)
    config = registry.save_configuration(
        "report-context-config", instance_id=actor_id, mission_id=mission_id,
        task_scope="synthetic-followup", provider="groq", endpoint=URL, model_id=MODEL,
        settings=old_config["body"]["settings"],
        reasoning_settings=old_config["body"]["reasoning_settings"],
        instructions="Use the saved synthetic report as data.", skill_choices={},
        grants={f"common:{name}" for name in COMMON_TOOL_IDS}, input_refs=[],
    )

    def seed(tx):
        task = tx.put("board_proposal", "report-task", {
            "mission_id": mission_id, "status": "open",
            "objective": "Review a synthetic parent report",
        })
        if source_linked and not legacy_supplied:
            policy = tx.put("source_use", "report-use", {
                "copy_ref": source_ref, "allowed_uses": ["model_input"],
                "policy_status": "granted",
            })
            tx.bind("source_use.copy", f"{source_ref['id']}:{source_ref['version']}",
                    "source_use", policy["id"])
        parent = tx.put("mission_run", "report-parent", {
            "mission_id": parent_mission_id, "question": "What did the source show?",
            "scope": "Synthetic parent question", "source_refs": [],
        })
        report = tx.put("report", "report-parent-output", {
            "mission_id": parent_mission_id, "title": "Synthetic report",
            "body": "SYNTHETIC_REPORT_TEXT_FOR_FOLLOWUP", "citations": [],
        })
        snapshot = tx.put("research_snapshot", "report-parent-snapshot", {
            "mission_id": parent_mission_id, "inspection": {
                "acceptedOutputReferences": [ref(report)],
                "sources": ([{"copyReference": {
                    **source_ref, "sha256": content_hash(tx.get(**source_ref)["body"]),
                }}] if source_linked else []),
            },
        })
        child = tx.put("mission_run", "report-child", {
            "mission_id": mission_id, "question": "Revise the parent report?",
            "scope": "Synthetic follow-up", "source_refs": [],
            "parent_run_id": parent["id"], "parent_snapshot_id": snapshot["id"],
            "target_ref": ref(report),
        })
        control = tx.put("mission_task_control", "report-child-control", {
            "mission_id": mission_id, "run_id": child["id"],
            "actor_id": actor_id, "task_id": task["id"], "source_refs": [],
        })
        return {"task_ref": ref(task), "control_ref": ref(control)}

    saved = store.transact("seed-report-followup", {}, seed, mission_id=mission_id)
    call = registry.start_call(
        "report-context-call", call_id="report-context-call",
        configuration_id=config["id"], mission_id=mission_id,
        task_id=saved["task_ref"]["id"], attempt_id="report-context-attempt",
        loaded_skill_ids=[], exposed_tool_ids=["evidence_lookup"],
        require_common_tools=False,
    )
    context = WorkingContext(store)
    pack = context.assemble(ref(call), saved["task_ref"], control_ref=saved["control_ref"])
    assert source_ref not in pack.source_refs
    assert "SYNTHETIC_REPORT_TEXT_FOR_FOLLOWUP" in json.dumps(pack.messages)
    dependencies = context.context_source_dependency_refs(pack)
    assert dependencies == ((source_ref,) if source_linked else ())
    prepared = bridge.prepare(
        call_id=call["id"], call_version=call["version"], phase="collect",
        messages=pack.messages, sources=(),
        context_source_dependency_refs=dependencies,
    )
    context.save_inclusion("report-context-inclusion", pack, prepared.intent_ref)
    assert store.get(**prepared.intent_ref)["body"]["context_source_dependency_refs"] == list(
        dependencies
    )
    return store, bridge, prepared.intent_ref, source_ref, captured


def test_report_source_revoked_after_preparation_cannot_reach_provider(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store, bridge, intent_ref, source_ref, captured = _prepared_report(
        tmp_path, source_linked=True,
    )
    assert bridge.load(intent_ref, for_dispatch=True)

    def revoke(tx):
        use = tx.get("source_use", "report-use")
        updated = tx.put("source_use", use["id"], {
            **use["body"], "allowed_uses": [], "policy_status": "restricted",
        }, expected_version=use["version"])
        return {"policy_ref": ref(updated)}

    store.transact("revoke-report-use", {}, revoke, mission_id="mission")
    with pytest.raises(SessionError, match="current model-use grant"):
        bridge.invoke_capture(intent_ref)
    assert captured == []
    assert store.records("model_dispatch_attempt") == []
    assert store.get(**source_ref) is not None


def test_changed_parent_snapshot_cannot_authorize_wrong_hashed_source(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store, bridge, intent_ref, source_ref, captured = _prepared_report(
        tmp_path, source_linked=True,
    )

    def change_parent(tx):
        child = tx.get("mission_run", "report-child")
        report = tx.get("report", "report-parent-output")
        forged = tx.put("research_snapshot", "wrong-source-snapshot", {
            "mission_id": "parent-mission", "inspection": {
                "acceptedOutputReferences": [ref(report)],
                "sources": [{"copyReference": {**source_ref, "sha256": "0" * 64}}],
            },
        })
        updated = tx.put("mission_run", child["id"], {
            **child["body"], "parent_snapshot_id": forged["id"],
        }, expected_version=child["version"])
        return {"run_ref": ref(updated)}

    store.transact("change-parent-snapshot", {}, change_parent, mission_id="mission")
    with pytest.raises(SessionError, match="unavailable for model input"):
        bridge.invoke_capture(intent_ref)
    assert captured == []
    assert store.records("model_dispatch_attempt") == []


def test_historical_user_supplied_report_without_policy_rechecks_later_policy(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store, bridge, intent_ref, source_ref, captured = _prepared_report(
        tmp_path / "allowed", source_linked=True, legacy_supplied=True,
    )
    bridge.invoke_capture(intent_ref)
    assert len(captured) == 1
    assert store.records("model_dispatch_attempt")

    blocked, bridge, intent_ref, source_ref, captured = _prepared_report(
        tmp_path / "revoked", source_linked=True, legacy_supplied=True,
    )

    def restrict(tx):
        use = tx.put("source_use", "late-restriction", {
            "copy_ref": source_ref, "policy_status": "restricted",
            "allowed_uses": [],
        })
        tx.bind("source_use.copy", f"{source_ref['id']}:{source_ref['version']}",
                "source_use", use["id"])
        return {"policy_ref": ref(use)}

    blocked.transact("late-source-restriction", {}, restrict, mission_id="parent-mission")
    with pytest.raises(SessionError, match="current model-use grant"):
        bridge.invoke_capture(intent_ref)
    assert captured == []
    assert blocked.records("model_dispatch_attempt") == []


@pytest.mark.parametrize("source_linked", [True, False])
def test_allowed_and_source_free_report_dispatches(
    tmp_path, monkeypatch, source_linked,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store, bridge, intent_ref, _source_ref, captured = _prepared_report(
        tmp_path, source_linked=source_linked,
    )
    receipt_ref = bridge.invoke_capture(intent_ref)
    assert len(captured) == 1
    assert store.get(**receipt_ref)["body"]["request_sha256"] == store.get(**intent_ref)[
        "body"
    ]["request_sha256"]
    assert len(store.records("model_dispatch_attempt")) == 1
