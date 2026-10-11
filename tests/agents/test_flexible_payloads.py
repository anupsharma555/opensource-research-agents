"""Prove nested scientific tool payloads survive one saved call without widening grants."""

from __future__ import annotations

from pathlib import Path

import pytest

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
from ora.agents.capabilities import content_hash
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]


def _payload_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "artifact": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "minLength": 1},
                    "id": {"type": "string", "minLength": 1},
                    "version": {"type": "integer", "minimum": 1},
                    "sha256": {"type": "string", "minLength": 64, "maxLength": 64},
                },
                "required": ["kind", "id", "version", "sha256"],
                "additionalProperties": False,
            },
            "observations": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "estimate": {"anyOf": [
                            {"type": "number", "minimum": -1, "maximum": 1},
                            {"type": "null"},
                        ]},
                        "note": {"type": ["string", "null"]},
                    },
                    "required": ["estimate"],
                    "additionalProperties": {"type": ["number", "null"]},
                },
            },
            "relation": {"enum": ["supports", "challenges", "context"]},
            "mode": {"const": "preserve"},
            "optional_text": {"type": "string"},
        },
        "required": ["artifact", "observations", "mode"],
        "additionalProperties": False,
    }


def test_nested_payload_and_settings_persist_while_bad_sibling_is_scoped(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    actor = AgentRegistry(store, ROOT).register(
        "agent", mission_id="mission", run_id="run", role_id="analysis", display_name="Analyst",
    )
    source_body = {"mission_id": "mission", "text": "Synthetic source"}
    source = store.transact(
        "source", source_body,
        lambda tx: tx.put("artifact", "synthetic-artifact", source_body),
        mission_id="mission",
    )
    artifact_ref = {
        "kind": source["kind"], "id": source["id"], "version": source["version"],
        "sha256": content_hash(source_body),
    }
    called: list[dict] = []

    def handler(arguments: dict) -> dict:
        called.append(arguments)
        return {"accepted": arguments, "count": len(arguments["observations"])}

    tool = Capability(
        "review_observations", "1", _payload_schema(), "specialist:analysis", handler,
        implementation_ref="build:synthetic-analysis-v1",
    )
    catalog = CapabilityCatalog.with_common([tool])
    registry = ConfigurationRegistry(store, ROOT, catalog)
    settings = {
        "sampling": {"temperatures": [0.25, 0.5], "stop": None},
        "response_format": {"type": "json", "strict": True},
    }
    reasoning = {"effort": "high", "budget": {"tokens": 4096, "fraction": 0.4}}
    config = registry.save_configuration(
        "config", instance_id=actor["id"], mission_id="mission", task_scope="synthetic review",
        provider="example", endpoint="https://models.example.test/v1", model_id="model",
        settings=settings, reasoning_settings=reasoning, instructions="Review exact evidence.",
        skill_choices={}, grants={f"common:{name}" for name in COMMON_TOOL_IDS}
        | {"specialist:analysis"}, input_refs=[artifact_ref],
    )
    call = registry.start_call(
        "call", call_id="call", configuration_id=config["id"], mission_id="mission",
        task_id="task", attempt_id="attempt", loaded_skill_ids=[],
        exposed_tool_ids=sorted(COMMON_TOOL_IDS | {"review_observations"}),
    )
    assert call["body"]["settings"] == settings
    assert call["body"]["reasoning_settings"] == reasoning
    arguments = {
        "artifact": artifact_ref,
        "observations": [
            {"estimate": 0.375, "note": None, "interval_width": 0.12},
            {"estimate": None, "note": "", "interval_width": None},
        ],
        "relation": "context", "mode": "preserve", "optional_text": "",
    }
    first = run_local_tool(
        store, catalog, "valid", mission_id="mission", call_id="call", manifest_version=1,
        tool_id="review_observations", arguments=arguments,
    )
    assert first["body"]["status"] == "completed"
    assert first["body"]["manifest_version"] == 1
    assert first["body"]["arguments"] == arguments
    assert first["body"]["result"]["accepted"] == arguments
    assert run_local_tool(
        store, catalog, "valid", mission_id="mission", call_id="call", manifest_version=1,
        tool_id="review_observations", arguments=arguments,
    )["id"] == first["id"]

    invalid = {**arguments, "artifact": {"kind": "artifact", "id": "missing-version"}}
    rejected = run_local_tool(
        store, catalog, "invalid", mission_id="mission", call_id="call", manifest_version=1,
        tool_id="review_observations", arguments=invalid,
    )
    assert rejected["body"]["status"] == "rejected"
    assert rejected["body"]["arguments"] is None
    assert len(called) == 1
    for suffix, changed in (
        ("enum", {"relation": "invented"}),
        ("const", {"mode": "overwrite"}),
        ("range", {"observations": [{"estimate": 1.5}]}),
    ):
        rejected_branch = run_local_tool(
            store, catalog, f"invalid-{suffix}", mission_id="mission", call_id="call",
            manifest_version=1, tool_id="review_observations",
            arguments={**arguments, **changed},
        )
        assert rejected_branch["body"]["status"] == "rejected"
        assert len(called) == 1
    second = run_local_tool(
        store, catalog, "valid-sibling", mission_id="mission", call_id="call", manifest_version=1,
        tool_id="review_observations", arguments={**arguments, "relation": "supports"},
    )
    assert second["body"]["status"] == "completed"
    assert len(called) == 2
    assert len(store.records("capability_invocation")) == 6

    with pytest.raises(CapabilityError, match="finite"):
        run_local_tool(
            store, catalog, "not-json", mission_id="mission", call_id="call",
            manifest_version=1, tool_id="review_observations",
            arguments={**arguments, "observations": [{"estimate": float("nan")}]},
        )
    with pytest.raises(ConfigurationError, match="nonsecret"):
        registry.save_configuration(
            "nested-secret", instance_id=actor["id"], mission_id="mission",
            task_scope="synthetic review", provider="example",
            endpoint="https://models.example.test/v1", model_id="model",
            settings={"response_format": {"apiKey": "synthetic-only"}},
            reasoning_settings={}, instructions="Review.", skill_choices={},
            grants={f"common:{name}" for name in COMMON_TOOL_IDS}, input_refs=[],
        )
    with pytest.raises(ConfigurationError, match="finite"):
        registry.save_configuration(
            "nested-nan", instance_id=actor["id"], mission_id="mission",
            task_scope="synthetic review", provider="example",
            endpoint="https://models.example.test/v1", model_id="model",
            settings={"sampling": [float("inf")]}, reasoning_settings={},
            instructions="Review.", skill_choices={},
            grants={f"common:{name}" for name in COMMON_TOOL_IDS}, input_refs=[],
        )


def test_unsupported_schema_rejects_only_its_registration() -> None:
    valid = CapabilityCatalog.with_common()
    with pytest.raises(CapabilityError, match="supported"):
        CapabilityCatalog.with_common([
            Capability(
                "unsupported", "1",
                {"type": "object", "properties": {"x": {"type": "string", "pattern": ".*"}}},
                "specialist:unsupported",
            ),
        ])
    assert "board_read" in valid.tools


def test_schema_explanations_do_not_change_argument_validation() -> None:
    from ora.agents.capabilities import validate_arguments

    schema = {
        "type": "object", "title": "Saved source selection",
        "description": "Choose a saved source, not a publication identifier.",
        "properties": {"source_id": {"type": "string", "minLength": 1,
                                      "description": "ID returned by source discovery"}},
        "required": ["source_id"], "additionalProperties": False,
    }
    catalog = CapabilityCatalog.with_common([
        Capability("inspect_source", "1", schema, "specialist:read",
                   description="Inspect one saved source"),
    ])
    validate_arguments(catalog.tools["inspect_source"].schema, {"source_id": "saved-1"})
    with pytest.raises(CapabilityError):
        validate_arguments(schema, {"source_id": 4})
    with pytest.raises(CapabilityError):
        CapabilityCatalog.with_common([
            Capability("invalid_annotation", "1", {**schema, "description": 1}, "read"),
        ])
