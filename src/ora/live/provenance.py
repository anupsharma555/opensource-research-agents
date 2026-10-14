"""Bridge local live calls to immutable shared agent and configuration records.

The live tables remain the operational ledger. Shared records in the same SQLite
database pin the actor and effective model inputs before a provider is contacted.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ora.agents import (
    COMMON_TOOL_IDS,
    AgentRegistry,
    Capability,
    CapabilityCatalog,
    ConfigurationError,
    ConfigurationRegistry,
)
from ora.agents.capabilities import content_hash, validate_arguments
from ora.state import StateStore


class LiveProvenance:
    def __init__(self, root: Path, state_dir: Path, tools: list[dict[str, Any]]) -> None:
        implementation = "sha256:" + hashlib.sha256(
            (Path(__file__).parent / "engine.py").read_bytes()
        ).hexdigest()
        self.store = StateStore(
            state_dir,
            database_name="research.sqlite3",
            record_context={
                "proof_basis": "live_model_on_supplied_evidence",
                "timing_basis": "observed_local",
                "implementation_ref": implementation,
            },
        )
        self.identities = AgentRegistry(self.store, root)
        capabilities = [
            Capability(
                tool_id=tool["function"]["name"],
                version="1",
                schema=tool["function"]["parameters"],
                permission=f"live:{tool['function']['name']}",
                side_effects=(
                    "none" if tool["function"]["name"] == "source_read" else "local_state"
                ),
                implementation_ref=implementation,
            )
            for tool in tools
        ]
        self.catalog = CapabilityCatalog.with_common(capabilities)
        self.configurations = ConfigurationRegistry(
            self.store,
            root,
            self.catalog,
            available_skill_capabilities={"source_read_by_id", "study_appraisal"},
        )
        self.tool_schemas = {
            tool["function"]["name"]: tool["function"]["parameters"] for tool in tools
        }

    def prepare_call(
        self,
        *,
        call_id: str,
        mission_id: str,
        origin_run_id: str,
        run_id: str,
        task_id: str,
        attempt_id: str,
        agent_id: str,
        display_name: str,
        role_id: str,
        messages: list[dict[str, Any]],
        provider: Any,
        tools: list[dict[str, Any]],
        loaded_skills: dict[str, dict[str, str]],
    ) -> tuple[str, str, int, dict[str, Any]]:
        """Save actor/config/manifest before dispatch; retries reuse the actor."""
        actor = self.identities.register(
            f"live-actor:{mission_id}:{agent_id}",
            mission_id=mission_id,
            run_id=origin_run_id,
            role_id=role_id,
            display_name=display_name,
        )
        # The role's saved registry references, not a model claim of skill use,
        # identify which instruction packages are loaded in the system message.
        role = self.configurations._actor_role(actor["id"], mission_id)
        if role != role_id:
            raise ValueError("Live actor role changed")
        from ora.skills.registry import load_registry

        registry = load_registry(
            self.configurations.project_root, {"source_read_by_id", "study_appraisal"}
        )
        skill_choices = {
            ref["skill_id"]: "concise" for ref in registry.roles[role_id]["skill_refs"]
            if ref["skill_id"] != "task-planning"  # MissionService-only optional capability.
        }
        for skill_id in skill_choices:
            current = registry.skills[skill_id]
            loaded = loaded_skills[skill_id]
            if (
                registry.render_instructions(skill_id) != loaded["instructions"]
                or content_hash(current.manifest) != loaded["manifest_sha256"]
            ):
                raise ConfigurationError(
                    "Loaded skill content changed before call configuration was saved"
                )
        instructions = "\n\n".join(
            message["content"]
            for message in messages
            if message.get("role") in {"system", "developer"}
            and isinstance(message.get("content"), str)
        )
        if not instructions:
            raise ValueError("Live call is missing its instruction message")
        effective = getattr(provider, "effective_configuration", None)
        if callable(effective):
            config = effective(messages, tools)
        else:
            # Custom/test providers do not imply a Groq endpoint or settings.
            config = {
                "provider": "unknown_custom",
                "endpoint": None,
                "model_id": getattr(provider, "model", "unknown"),
                "settings": {"availability": "unknown"},
                "reasoning_settings": {"availability": "unknown"},
            }
        if not isinstance(config, dict):
            raise ValueError("Provider effective configuration must be an object")
        input_body = {
            "schema": "ora.live.call_input.v1",
            "run_id": run_id,
            "call_id": call_id,
            "messages_ref": f"calls:{call_id}.messages",
            "messages_sha256": content_hash(messages),
            "tools_sha256": content_hash(tools),
        }
        saved_input = self.store.transact(
            f"live-input:{call_id}",
            input_body,
            lambda tx: tx.put(
                "live_call_input", f"input-{call_id}", input_body, expected_version=0
            ),
            mission_id=mission_id,
        )
        input_ref = {
            "kind": saved_input["kind"],
            "id": saved_input["id"],
            "version": saved_input["version"],
            "sha256": content_hash(saved_input["body"]),
        }
        grants = {f"common:{name}" for name in COMMON_TOOL_IDS}
        grants.update(f"live:{name}" for name in self.tool_schemas)
        grants.update(f"skill:{name}" for name in skill_choices)
        saved = self.configurations.save_configuration(
            f"live-config:{call_id}",
            instance_id=actor["id"],
            mission_id=mission_id,
            task_scope=f"live run {run_id} task {task_id}",
            provider=config["provider"],
            endpoint=config["endpoint"],
            model_id=config["model_id"],
            settings=config["settings"],
            reasoning_settings=config["reasoning_settings"],
            instructions=instructions,
            skill_choices=skill_choices,
            grants=grants,
            input_refs=[input_ref],
        )
        for skill_id in skill_choices:
            snapshot = saved["body"]["skills"][skill_id]
            loaded = loaded_skills[skill_id]
            if (
                snapshot["instructions"] != loaded["instructions"]
                or snapshot["manifest_sha256"] != loaded["manifest_sha256"]
            ):
                raise ConfigurationError(
                    "Saved skill content differs from instructions loaded for this call"
                )
        manifest = self.configurations.start_call(
            f"live-manifest:{call_id}",
            call_id=call_id,
            configuration_id=saved["id"],
            mission_id=mission_id,
            task_id=task_id,
            attempt_id=attempt_id,
            loaded_skill_ids=list(skill_choices),
            exposed_tool_ids=list(self.tool_schemas),
            require_common_tools=False,
        )
        if manifest["body"]["configuration_id"] != saved["id"]:
            raise ValueError("Call manifest does not reference its saved configuration")
        return actor["id"], saved["id"], saved["version"], config

    def validate_tool(self, name: str, arguments: dict[str, Any]) -> None:
        schema = self.tool_schemas.get(name)
        if schema is None:
            raise ValueError("Unknown tool")
        validate_arguments(schema, arguments)
