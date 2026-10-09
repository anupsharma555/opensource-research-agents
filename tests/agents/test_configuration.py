"""Prove pinned call settings and real local fixture capability execution."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import ora.agents.configuration as configuration_module
from ora.agents import (
    COMMON_TOOL_IDS,
    AgentRegistry,
    Capability,
    CapabilityCatalog,
    CapabilityError,
    ConfigurationError,
    ConfigurationRegistry,
    run_local_tool,
)
from ora.agents.capabilities import CONTEXT_TOOL_IDS, content_hash
from ora.contracts.models import EvidencePacket
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]


def _fixture(tmp_path: Path, role_id: str = "literature_acquisition"):
    calls: list[str] = []

    def handler(name: str):
        def run(args: dict):
            calls.append(name)
            return {"route": name, "query": next(iter(args.values()))}

        return run

    tools = [
        Capability(
            "search_sources",
            "1",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            "specialist:search",
            handler("search"),
            implementation_ref="build:fixture-search-v1",
        ),
        Capability(
            "critique_claim",
            "1",
            {
                "type": "object",
                "properties": {"claim_id": {"type": "string"}},
                "required": ["claim_id"],
                "additionalProperties": False,
            },
            "specialist:critique",
            handler("critique"),
            implementation_ref="build:fixture-critique-v1",
        ),
        Capability(
            "check_data_feasibility",
            "1",
            {
                "type": "object",
                "properties": {"dataset_id": {"type": "string"}},
                "required": ["dataset_id"],
                "additionalProperties": False,
            },
            "specialist:data",
            handler("data"),
            implementation_ref="build:fixture-data-v1",
        ),
    ]
    common = {name: handler(name) for name in COMMON_TOOL_IDS - CONTEXT_TOOL_IDS}
    catalog = CapabilityCatalog.with_common(
        tools,
        common,
        implementation_refs={name: f"build:fixture-{name}-v1" for name in common},
    )
    store = StateStore(tmp_path)
    agent = AgentRegistry(store, ROOT).register(
        "agent-one",
        mission_id="mission-one",
        run_id="run-one",
        role_id=role_id,
        display_name="Worker One",
    )
    source_body = {"text": "Synthetic source"}
    source = store.transact(
        "source-one",
        source_body,
        lambda tx: tx.put("source", "source-one", source_body, expected_version=0),
        mission_id="mission-one",
    )
    ref = {
        "kind": "source",
        "id": source["id"],
        "version": source["version"],
        "sha256": content_hash(source_body),
    }
    registry = ConfigurationRegistry(
        store, ROOT, catalog, available_skill_capabilities={"source_read_by_id", "study_appraisal"}
    )
    return store, registry, catalog, agent, ref, calls


def _config(
    registry: ConfigurationRegistry,
    agent: dict,
    ref: dict,
    *,
    skill_choices: dict[str, str] | None = None,
    specialist: str = "search",
):
    grants = (
        {f"common:{name}" for name in COMMON_TOOL_IDS}
        | {f"specialist:{specialist}"}
        | {f"skill:{skill_id}" for skill_id in (skill_choices or {})}
    )
    return registry.save_configuration(
        "config-one",
        instance_id=agent["id"],
        mission_id="mission-one",
        task_scope="synthetic search",
        provider="example-provider",
        endpoint="https://models.example.test/v1",
        model_id="research-model-v1",
        settings={"max_completion_tokens": 1024},
        reasoning_settings={"effort": "high"},
        instructions="Use saved sources.",
        skill_choices=skill_choices or {},
        grants=grants,
        input_refs=[ref],
        qualification_refs={"search_sources": {"status": "provisional"}}
        if specialist == "search"
        else None,
    )


@pytest.mark.parametrize(
    "role_id,tool_id,grant,args",
    [
        ("literature_acquisition", "search_sources", "search", {"query": "synthetic question"}),
        (
            "independent_methods_reviewer_critic",
            "critique_claim",
            "critique",
            {"claim_id": "claim-one"},
        ),
        ("data_management", "check_data_feasibility", "data", {"dataset_id": "data-one"}),
    ],
)
def test_diverse_scoped_tools_execute_locally(
    tmp_path: Path, role_id: str, tool_id: str, grant: str, args: dict
) -> None:
    store, registry, catalog, agent, ref, calls = _fixture(tmp_path, role_id)
    config = _config(registry, agent, ref, specialist=grant)
    assert COMMON_TOOL_IDS.issubset(config["body"]["permitted_tools"])
    assert tool_id in config["body"]["permitted_tools"]
    assert "jevk5-0.2" not in config["body"]["permitted_tools"]
    assert config["body"]["catalog_snapshot"]["jevk5-0.2"]["local_handler_available"] is False
    call = registry.start_call(
        "call-op",
        call_id="call-one",
        configuration_id=config["id"],
        mission_id="mission-one",
        task_id="task-one",
        attempt_id="attempt-one",
        loaded_skill_ids=[],
        exposed_tool_ids=sorted(COMMON_TOOL_IDS),
    )
    assert tool_id not in call["body"]["exposed_tools"]
    denied = run_local_tool(
        store,
        catalog,
        "denied",
        mission_id="mission-one",
        call_id="call-one",
        manifest_version=1,
        tool_id=tool_id,
        arguments=args,
    )
    assert denied["body"]["status"] == "rejected"
    assert not calls
    discovery = run_local_tool(
        store,
        catalog,
        "discover",
        mission_id="mission-one",
        call_id="call-one",
        manifest_version=1,
        tool_id="discover_capabilities",
        arguments={},
    )
    assert discovery["body"]["status"] == "completed"
    assert any(
        item["tool_id"] == tool_id and not item["exposed"]
        for item in discovery["body"]["result"]["available"]
    )
    requested = run_local_tool(
        store,
        catalog,
        "request-exposure",
        mission_id="mission-one",
        call_id="call-one",
        manifest_version=1,
        tool_id="request_capability",
        arguments={"tool_id": tool_id},
    )
    assert requested["body"]["status"] == "completed"
    assert requested["body"]["result"]["manifest_version"] == 2
    expanded = store.get("agent_call", "call-one")
    assert expanded["version"] == 2
    assert expanded["body"]["model_id"] == call["body"]["model_id"]
    assert expanded["body"]["grants"] == call["body"]["grants"]
    assert tool_id not in store.get("agent_call", "call-one", version=1)["body"]["exposed_tools"]
    view = registry.discover("call-one")
    assert any(item["tool_id"] == tool_id and item["exposed"] for item in view["available"])
    assert not view["unavailable_optional"]
    done = run_local_tool(
        store,
        catalog,
        "invoke",
        mission_id="mission-one",
        call_id="call-one",
        manifest_version=2,
        tool_id=tool_id,
        arguments=args,
    )
    assert done["body"]["status"] == "completed"
    assert done["body"]["result"]["route"] == grant
    assert (
        run_local_tool(
            store,
            catalog,
            "invoke",
            mission_id="mission-one",
            call_id="call-one",
            manifest_version=2,
            tool_id=tool_id,
            arguments=args,
        )["id"]
        == done["id"]
    )
    assert calls == [grant]
    stale = run_local_tool(
        store,
        catalog,
        "stale-view",
        mission_id="mission-one",
        call_id="call-one",
        manifest_version=1,
        tool_id=tool_id,
        arguments=args,
    )
    assert stale["body"]["status"] == "rejected"
    assert calls == [grant]


def test_manifest_resume_uses_saved_values_and_flags_drift(tmp_path: Path) -> None:
    store, registry, catalog, agent, ref, _ = _fixture(tmp_path, "evidence_preparation_synthesis")
    config = _config(registry, agent, ref, skill_choices={"evidence-linking": "concise"})
    call = registry.start_call(
        "call",
        call_id="call",
        configuration_id=config["id"],
        mission_id="mission-one",
        task_id="task-one",
        attempt_id="attempt-one",
        loaded_skill_ids=["evidence-linking"],
        exposed_tool_ids=sorted(COMMON_TOOL_IDS),
    )
    assert call["body"]["model_id"] == "research-model-v1"
    assert call["body"]["reasoning_settings"] == {"effort": "high"}
    assert call["body"]["skills"]["evidence-linking"]["version"]
    fresh = ConfigurationRegistry(
        StateStore(tmp_path),
        ROOT,
        catalog,
        available_skill_capabilities={"source_read_by_id", "study_appraisal"},
    )
    resumed = fresh.resume("call", available_models={("example-provider", "research-model-v1")})
    assert resumed["status"] == "ready"
    assert resumed["call"]["body"]["configuration_sha256"] == config["body"]["configuration_sha256"]
    changed_config = registry.save_configuration(
        "updated-config",
        instance_id=agent["id"],
        mission_id="mission-one",
        task_scope="synthetic search",
        provider="example-provider",
        endpoint="https://models.example.test/v1",
        model_id="research-model-v2",
        settings={"max_completion_tokens": 1024},
        reasoning_settings={"effort": "high"},
        instructions="Use saved sources.",
        skill_choices={"evidence-linking": "concise"},
        grants={f"common:{name}" for name in COMMON_TOOL_IDS}
        | {"specialist:search", "skill:evidence-linking"},
        input_refs=[ref],
        previous_configuration_id=config["id"],
        change_reason="Explicit model qualification change",
    )
    assert changed_config["body"]["instance_id"] == agent["id"]
    assert any(
        link["relation"] == "supersedes_configuration" for link in store.links(changed_config)
    )
    assert (
        fresh.resume("call", available_models={("example-provider", "research-model-v1")})["call"][
            "body"
        ]["model_id"]
        == "research-model-v1"
    )
    new_default_also_available = fresh.resume(
        "call",
        available_models={
            ("example-provider", "research-model-v1"),
            ("example-provider", "new-default"),
        },
    )
    assert new_default_also_available["call"]["body"]["model_id"] == "research-model-v1"
    assert fresh.resume("call", available_models={("example-provider", "new-default")})[
        "drift"
    ] == ["selected_model_unavailable"]
    without_skill = ConfigurationRegistry(StateStore(tmp_path), ROOT, catalog)
    assert (
        "skill_changed:evidence-linking"
        in without_skill.resume(
            "call", available_models={("example-provider", "research-model-v1")}
        )["drift"]
    )
    changed = CapabilityCatalog.with_common(
        [
            Capability(
                "search_sources",
                "2",
                {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "specialist:search",
            )
        ]
    )
    assert (
        "tool_changed:search_sources"
        in fresh.resume(
            "call", available_models={("example-provider", "research-model-v1")}, catalog=changed
        )["drift"]
    )
    original_tool = catalog.tools["search_sources"]
    replacement_tool = replace(
        original_tool,
        handler=lambda _args: {"wrong": "implementation"},
        implementation_ref="build:fixture-search-v2",
    )
    changed_implementation = CapabilityCatalog(
        [
            replacement_tool if tool.tool_id == "search_sources" else tool
            for tool in catalog.tools.values()
        ]
    )
    assert (
        "tool_changed:search_sources"
        in fresh.resume(
            "call",
            available_models={("example-provider", "research-model-v1")},
            catalog=changed_implementation,
        )["drift"]
    )
    exposed = registry.expose_tool(
        "expose-for-drift", call_id="call", mission_id="mission-one", tool_id="search_sources"
    )
    rejected = run_local_tool(
        store,
        changed_implementation,
        "stale-handler",
        mission_id="mission-one",
        call_id="call",
        manifest_version=exposed["version"],
        tool_id="search_sources",
        arguments={"query": "synthetic"},
    )
    assert rejected["body"]["status"] == "rejected"
    with pytest.raises(CapabilityError, match="implementation ref"):
        CapabilityCatalog.with_common([replace(original_tool, implementation_ref=None)])


def test_permissions_reject_invalid_and_secret_settings(tmp_path: Path) -> None:
    store, registry, catalog, agent, ref, calls = _fixture(tmp_path)
    for key in (
        "api_key",
        "apiKey",
        "X-API-KEY",
        "ａｐｉＫｅｙ",
        "token",
        "accessToken",
        "Authorization",
        "groqKey",
        "xAuth",
        "bearer",
    ):
        with pytest.raises(ConfigurationError, match="nonsecret"):
            registry.save_configuration(
                "bad-key",
                instance_id=agent["id"],
                mission_id="mission-one",
                task_scope="synthetic search",
                provider="example",
                endpoint="https://models.example.test/v1",
                model_id="m",
                settings={key: "synthetic-sensitive-value"},
                reasoning_settings={},
                instructions="A",
                skill_choices={},
                grants={f"common:{name}" for name in COMMON_TOOL_IDS},
                input_refs=[ref],
            )
    with pytest.raises(CapabilityError):
        catalog.permitted({"specialist:search"})
    config = _config(registry, agent, ref)
    with pytest.raises(ConfigurationError):
        registry.start_call(
            "bad-call",
            call_id="bad",
            configuration_id=config["id"],
            mission_id="mission-one",
            task_id="task",
            attempt_id="attempt",
            loaded_skill_ids=[],
            exposed_tool_ids=["search_sources"],
        )
    registry.start_call(
        "call",
        call_id="call",
        configuration_id=config["id"],
        mission_id="mission-one",
        task_id="task",
        attempt_id="attempt",
        loaded_skill_ids=[],
        exposed_tool_ids=sorted(COMMON_TOOL_IDS),
    )
    bad = run_local_tool(
        store,
        catalog,
        "bad-args",
        mission_id="mission-one",
        call_id="call",
        manifest_version=1,
        tool_id="board_read",
        arguments={"unexpected": "x"},
    )
    assert bad["body"]["status"] == "rejected"
    not_granted = run_local_tool(
        store,
        catalog,
        "not-granted",
        mission_id="mission-one",
        call_id="call",
        manifest_version=1,
        tool_id="critique_claim",
        arguments={"claim_id": "c"},
    )
    assert not_granted["body"]["status"] == "rejected"
    denied_expansion = run_local_tool(
        store,
        catalog,
        "not-granted-expansion",
        mission_id="mission-one",
        call_id="call",
        manifest_version=1,
        tool_id="request_capability",
        arguments={"tool_id": "critique_claim"},
    )
    assert denied_expansion["body"]["status"] == "rejected"
    assert store.get("agent_call", "call")["version"] == 1
    assert not calls


def test_optional_classifier_is_visible_only_to_granted_call(tmp_path: Path) -> None:
    store, _, catalog, agent, ref, _ = _fixture(tmp_path)
    registry = ConfigurationRegistry(store, ROOT, catalog)
    config = registry.save_configuration(
        "classifier-config",
        instance_id=agent["id"],
        mission_id="mission-one",
        task_scope="synthetic classification",
        provider="example",
        endpoint="https://models.example.test/v1",
        model_id="research-model-v1",
        settings={},
        reasoning_settings={},
        instructions="Use permitted tools.",
        skill_choices={},
        grants={f"common:{name}" for name in COMMON_TOOL_IDS}
        | {"specialist:article_classification"},
        input_refs=[ref],
    )
    registry.start_call(
        "classifier-call",
        call_id="classifier-call",
        configuration_id=config["id"],
        mission_id="mission-one",
        task_id="task",
        attempt_id="attempt",
        loaded_skill_ids=[],
        exposed_tool_ids=sorted(COMMON_TOOL_IDS),
    )
    discovery = run_local_tool(
        store,
        catalog,
        "classifier-discovery",
        mission_id="mission-one",
        call_id="classifier-call",
        manifest_version=1,
        tool_id="discover_capabilities",
        arguments={},
    )
    assert any(
        item["tool_id"] == "jevk5-0.2"
        for item in discovery["body"]["result"]["unavailable_optional"]
    )
    denied = run_local_tool(
        store,
        catalog,
        "classifier-request",
        mission_id="mission-one",
        call_id="classifier-call",
        manifest_version=1,
        tool_id="request_capability",
        arguments={"tool_id": "jevk5-0.2"},
    )
    assert denied["body"]["status"] == "rejected"
    assert store.get("agent_call", "classifier-call")["version"] == 1


@pytest.mark.parametrize("invalid", [{"value": float("nan")}, {"value": object()}])
def test_invalid_tool_output_persists_safe_failed_receipt(tmp_path: Path, invalid: dict) -> None:
    store, _, _, agent, ref, _ = _fixture(tmp_path)
    invalid_tool = Capability(
        "fixture_invalid",
        "1",
        {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "specialist:invalid",
        lambda _args: invalid,
        implementation_ref="build:fixture-invalid-v1",
    )
    catalog = CapabilityCatalog.with_common([invalid_tool])
    registry = ConfigurationRegistry(store, ROOT, catalog)
    config = registry.save_configuration(
        "invalid-config",
        instance_id=agent["id"],
        mission_id="mission-one",
        task_scope="synthetic invalid output",
        provider="example",
        endpoint="https://models.example.test/v1",
        model_id="m1",
        settings={},
        reasoning_settings={},
        instructions="Use fixtures.",
        skill_choices={},
        grants={f"common:{name}" for name in COMMON_TOOL_IDS} | {"specialist:invalid"},
        input_refs=[ref],
    )
    registry.start_call(
        "invalid-call",
        call_id="invalid-call",
        configuration_id=config["id"],
        mission_id="mission-one",
        task_id="task",
        attempt_id="attempt",
        loaded_skill_ids=[],
        exposed_tool_ids=sorted([*COMMON_TOOL_IDS, "fixture_invalid"]),
    )
    receipt = run_local_tool(
        store,
        catalog,
        "invalid-result",
        mission_id="mission-one",
        call_id="invalid-call",
        manifest_version=1,
        tool_id="fixture_invalid",
        arguments={},
    )
    assert receipt["body"]["status"] == "failed"
    assert receipt["body"]["result"] is None
    assert receipt["body"]["result_sha256"] is None
    assert store.get("capability_invocation", receipt["id"]) == receipt


def test_all_roles_and_temporary_inherit_common_tools(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    identity = AgentRegistry(store, ROOT)
    catalog = CapabilityCatalog.with_common()
    registry = ConfigurationRegistry(store, ROOT, catalog)
    for index, role_id in enumerate([*sorted(identity.role_ids), "temporary_specialist"]):
        kind = "temporary" if role_id == "temporary_specialist" else "role"
        actor = identity.register(
            f"agent-{index}",
            mission_id="mission-all",
            run_id="run-all",
            role_id=role_id,
            kind=kind,
            display_name=f"Worker {index}",
        )
        config = registry.save_configuration(
            f"config-{index}",
            instance_id=actor["id"],
            mission_id="mission-all",
            task_scope="fixture task",
            provider="example",
            endpoint="https://models.example.test/v1",
            model_id="m1",
            settings={},
            reasoning_settings={},
            instructions="Use research evidence.",
            skill_choices={},
            grants={f"common:{tool}" for tool in COMMON_TOOL_IDS},
            input_refs=[],
        )
        call = registry.start_call(
            f"call-{index}",
            call_id=f"call-{index}",
            configuration_id=config["id"],
            mission_id="mission-all",
            task_id=f"task-{index}",
            attempt_id=f"attempt-{index}",
            loaded_skill_ids=[],
            exposed_tool_ids=sorted(COMMON_TOOL_IDS),
        )
        assert set(call["body"]["exposed_tools"]) == COMMON_TOOL_IDS
        assert all(
            item["status"] == "provisional" for item in config["body"]["qualification"].values()
        )


def test_qualification_reference_must_match_role_model_and_capability(tmp_path: Path) -> None:
    store, registry, _, agent, ref, _ = _fixture(tmp_path)
    evaluation = store.transact(
        "evaluation-one",
        {"id": "eval-one"},
        lambda tx: tx.put(
            "capability_evaluation",
            "eval-one",
            {
                "capability_id": "search_sources",
                "role_id": "literature_acquisition",
                "model_id": "research-model-v1",
                "status": "qualified",
                "task_scope": "synthetic search",
            },
            expected_version=0,
        ),
        mission_id="mission-one",
    )
    qualified = {
        "search_sources": {
            "status": "qualified",
            "evaluation_ref": {
                "kind": evaluation["kind"],
                "id": evaluation["id"],
                "version": evaluation["version"],
            },
        }
    }
    config = registry.save_configuration(
        "qualified",
        instance_id=agent["id"],
        mission_id="mission-one",
        task_scope="synthetic search",
        provider="example-provider",
        endpoint="https://models.example.test/v1",
        model_id="research-model-v1",
        settings={},
        reasoning_settings={},
        instructions="Use sources.",
        skill_choices={},
        grants={f"common:{name}" for name in COMMON_TOOL_IDS} | {"specialist:search"},
        input_refs=[ref],
        qualification_refs=qualified,
    )
    assert config["body"]["qualification"]["search_sources"]["status"] == "qualified"
    with pytest.raises(ConfigurationError, match="does not match"):
        registry.save_configuration(
            "wrong-scope",
            instance_id=agent["id"],
            mission_id="mission-one",
            task_scope="different research question",
            provider="example-provider",
            endpoint="https://models.example.test/v1",
            model_id="research-model-v1",
            settings={},
            reasoning_settings={},
            instructions="Use sources.",
            skill_choices={},
            grants={f"common:{name}" for name in COMMON_TOOL_IDS} | {"specialist:search"},
            input_refs=[ref],
            qualification_refs=qualified,
        )
    with pytest.raises(ConfigurationError, match="does not match"):
        registry.save_configuration(
            "wrong-model",
            instance_id=agent["id"],
            mission_id="mission-one",
            task_scope="synthetic search",
            provider="example-provider",
            endpoint="https://models.example.test/v1",
            model_id="different",
            settings={},
            reasoning_settings={},
            instructions="Use sources.",
            skill_choices={},
            grants={f"common:{name}" for name in COMMON_TOOL_IDS} | {"specialist:search"},
            input_refs=[ref],
            qualification_refs=qualified,
        )


def test_loaded_skill_helper_executes_against_exact_saved_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, registry, _, agent, _, _ = _fixture(tmp_path, "evidence_preparation_synthesis")
    example = json.loads(
        (ROOT / "runtime_skills" / "evidence_linking" / "example.json").read_text(encoding="utf-8")
    )
    evidence = EvidencePacket.model_validate(example["input"]["evidence"])
    body = evidence.model_dump(mode="json")
    packet = store.transact(
        "packet",
        body,
        lambda tx: tx.put("evidence_packet", evidence.id, body, expected_version=0),
        mission_id="mission-one",
    )
    ref = {
        "kind": "evidence_packet",
        "id": packet["id"],
        "version": 1,
        "sha256": content_hash(body),
    }
    with pytest.raises(ConfigurationError, match="host grant"):
        registry.save_configuration(
            "ungranted-skill",
            instance_id=agent["id"],
            mission_id="mission-one",
            task_scope="synthetic claim support",
            provider="example",
            endpoint="https://models.example.test/v1",
            model_id="m1",
            settings={},
            reasoning_settings={},
            instructions="Use saved evidence.",
            skill_choices={"evidence-linking": "concise"},
            grants={f"common:{name}" for name in COMMON_TOOL_IDS},
            input_refs=[ref],
        )
    config = registry.save_configuration(
        "skill-config",
        instance_id=agent["id"],
        mission_id="mission-one",
        task_scope="synthetic claim support",
        provider="example",
        endpoint="https://models.example.test/v1",
        model_id="m1",
        settings={},
        reasoning_settings={},
        instructions="Use saved evidence.",
        skill_choices={"evidence-linking": "concise"},
        grants={f"common:{name}" for name in COMMON_TOOL_IDS} | {"skill:evidence-linking"},
        input_refs=[ref],
    )
    registry.start_call(
        "skill-call",
        call_id="skill-call",
        configuration_id=config["id"],
        mission_id="mission-one",
        task_id="task",
        attempt_id="attempt",
        loaded_skill_ids=["evidence-linking"],
        exposed_tool_ids=sorted(COMMON_TOOL_IDS),
    )
    args = {
        "claim": example["input"]["claim"],
        "locator": example["input"]["locator"],
        "relation": "supports",
    }
    used = registry.invoke_skill(
        "use-skill",
        mission_id="mission-one",
        call_id="skill-call",
        manifest_version=1,
        skill_id="evidence-linking",
        evidence_ref=ref,
        arguments=args,
    )
    assert used["body"]["status"] == "completed"
    assert used["body"]["result"] == example["expected_helper_output"]
    assert (
        registry.invoke_skill(
            "use-skill",
            mission_id="mission-one",
            call_id="skill-call",
            manifest_version=1,
            skill_id="evidence-linking",
            evidence_ref=ref,
            arguments=args,
        )["id"]
        == used["id"]
    )
    rejected = registry.invoke_skill(
        "bad-locator",
        mission_id="mission-one",
        call_id="skill-call",
        manifest_version=1,
        skill_id="evidence-linking",
        evidence_ref=ref,
        arguments={**args, "locator": "not a real passage"},
    )
    assert rejected["body"]["status"] == "rejected"
    original_loader = configuration_module.load_registry
    for index, invalid in enumerate(({"value": float("nan")}, {"value": object()})):

        def invalid_loader(root, capabilities, output=invalid):
            current = original_loader(root, capabilities)
            skill = current.skills["evidence-linking"]
            altered = replace(skill, helper=lambda *_args, **_kwargs: output)
            return replace(current, skills={**current.skills, "evidence-linking": altered})

        monkeypatch.setattr(configuration_module, "load_registry", invalid_loader)
        invalid_receipt = registry.invoke_skill(
            f"invalid-skill-result-{index}",
            mission_id="mission-one",
            call_id="skill-call",
            manifest_version=1,
            skill_id="evidence-linking",
            evidence_ref=ref,
            arguments=args,
        )
        assert invalid_receipt["body"]["status"] == "rejected"
        assert invalid_receipt["body"]["result"] is None
        assert store.get("skill_invocation", invalid_receipt["id"]) == invalid_receipt


def test_appraisal_helper_has_its_own_pinned_skill_receipt(tmp_path: Path) -> None:
    store, registry, _, agent, _, _ = _fixture(tmp_path, "independent_methods_reviewer_critic")
    example = json.loads(
        (ROOT / "runtime_skills" / "study_appraisal" / "example.json").read_text(encoding="utf-8")
    )
    evidence = EvidencePacket.model_validate(example["input"]["evidence"])
    body = evidence.model_dump(mode="json")
    packet = store.transact(
        "appraisal-packet",
        body,
        lambda tx: tx.put("evidence_packet", evidence.id, body, expected_version=0),
        mission_id="mission-one",
    )
    ref = {
        "kind": "evidence_packet",
        "id": packet["id"],
        "version": 1,
        "sha256": content_hash(body),
    }
    config = registry.save_configuration(
        "appraisal-config",
        instance_id=agent["id"],
        mission_id="mission-one",
        task_scope="synthetic study appraisal",
        provider="example",
        endpoint="https://models.example.test/v1",
        model_id="m1",
        settings={},
        reasoning_settings={},
        instructions="Review exact result.",
        skill_choices={"study-appraisal": "concise"},
        grants={f"common:{name}" for name in COMMON_TOOL_IDS} | {"skill:study-appraisal"},
        input_refs=[ref],
    )
    registry.start_call(
        "appraisal-call-op",
        call_id="appraisal-call",
        configuration_id=config["id"],
        mission_id="mission-one",
        task_id="task",
        attempt_id="attempt",
        loaded_skill_ids=["study-appraisal"],
        exposed_tool_ids=sorted(COMMON_TOOL_IDS),
    )
    args = {key: value for key, value in example["input"].items() if key != "evidence"}
    result = registry.invoke_skill(
        "appraisal-use",
        mission_id="mission-one",
        call_id="appraisal-call",
        manifest_version=1,
        skill_id="study-appraisal",
        evidence_ref=ref,
        arguments=args,
    )
    assert result["body"]["status"] == "completed"
    assert result["body"]["result"]["scientific_validity_status"] == example["expected_status"]
