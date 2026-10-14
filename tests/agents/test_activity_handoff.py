"""Link real local receipts and saved outputs to one actor, then hand off context."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ora.agents import (
    COMMON_TOOL_IDS,
    AgentIdentityError,
    AgentRegistry,
    CapabilityCatalog,
    ConfigurationError,
    ConfigurationRegistry,
    run_local_tool,
)
from ora.agents.capabilities import content_hash
from ora.contracts.models import EvidencePacket
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def test_actual_receipts_outputs_and_unfinished_task_follow_replacement(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    identities = AgentRegistry(store, ROOT)
    actor = identities.register(
        "old", mission_id="m", run_id="r1", role_id="evidence_preparation_synthesis",
        display_name="Sage",
    )
    example = json.loads(
        (ROOT / "runtime_skills" / "evidence_linking" / "example.json").read_text()
    )
    evidence = EvidencePacket.model_validate(example["input"]["evidence"])
    packet_body = evidence.model_dump(mode="json")
    packet = store.transact(
        "packet", packet_body,
        lambda tx: tx.put("evidence_packet", evidence.id, packet_body), mission_id="m",
    )
    packet_ref = {**_ref(packet), "sha256": content_hash(packet_body)}
    catalog = CapabilityCatalog.with_common()
    configurations = ConfigurationRegistry(
        store, ROOT, catalog, available_skill_capabilities={"source_read_by_id"},
    )
    config = configurations.save_configuration(
        "config", instance_id=actor["id"], mission_id="m", task_scope="synthetic support",
        provider="example", endpoint="https://models.example.test/v1", model_id="model",
        settings={"response_format": {"type": "json", "strict": True}},
        reasoning_settings={"effort": "high"}, instructions="Use saved evidence.",
        skill_choices={"evidence-linking": "concise"},
        grants={f"common:{name}" for name in COMMON_TOOL_IDS} | {"skill:evidence-linking"},
        input_refs=[packet_ref],
    )
    skill_snapshot = config["body"]["skills"]["evidence-linking"]
    assert skill_snapshot["value_test_plan_ref"]["status"] == "intended_not_evaluated"
    assert skill_snapshot["value_test_result_ref"] is None
    assert config["body"]["qualification"]["evidence-linking"]["status"] == "provisional"
    call = configurations.start_call(
        "call-op", call_id="call", configuration_id=config["id"], mission_id="m",
        task_id="unfinished-task", attempt_id="attempt-one",
        loaded_skill_ids=["evidence-linking"], exposed_tool_ids=sorted(COMMON_TOOL_IDS),
    )
    tool = run_local_tool(
        store, catalog, "discover", mission_id="m", call_id="call", manifest_version=1,
        tool_id="discover_capabilities", arguments={},
    )
    assert tool["body"]["status"] == "completed"
    assert tool["body"]["manifest_version"] == call["version"]
    args = {
        "claim": example["input"]["claim"],
        "locator": example["input"]["locator"],
        "relation": "supports",
    }
    skill = configurations.invoke_skill(
        "use-skill", mission_id="m", call_id="call", manifest_version=1,
        skill_id="evidence-linking", evidence_ref=packet_ref, arguments=args,
    )
    assert skill["body"]["status"] == "completed"

    owner = {"kind": "agent_instance", "id": actor["id"], "version": 1}
    def output(kind: str) -> dict:
        body = {
            "mission_id": "m", "task_id": "unfinished-task", "attempt_id": "attempt-one",
            "actor_ref": owner, "synthetic": True, "skill_result_ref": _ref(skill),
        }
        return store.transact(
            f"save-{kind}", body, lambda tx: tx.put(kind, f"synthetic-{kind}", body),
            mission_id="m",
        )

    claim, artifact = output("claim"), output("artifact")
    for target in (tool, skill, claim, artifact):
        activity = identities.record_activity(
            f"link-{target['kind']}", mission_id="m", instance_id=actor["id"],
            target_kind=target["kind"], target_id=target["id"],
            target_version=target["version"], task_id="unfinished-task",
            attempt_id="attempt-one",
            call_id="call" if target in (tool, skill) else None,
        )
        assert {link["relation"] for link in store.links(activity)} == {
            "performed_by", "describes",
        }
        assert activity["body"]["instance_id"] == actor["id"]

    task_body = {"mission_id": "m", "status": "waiting", "owner_instance_id": actor["id"]}
    task = store.transact(
        "task", task_body,
        lambda tx: tx.put("task", "unfinished-task", task_body), mission_id="m",
    )
    successor = identities.register(
        "new", mission_id="m", run_id="r2", role_id="evidence_preparation_synthesis",
        display_name="Sage 2", predecessor_id=actor["id"],
    )
    context = [_ref(item) for item in (tool, skill, claim, artifact)]
    handoff = identities.record_handoff(
        "handoff", mission_id="m", successor_id=successor["id"],
        task_ref=_ref(task), context_refs=context,
    )
    assert handoff["body"]["predecessor_ref"] == owner
    assert handoff["body"]["successor_ref"] == _ref(successor)
    assert handoff["body"]["context_refs"] == context
    assert handoff["body"]["task_control_changed"] is False
    assert {link["relation"] for link in store.links(handoff)} == {
        "from_instance", "to_instance", "about_task", "context",
    }
    assert identities.record_handoff(
        "handoff", mission_id="m", successor_id=successor["id"],
        task_ref=_ref(task), context_refs=context,
    )["id"] == handoff["id"]
    assert store.get("task", "unfinished-task")["body"] == task_body
    assert StateStore(tmp_path).get(**_ref(handoff))["body"]["context_refs"] == context

    foreign = store.transact(
        "foreign", {"foreign": True},
        lambda tx: tx.put("message", "foreign", {"mission_id": "another"}),
        mission_id="another",
    )
    with pytest.raises(AgentIdentityError, match="another mission"):
        identities.record_handoff(
            "bad-handoff", mission_id="m", successor_id=successor["id"],
            task_ref=_ref(task), context_refs=[_ref(foreign)],
        )
    assert store.get(**_ref(task))["body"]["status"] == "waiting"


def test_value_test_result_is_versioned_but_does_not_qualify_skill(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    actor = AgentRegistry(store, ROOT).register(
        "actor", mission_id="m", run_id="r", role_id="evidence_preparation_synthesis",
        display_name="Sage",
    )
    catalog = CapabilityCatalog.with_common()
    registry = ConfigurationRegistry(
        store, ROOT, catalog, available_skill_capabilities={"source_read_by_id"},
    )
    test_body = {
        "skill_id": "evidence-linking", "skill_version": "1.0.3",
        "role_id": "evidence_preparation_synthesis", "model_id": "model",
        "task_scope": "synthetic support", "outcome": "mixed", "synthetic": True,
    }
    evaluated = store.transact(
        "test-result", test_body,
        lambda tx: tx.put("skill_value_test", "synthetic-evaluation", test_body),
        mission_id="m",
    )
    result_ref = {**_ref(evaluated), "sha256": content_hash(test_body)}

    def save(op: str, pointer: dict) -> dict:
        return registry.save_configuration(
            op, instance_id=actor["id"], mission_id="m", task_scope="synthetic support",
            provider="example", endpoint="https://models.example.test/v1", model_id="model",
            settings={}, reasoning_settings={}, instructions="Use evidence.",
            skill_choices={"evidence-linking": "concise"},
            grants={f"common:{name}" for name in COMMON_TOOL_IDS} | {"skill:evidence-linking"},
            input_refs=[], value_test_refs={"evidence-linking": pointer},
        )

    config = save("config", result_ref)
    assert config["body"]["skills"]["evidence-linking"]["value_test_result_ref"] == result_ref
    assert config["body"]["qualification"]["evidence-linking"]["status"] == "provisional"
    assert any(link["relation"] == "value_test_result" for link in store.links(config))
    with pytest.raises(ConfigurationError, match="does not match"):
        save("wrong-version", {**result_ref, "sha256": "0" * 64})
    wrong_version_body = {**test_body, "skill_version": "older-version"}
    older = store.transact(
        "older-evaluation", wrong_version_body,
        lambda tx: tx.put("skill_value_test", "older-evaluation", wrong_version_body),
        mission_id="m",
    )
    with pytest.raises(ConfigurationError, match="does not match"):
        save("mismatched-skill", {**_ref(older), "sha256": content_hash(wrong_version_body)})
    assert store.get(**_ref(config))["body"]["skills"]["evidence-linking"][
        "value_test_result_ref"
    ] == result_ref
