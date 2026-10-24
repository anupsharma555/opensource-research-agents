"""Host-invoked bridge from saved agent calls and source copies to one model turn.

This module owns neither admission nor tool execution. Every request is derived
from one exact agent_call and its saved agent_configuration. The host supplies
per-copy model-use decisions; peer text cannot grant source access.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from ora.agents.capabilities import content_hash
from ora.agents.configuration import (
    ConfigurationError,
    _call_manifest_body,
    _call_request,
)
from ora.coordination.traces import TraceContext, TraceJournal, ref
from ora.evidence.library import EvidenceError
from ora.evidence.local_text_quality import attest_local_text_quality_tx
from ora.retrieval.providers import HTTPDispatchBlocked
from ora.retrieval.redact import redact_text
from ora.state import StateStore
from ora.state.source_lineage import inherited_public_source
from ora.state.store import Transaction, canonical

from .groq import (
    MODEL,
    URL,
    CallConfiguration,
    GroqSelectedAdapter,
    ModelOutcome,
    ModelPreflightError,
    PreparedCall,
    SourceUse,
)
from .reasoning_artifacts import (
    export_receipt_reasoning,
    record_export_failure,
)
from .token_estimate import (
    LEGACY_BASIS,
    TOKENIZER_BASIS,
    TOKENIZER_BASIS_V1,
    TOKENIZER_BASIS_V2,
    TOKENIZER_BASIS_V3,
    TokenEstimateUnavailable,
    estimate_request_tokens,
)
from .token_measurement import measure_tokens


class SessionError(ValueError):
    """A saved selection, source copy or captured result failed its boundary."""


class DispatchUncertain(SessionError):
    """This intent was already sent or may have been sent; the host must reconcile."""


@dataclass(frozen=True)
class SourceSelection:
    """Host decision for an exact, saved source copy, never a peer payload."""

    copy_ref: dict[str, Any]
    use: Literal["permitted", "restricted", "unknown"]
    allowed_for_model: bool
    route: Literal["direct", "cache", "peer"] = "direct"
    passage_refs: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class SessionPrepared:
    intent_ref: dict[str, Any]
    request: PreparedCall


def _exact_ref(pointer: dict[str, Any], kind: str) -> None:
    if (type(pointer) is not dict or set(pointer) != {"kind", "id", "version"}
        or pointer["kind"] != kind or not isinstance(pointer["id"], str)
        or not pointer["id"] or type(pointer["version"]) is not int
        or pointer["version"] < 1):
        raise SessionError(f"Exact {kind} reference is required")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _check_visible_shared_materials(reader, intent: dict, request_body: dict) -> None:
    """Recheck source-derived peer metadata in exact saved input before send.

    This runs once during ordinary preflight and again in the dispatch-marker
    transaction. Old requests without a shared catalog retain their saved form.
    """
    from ora.mission.context import _shared_material_overview, _shared_material_reader_view
    from ora.mission.shared_materials import shared_material_entry, shared_material_view

    scope = {
        "mission_id": intent["body"]["mission_id"],
        "actor_id": intent["body"]["instance_id"],
        "task_id": intent["body"]["task_id"],
    }

    def current(pointer: object) -> dict | None:
        if (type(pointer) is not dict or set(pointer) != {"kind", "id", "version"}
                or pointer.get("kind") not in {"retrieval_hit", "report"}):
            raise SessionError("Visible shared pointer is not exact")
        return shared_material_entry(reader, **scope, pointer=pointer)

    def summary_for(entry: dict) -> dict:
        summary = {"kind": entry["ref"]["kind"]}
        summary.update({key: value for key, value in entry.items() if key != "ref"})
        title = summary.get("title")
        if isinstance(title, str):
            summary["title"] = {
                "text": title[:300], "total_chars": len(title),
                "truncated": len(title) > 300,
            }
        return summary

    messages = request_body.get("messages", [])
    for position, message in enumerate(messages):
        if message.get("role") not in {"user", "tool"}:
            continue
        try:
            content = json.loads(message.get("content") or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(content, dict):
            continue
        if content.get("schema") == "ora.working_context.v4":
            shared = content.get("shared_materials")
            overview = shared.get("overview") if isinstance(shared, dict) else None
            if isinstance(overview, dict):
                saved = current(overview.get("ref"))
                inclusion_id = f"context:{intent['id']}" if "read_request" in overview else None
                if saved is None or _shared_material_overview(saved, inclusion_id) != overview:
                    raise SessionError("Visible shared overview changed before dispatch")
            for entry in shared.get("entries", []) if isinstance(shared, dict) else []:
                saved = current(entry.get("ref")) if isinstance(entry, dict) else None
                expected = (
                    _shared_material_reader_view(saved, f"context:{intent['id']}")
                    if saved is not None and "read_request" in entry
                    else shared_material_view(saved) if saved is not None else None
                )
                if expected != entry:
                    raise SessionError("Visible shared catalog changed before dispatch")
        items = (
            content.get("items") if content.get("schema") == "ora.context_omission_page.v2"
            else content.get("included_items")
            if content.get("schema") == "ora.context_page_tool_result.v1" else None
        )
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "shared_material":
                    continue
                summary = item.get("summary")
                if not isinstance(summary, dict):
                    raise SessionError("Visible shared page summary is invalid")
                if summary.get("status") in {
                    "shared_material_unavailable", "summary_deferred_by_page_budget",
                }:
                    continue  # These contain no source-derived title or lead.
                entry = current(item.get("ref"))
                inclusion_id = (
                    content.get("inclusion_ref", {}).get("id")
                    if content.get("schema") == "ora.context_omission_page.v2"
                    else content.get("inclusion_id")
                )
                expected = (
                    _shared_material_reader_view(entry, inclusion_id)
                    if entry is not None and "read_request" in summary
                    else shared_material_view(entry) if entry is not None else None
                )
                if expected is None or summary_for(expected) != summary:
                    raise SessionError("Visible shared page changed before dispatch")
        if (content.get("schema") == "ora.context_record_window.v1"
                and isinstance(content.get("ref"), dict)
                and content["ref"].get("kind") in {"retrieval_hit", "report"}):
            pointer = content["ref"]
            if pointer["kind"] == "report":
                assistant = messages[position - 1] if position else {}
                calls = assistant.get("tool_calls", []) if isinstance(assistant, dict) else []
                try:
                    arguments = json.loads(calls[0]["function"]["arguments"])
                    inclusion = reader.get(
                        "context_inclusion", arguments["inclusion_id"], 1,
                    )
                except (IndexError, KeyError, TypeError, ValueError):
                    inclusion = None
                if inclusion is None or not any(
                    item.get("type") == "shared_material" and item.get("fetch") == pointer
                    for item in inclusion["body"].get("omitted", [])
                ):
                    continue  # Existing own-report and parent-target paths retain their guards.
            entry = current(pointer)
            if entry is None:
                raise SessionError("Visible shared record lost current authority")
            if pointer["kind"] == "retrieval_hit":
                encoded = canonical(shared_material_view(entry))
                offset, text = content.get("offset"), content.get("text")
                if (type(offset) is not int or not isinstance(text, str)
                        or content.get("projection_sha256") != _sha(encoded.encode())
                        or encoded[offset:offset + len(text)] != text):
                    raise SessionError("Visible shared hit window changed before dispatch")
            elif content.get("report_text_status") == "available" and (
                entry.get("status") != "accepted_text_available"
            ):
                raise SessionError("Visible shared report lost accepted text use")


def _explicit_prohibition(body: dict) -> bool:
    use_record = body.get("use_record")
    return bool(
        body.get("allow_model_input") is False
        or body.get("allowed_for_model") is False
        or body.get("model_use") in {"restricted", "prohibited"}
        or body.get("rights_status") in {"restricted", "not_retained"}
        or (isinstance(use_record, dict)
            and (use_record.get("allow_model_input") is False
                 or use_record.get("allowed_for_model") is False
                 or use_record.get("use") in {"restricted", "prohibited"}))
    )


def _current_model_use(reader, copy_ref: dict) -> dict | None:
    """Respect a separately versioned source policy at the actual send boundary.

    Older supplied-evidence records use the policy saved on the copy itself.
    A newer intake policy, when present, must refer to this exact copy/version.
    """
    policy = reader.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
    if policy is None:
        return None
    body = policy["body"]
    if policy["kind"] != "source_use" or body.get("copy_ref") != copy_ref:
        raise SessionError("Current source-use policy does not match the selected copy")
    if (_explicit_prohibition(body) or not isinstance(body.get("allowed_uses"), list)
            or "model_input" not in body["allowed_uses"]):
        raise SessionError("Current source-use policy revoked model input for this copy")
    return ref(policy)


def _inherited_context_copy(reader, copy_ref: dict, mission_id: str,
                            call_ref: dict) -> bool:
    """Recognize only a source in this call's exact accepted parent snapshot."""
    def matching_source(item: object) -> bool:
        pointer = item.get("copyReference") if isinstance(item, dict) else None
        if not isinstance(pointer, dict):
            return False
        if set(pointer) == {"kind", "id", "version", "sha256"}:
            exact = {key: pointer[key] for key in ("kind", "id", "version")}
            copy = reader.get(**exact) if exact["kind"] == "source_copy" else None
            if copy is None or content_hash(copy["body"]) != pointer["sha256"]:
                return False
            pointer = exact
        return set(pointer) == {"kind", "id", "version"} and pointer == copy_ref

    call = reader.get(**call_ref)
    actor = reader.get("agent_instance", call["body"].get("instance_id"), 1) if call else None
    run = reader.get("mission_run", actor["body"].get("run_id")) if actor else None
    parent_id = run["body"].get("parent_run_id") if run else None
    parent = reader.get("mission_run", parent_id) if isinstance(parent_id, str) else None
    snapshot = reader.get(
        "research_snapshot", run["body"].get("parent_snapshot_id"), 1
    ) if run else None
    target_ref = run["body"].get("target_ref") if run else None
    target = reader.get(**target_ref) if isinstance(target_ref, dict) else None
    copy = reader.get(**copy_ref)
    if copy and (copy["body"].get("document_ref") or copy["body"].get("navigation_ref")):
        return bool(
            call and actor and run
            and call["body"].get("mission_id") == mission_id
            and call["body"].get("instance_id") == actor["id"]
            and actor["body"].get("mission_id") == mission_id
            and inherited_public_source(
                reader, mission_id=mission_id, run_id=run["id"],
                copy_ref=copy_ref, actor_id=actor["id"],
                task_id=call["body"].get("task_id"),
            )
        )
    return bool(
        call and actor and run and parent and snapshot and target
        and call["body"].get("mission_id") == mission_id
        and actor["body"].get("mission_id") == mission_id
        and run["body"].get("mission_id") == mission_id
        and snapshot["body"].get("mission_id") == parent["body"].get("mission_id")
        and target["kind"] == "report"
        and target["body"].get("mission_id") == parent["body"].get("mission_id")
        and target_ref in snapshot["body"].get("inspection", {}).get(
            "acceptedOutputReferences", []
        )
        and any(matching_source(item) for item in snapshot["body"].get(
            "inspection", {}).get("sources", []))
    )


def _current_context_dependency_use(reader, copy_ref: dict, mission_id: str,
                                    call_ref: dict) -> dict | None:
    """Check a copy represented by derived context text, including explicit grant."""
    _exact_ref(copy_ref, "source_copy")
    copy = reader.get(**copy_ref)
    if (copy is None or _explicit_prohibition(copy["body"])):
        raise SessionError("Context source dependency is unavailable for model input")
    if (copy["body"].get("mission_id") not in (None, mission_id)
            and not _inherited_context_copy(reader, copy_ref, mission_id, call_ref)):
        raise SessionError("Context source dependency is unavailable for model input")
    current = reader.get("source_copy", copy_ref["id"])
    if current is not None and _explicit_prohibition(current["body"]):
        raise SessionError("Current context source copy prohibits model input")
    policy = reader.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
    if policy is None:
        encounter_ref = copy["body"].get("encounter")
        encounter = reader.get(**encounter_ref) if isinstance(encounter_ref, dict) else None
        if (
            encounter is not None
            and encounter["body"].get("route") == "user_supplied"
            and copy["body"].get("rights_status") in {None, "permitted"}
        ):
            # Historical user submissions can predate source_use records. The
            # report reader permits this exact route; a later policy record
            # removes this exception at the dispatch-time recheck.
            return None
    if policy is None or policy["body"].get("policy_status") != "granted":
        raise SessionError("Context source dependency lacks a current model-use grant")
    policy_ref = _current_model_use(reader, copy_ref)
    if policy_ref is None:
        raise SessionError("Context source dependency lacks a current model-use grant")
    return policy_ref


class ModelSession:
    """Prepare, invoke and capture one selected-model call against saved state."""

    def __init__(self, store: StateStore, project_root: Path, *,
                 adapter: GroqSelectedAdapter | None = None,
                 journal: TraceJournal | None = None) -> None:
        self.store = store
        self.adapter = adapter or GroqSelectedAdapter(project_root)
        self.journal = journal or TraceJournal(store)

    def _selection(self, call_id: str, call_version: int) -> tuple[dict, dict, dict]:
        if type(call_version) is not int or call_version < 1:
            raise SessionError("Exact call manifest version is required")
        call = self.store.get("agent_call", call_id, call_version)
        if call is None:
            raise SessionError("Saved call manifest is unavailable")
        return self._selection_for_call(call)

    def _selection_for_call(self, call: dict) -> tuple[dict, dict, dict]:
        body = call["body"]
        config = self.store.get("agent_configuration", body.get("configuration_id"), 1)
        if config is None or config["body"].get("mission_id") != body.get("mission_id"):
            raise SessionError("Saved configuration is unavailable for this call")
        saved = config["body"]
        actual_hash = content_hash({key: value for key, value in saved.items()
                                    if key != "configuration_sha256"})
        if (saved.get("configuration_sha256") != actual_hash
            or body.get("configuration_sha256") != actual_hash):
            raise SessionError("Saved configuration integrity failed")
        for key in ("instance_id", "provider", "endpoint", "model_id", "settings",
                    "reasoning_settings", "instructions_sha256", "input_refs"):
            if body.get(key) != saved.get(key):
                raise SessionError("Call and saved selection differ")
        if body.get("permitted_tools") != saved.get("permitted_tools"):
            raise SessionError("Saved tool descriptor changed")
        actor = self.store.get("agent_instance", body.get("instance_id"), 1)
        if actor is None or actor["body"].get("mission_id") != body.get("mission_id"):
            raise SessionError("Call actor is unavailable in its mission")
        if _sha(saved["instructions"].encode()) != saved.get("instructions_sha256"):
            raise SessionError("Saved instructions changed")
        if (body["provider"], body["endpoint"], body["model_id"]) != ("groq", URL, MODEL):
            raise SessionError("Selected provider, endpoint or model is unsupported")
        return call, config, actor

    def _preview_selection(self, call: dict) -> tuple[dict, dict, dict]:
        if (
            type(call) is not dict or set(call) != {"kind", "id", "version", "body"}
            or call["kind"] != "agent_call" or not isinstance(call["id"], str)
            or not call["id"] or type(call["version"]) is not int or call["version"] != 1
            or type(call["body"]) is not dict
        ):
            raise SessionError("Exact provisional v1 call manifest is required")
        if self.store.get("agent_call", call["id"]) is not None:
            raise SessionError("Provisional call ID is already allocated")
        body = call["body"]
        try:
            request = _call_request(
                call_id=body["call_id"], configuration_id=body["configuration_id"],
                mission_id=body["mission_id"], task_id=body["task_id"],
                attempt_id=body["attempt_id"],
                loaded_skill_ids=body["loaded_skill_ids"],
                exposed_tool_ids=body["exposed_tool_ids"],
                require_common_tools=body.get("require_common_tools", True),
            )
        except (ConfigurationError, KeyError, TypeError) as exc:
            raise SessionError("Provisional call request is invalid") from exc
        if call["id"] != request["call_id"]:
            raise SessionError("Provisional call ID differs from its manifest")
        try:
            selected, config, actor = self._selection_for_call(call)
        except (KeyError, TypeError) as exc:
            raise SessionError("Provisional call selection is invalid") from exc
        try:
            expected = _call_manifest_body(config, request)
        except (ConfigurationError, KeyError, TypeError) as exc:
            raise SessionError("Provisional call capabilities are invalid") from exc
        if body != expected:
            raise SessionError("Provisional call differs from canonical configuration")
        task = self.store.get("board_proposal", body["task_id"])
        if task is None or task["body"].get("mission_id") != body["mission_id"]:
            raise SessionError("Provisional call task is unavailable in its mission")
        return selected, config, actor

    @staticmethod
    def _configuration(config: dict) -> tuple[CallConfiguration, str, dict | None]:
        saved = config["body"]
        settings = saved.get("settings")
        reasoning = saved.get("reasoning_settings")
        if (type(settings) is not dict
            or set(settings) - {"max_completion_tokens", "stream", "unknown_source_policy",
                                "response_format", "credential_profile", "quota_domain"}
            or type(reasoning) is not dict
            or set(reasoning) != {"reasoning_effort", "include_reasoning"}
            or reasoning.get("reasoning_effort") not in ("medium", "high")
            or reasoning.get("include_reasoning") is not True):
            raise SessionError("Saved model settings are unsupported or unpinned")
        if (type(settings.get("max_completion_tokens")) is not int
            or type(settings.get("stream")) is not bool):
            raise SessionError("Saved output budget and stream mode must be explicit")
        unknown_policy = settings.get("unknown_source_policy")
        if unknown_policy not in {"reject", "allow_metadata_only"}:
            raise SessionError("Saved unknown-source policy must be explicit")
        response_format = settings.get("response_format")
        if response_format is not None and (
            type(response_format) is not dict
            or response_format.get("type") != "json_schema"
            or type(response_format.get("json_schema")) is not dict
            or response_format["json_schema"].get("strict") is not True
        ):
            raise SessionError("Saved final output format must be strict JSON Schema")
        selection = CallConfiguration(
            configuration_id=config["id"],
            configuration_sha256=saved["configuration_sha256"],
            instructions=saved["instructions"], provider=saved["provider"],
            endpoint=saved["endpoint"], model_id=saved["model_id"],
            reasoning_effort=reasoning["reasoning_effort"],
            include_reasoning=reasoning["include_reasoning"],
            max_completion_tokens=settings["max_completion_tokens"],
            stream=settings["stream"],
            credential_profile=settings.get("credential_profile", "primary"),
            quota_domain=settings.get("quota_domain", "groq-selected"),
        )
        return selection, unknown_policy, response_format

    def _sources(self, call: dict, sources: tuple[SourceSelection, ...],
                 unknown_policy: str) -> tuple[SourceUse, ...]:
        pinned = {(
            item["kind"], item["id"], item["version"]): item["sha256"]
            for item in call["body"]["input_refs"] if item["kind"] == "source_copy"
        }
        supplied = set()
        resolved = []
        for selected in sources:
            if not isinstance(selected, SourceSelection):
                raise SessionError("Source decisions must be typed host inputs")
            pointer = selected.copy_ref
            _exact_ref(pointer, "source_copy")
            key = (pointer["kind"], pointer["id"], pointer["version"])
            if key not in pinned or key in supplied:
                raise SessionError("Source copy is missing, duplicated or changed from the call")
            supplied.add(key)
            copy = self.store.get(**pointer)
            if copy is None or content_hash(copy["body"]) != pinned[key]:
                raise SessionError("Source copy version or body hash changed")
            body = copy["body"]
            if body.get("mission_id") not in (None, call["body"]["mission_id"]):
                actor = self.store.get("agent_instance", call["body"]["instance_id"], 1)
                if not actor or not inherited_public_source(
                    self.store, mission_id=call["body"]["mission_id"],
                    run_id=actor["body"].get("run_id"), copy_ref=pointer,
                    actor_id=actor["id"], task_id=call["body"]["task_id"],
                ):
                    raise SessionError("Source copy belongs to another mission")
            use_record = body.get("use_record")
            if (isinstance(use_record, dict) and use_record.get("use") == "unknown"
                and selected.use == "permitted"):
                raise SessionError("Unknown saved copy use cannot be promoted to permitted")
            if _explicit_prohibition(body):
                raise SessionError("Saved copy prohibits model input")
            current = self.store.get("source_copy", pointer["id"])
            if current is not None and _explicit_prohibition(current["body"]):
                raise SessionError("Current saved copy-use record revoked model input")
            blob = body.get("blob")
            retained = body.get("retain_bytes") is True and isinstance(blob, dict)
            original_sha = None
            if selected.use == "permitted" and selected.allowed_for_model is True:
                _current_model_use(self.store, pointer)
                if body.get("rights_status") not in (None, "permitted"):
                    raise SessionError("Saved source rights are not confirmed for model input")
                if not retained:
                    raise SessionError("Permitted model input needs retained source bytes")
                raw = self.store.read_blob(blob["sha256"])
                if body.get("content_sha256") != _sha(raw):
                    raise SessionError("Retained source bytes disagree with their checksum")
                if selected.passage_refs:
                    from ora.evidence import EvidenceLibrary
                    from ora.evidence.intake import checked_model_text

                    checked = checked_model_text(EvidenceLibrary(self.store), pointer,
                                                 list(selected.passage_refs))
                    text = checked["text"]
                    checksum = checked["text_sha256"]
                    original_sha = checked["content_sha256"]
                else:
                    if not str(body.get("mime_type", "")).startswith("text/"):
                        raise SessionError("Model input needs a text copy or checked passage references")
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeError as exc:
                        raise SessionError("Model input needs UTF-8 source text") from exc
                    checksum = _sha(raw)
            elif (selected.use == "unknown" and selected.allowed_for_model is False
                  and unknown_policy == "allow_metadata_only"):
                text = None
                checksum = body.get("content_sha256")
                if not isinstance(checksum, str):
                    raise SessionError("Unknown source needs an exact content checksum")
            else:
                raise SessionError("Source-use decision does not permit this copy")
            resolved.append(SourceUse(pointer["id"], pointer["version"], checksum,
                                      selected.use, selected.allowed_for_model,
                                      unknown_policy=unknown_policy, text=text,
                                      route=selected.route, copy_sha256=original_sha,
                                      passage_refs=selected.passage_refs))
        if supplied != set(pinned):
            raise SessionError("Every saved source copy needs a model-use decision")
        return tuple(resolved)

    @staticmethod
    def _tools(call: dict) -> tuple[dict[str, Any], ...]:
        selected = call["body"].get("exposed_tools", [])
        descriptors = call["body"].get("permitted_tools", {})
        if not isinstance(selected, list) or len(selected) != len(set(selected)):
            raise SessionError("Saved tool exposure is invalid")
        validated = []
        for name in selected:
            descriptor = descriptors.get(name)
            if (not isinstance(descriptor, dict) or descriptor.get("tool_id") != name
                or content_hash(descriptor.get("schema")) != descriptor.get("schema_sha256")
                or not isinstance(descriptor.get("description", ""), str)):
                raise SessionError("Saved tool descriptor changed")
            if name == "browser_search":
                if (descriptor.get("execution_model") != "provider_builtin"
                    or descriptor.get("schema") != {
                        "type": "object", "properties": {}, "required": [],
                        "additionalProperties": False,
                    }):
                    raise SessionError("Saved browser_search descriptor is invalid")
                validated.append((name, descriptor))
                continue
            if descriptor.get("execution_model") == "provider_builtin":
                raise SessionError("Unsupported provider built-in tool")
            validated.append((name, descriptor))
        # Presentation order changes no grants: every pinned descriptor above
        # is checked before the exact selected set is serialized for the model.
        stable = {
            "discover_capabilities": 0, "request_capability": 1,
            "publish_result": 2, "read_context_page": 3,
        }
        ordered = (
            validated if any(name == "browser_search" for name, _ in validated)
            else sorted(validated, key=lambda item: (
                0, stable[item[0]]
            ) if item[0] in stable else (1, item[0]))
        )
        result = []
        for name, descriptor in ordered:
            if name == "browser_search":
                result.append({"type": "browser_search"})
                continue
            result.append({"type": "function", "function": {
                "name": name, "parameters": descriptor["schema"],
                **({"description": descriptor["description"]} if descriptor.get("description") else {}),
            }})
        return tuple(result)

    def _prepare_request(
        self, call: dict, config: dict, *, phase: Literal["collect", "final"],
        messages: list[dict[str, Any]], sources: tuple[SourceSelection, ...],
        parent_call_ref: dict[str, Any] | None,
    ) -> tuple[PreparedCall, CallConfiguration, str]:
        if parent_call_ref is not None:
            _exact_ref(parent_call_ref, "agent_call")
            parent = self.store.get(**parent_call_ref)
            if parent is None or any(parent["body"].get(key) != call["body"].get(key)
                                     for key in ("mission_id", "task_id", "instance_id",
                                                 "configuration_sha256")):
                raise SessionError("Parent call is unavailable for the same saved work")
        selected, unknown_policy, response_format = self._configuration(config)
        source_inputs = self._sources(call, sources, unknown_policy)
        if phase == "final":
            if response_format is None:
                raise SessionError("Saved selection lacks a final schema")
            schema_spec = response_format["json_schema"]
            tools = ()
        else:
            schema_spec = None
            tools = self._tools(call)
        try:
            prepared = self.adapter.prepare(
                selected, call_id=call["id"], attempt_id=call["body"]["attempt_id"],
                task_category=call["body"]["task_scope"], phase=phase,
                messages=messages, sources=source_inputs, tools=tools,
                schema_name=schema_spec["name"] if schema_spec else None,
                schema=schema_spec["schema"] if schema_spec else None,
                parent_call_id=parent_call_ref["id"] if parent_call_ref else None,
            )
        except (ModelPreflightError, KeyError, TypeError) as exc:
            raise SessionError("Saved model selection cannot prepare this phase") from exc
        request_bytes = canonical(prepared.body).encode()
        if _sha(request_bytes) != prepared.request_sha256:
            raise SessionError("Prepared request body hash disagrees with the manifest")
        return prepared, selected, unknown_policy

    def preview_request(
        self, *, call: dict, phase: Literal["collect", "final"],
        messages: list[dict[str, Any]], sources: tuple[SourceSelection, ...],
        parent_call_ref: dict[str, Any] | None = None,
    ) -> PreparedCall:
        """Build exact input from a canonical preview or saved call, without writes."""
        saved = (
            self.store.get("agent_call", call["id"], call["version"])
            if type(call) is dict and call.get("kind") == "agent_call"
            and isinstance(call.get("id"), str) and type(call.get("version")) is int
            else None
        )
        if saved is not None and saved == call:
            selected_call, config, _actor = self._selection(call["id"], call["version"])
        else:
            selected_call, config, _actor = self._preview_selection(call)
        prepared, _selected, _unknown_policy = self._prepare_request(
            selected_call, config, phase=phase, messages=messages, sources=sources,
            parent_call_ref=parent_call_ref,
        )
        return prepared

    def prepare(self, *, call_id: str, call_version: int, phase: Literal["collect", "final"],
                messages: list[dict[str, Any]], sources: tuple[SourceSelection, ...],
                parent_call_ref: dict[str, Any] | None = None,
                context_source_dependency_refs: tuple[dict[str, Any], ...] = (),
                request_token_estimate: dict[str, Any] | None = None,
                research_activity_digest: dict[str, Any] | None = None,
                ) -> SessionPrepared:
        call, config, _actor = self._selection(call_id, call_version)
        prepared, selected, unknown_policy = self._prepare_request(
            call, config, phase=phase, messages=messages, sources=sources,
            parent_call_ref=parent_call_ref,
        )
        request_bytes = canonical(prepared.body).encode()
        if request_token_estimate is not None:
            self._check_token_estimate(request_bytes, request_token_estimate)
        blob = self.store.put_blob(request_bytes)
        call_ref, config_ref = ref(call), ref(config)
        source_refs = [selected.copy_ref for selected in sources]
        body = {"schema": "ora.model_request_intent.v1", "mission_id": call["body"]["mission_id"],
                "task_id": call["body"]["task_id"], "attempt_id": call["body"]["attempt_id"],
                "instance_id": call["body"]["instance_id"], "call_ref": call_ref,
                "configuration_ref": config_ref, "configuration_sha256": selected.configuration_sha256,
                "request_sha256": prepared.request_sha256, "blob": blob,
                "effective_reasoning_effort": prepared.body["reasoning_effort"],
                "reasoning_effort_basis": prepared.manifest()["reasoning_effort_basis"],
                "phase": phase, "parent_call_ref": parent_call_ref,
                "source_refs": source_refs,
                "context_source_dependency_refs": list(context_source_dependency_refs),
                "evidence_refs": list(prepared.evidence_refs),
                "source_decisions": [{"copy_ref": s.copy_ref, "use": s.use,
                                      "allowed_for_model": s.allowed_for_model,
                                      "route": s.route, "unknown_policy": unknown_policy,
                                      "passage_refs": list(s.passage_refs)}
                                     for s in sources]}
        if "credential_profile" in config["body"]["settings"]:
            body.update(credential_profile=selected.credential_profile,
                        quota_domain=selected.quota_domain)
        if request_token_estimate is not None:
            body["request_token_estimate"] = request_token_estimate
        if research_activity_digest is not None:
            if not any(
                isinstance(message.get("content"), str)
                and canonical({"authority": "saved_peer_research_activity_not_source_instruction",
                               "research_activity_digest": research_activity_digest}) == message["content"]
                for message in messages
            ):
                raise SessionError("Research activity metadata differs from model input")
            body["research_activity_digest"] = research_activity_digest
        intent_id = f"model-intent:{call_id}:{call_version}"

        def save(tx):
            saved_call = tx.get(**call_ref)
            saved_config = tx.get(**config_ref)
            if saved_call != call or saved_config != config:
                raise SessionError("Call or configuration changed before intent commit")
            quality_refs = []
            for pointer in source_refs:
                copy = tx.get(**pointer)
                if copy is None or _explicit_prohibition(copy["body"]):
                    raise SessionError("Source copy changed before intent commit")
                current = tx.get("source_copy", pointer["id"])
                if current is not None and _explicit_prohibition(current["body"]):
                    raise SessionError("Current copy-use record revoked model input")
                if next(s.allowed_for_model for s in sources if s.copy_ref == pointer):
                    policy_ref = _current_model_use(tx, pointer)
                    selection = next(s for s in sources if s.copy_ref == pointer)
                    try:
                        quality_refs.append(attest_local_text_quality_tx(
                            tx, pointer, policy_ref,
                            require_bound_quality=bool(selection.passage_refs),
                        ))
                    except EvidenceError as exc:
                        raise SessionError(str(exc)) from exc
            for pointer in context_source_dependency_refs:
                policy_ref = _current_context_dependency_use(
                    tx, pointer, body["mission_id"], call_ref,
                )
                try:
                    quality_refs.append(attest_local_text_quality_tx(
                        tx, pointer, policy_ref,
                    ))
                except EvidenceError as exc:
                    raise SessionError(str(exc)) from exc
            intent = tx.put("model_request_intent", intent_id, body)
            tx.link(intent, saved_call, "prepared_from")
            tx.link(intent, saved_config, "uses_configuration")
            for quality_ref in {canonical(item): item for item in quality_refs}.values():
                tx.link(intent, tx.get(**quality_ref), "depends_on")
            for pointer in source_refs:
                tx.link(intent, tx.get(**pointer), "uses_source_copy")
            for selection in sources:
                for pointer in selection.passage_refs:
                    passage = tx.get(**pointer)
                    if passage is None or passage["body"].get("copy_ref") != selection.copy_ref:
                        raise SessionError("Prepared passage is not from the selected source copy")
                    tx.link(intent, passage, "uses_source_passage")
            return ref(intent)

        intent_ref = self.store.transact(f"model-intent-save:{intent_id}", body, save,
                                         mission_id=body["mission_id"])
        return SessionPrepared(intent_ref, prepared)

    @staticmethod
    def _check_token_estimate(raw: bytes, saved: dict) -> None:
        """Retain the exact saved estimate basis; never reprice an old request."""
        if (not isinstance(saved, dict)
                or set(saved) != {"tokens", "basis", "asset_sha256", "fallback_reason"}
                or type(saved.get("tokens")) is not int
                or saved.get("basis") not in {
                    LEGACY_BASIS, TOKENIZER_BASIS_V1, TOKENIZER_BASIS_V2,
                    TOKENIZER_BASIS_V3, TOKENIZER_BASIS,
                }):
            raise SessionError("Saved request token estimate is invalid")
        try:
            current = estimate_request_tokens(raw, basis=saved["basis"])
        except (TokenEstimateUnavailable, ValueError) as exc:
            raise SessionError("Saved request token estimate cannot be reproduced") from exc
        if any(saved[key] != getattr(current, key) for key in ("tokens", "basis", "asset_sha256")):
            raise SessionError("Saved request token estimate differs from exact bytes")

    def load(self, intent_ref: dict[str, Any], *, for_dispatch: bool = False) -> SessionPrepared:
        _exact_ref(intent_ref, "model_request_intent")
        intent = self.store.get(**intent_ref)
        if intent is None:
            raise SessionError("Prepared request intent is unavailable")
        saved = intent["body"]
        call_ref = saved["call_ref"]
        call, config, _actor = self._selection(call_ref["id"], call_ref["version"])
        if saved["configuration_ref"] != ref(config) or saved["call_ref"] != ref(call):
            raise SessionError("Prepared request lineage changed")
        selected, unknown_policy, _format = self._configuration(config)
        if (saved.get("credential_profile", "primary") != selected.credential_profile
                or saved.get("quota_domain", "groq-selected") != selected.quota_domain):
            raise SessionError("Prepared credential profile differs from saved configuration")
        if for_dispatch:
            decisions = tuple(SourceSelection(item["copy_ref"], item["use"],
                                              item["allowed_for_model"], item["route"],
                                              tuple(item.get("passage_refs", [])))
                              for item in saved["source_decisions"])
            self._sources(call, decisions, unknown_policy)
            for pointer in saved.get("context_source_dependency_refs", []):
                _current_context_dependency_use(
                    self.store, pointer, saved["mission_id"], saved["call_ref"]
                )
        request_bytes = self.store.read_blob(saved["blob"]["sha256"])
        if _sha(request_bytes) != saved["request_sha256"]:
            raise SessionError("Prepared request bytes changed")
        if for_dispatch and "request_token_estimate" in saved:
            self._check_token_estimate(request_bytes, saved["request_token_estimate"])
        body = json.loads(request_bytes)
        if for_dispatch:
            _check_visible_shared_materials(self.store, intent, body)
        if saved.get("effective_reasoning_effort") is not None and (
            saved["effective_reasoning_effort"] != body.get("reasoning_effort")
            or saved.get("reasoning_effort_basis") != (
                "browser_search" if {"type": "browser_search"} in body.get("tools", [])
                else "selected_configuration"
            )
        ):
            raise SessionError("Saved effective reasoning effort differs from request")
        prepared = PreparedCall(
            call_id=call["id"], attempt_id=saved["attempt_id"],
            parent_call_id=(saved["parent_call_ref"]["id"]
                            if saved["parent_call_ref"] else None),
            task_category=call["body"]["task_scope"],
            phase=saved["phase"], configuration_id=config["id"],
            configuration_sha256=saved["configuration_sha256"],
            evidence_refs=tuple(saved["evidence_refs"]),
            body=body, request_sha256=saved["request_sha256"],
            credential_profile=selected.credential_profile,
            quota_domain=selected.quota_domain,
        )
        return SessionPrepared(intent_ref, prepared)

    @staticmethod
    def _context(intent: dict, actor: dict) -> TraceContext:
        body = intent["body"]
        call_ref = body["call_ref"]
        return TraceContext(
            mission_id=body["mission_id"], run_id=actor["body"]["run_id"],
            task_id=body["task_id"], attempt_id=body["attempt_id"],
            instance_id=body["instance_id"],
            operation_id=f"model-call:{call_ref['id']}:{call_ref['version']}",
            call_id=call_ref["id"], call_version=call_ref["version"],
            parent_operation_id=(f"model-call:{body['parent_call_ref']['id']}:"
                                 f"{body['parent_call_ref']['version']}"
                                 if body["parent_call_ref"] else None),
            configuration_ref=body["configuration_ref"],
        )

    def capture(self, intent_ref: dict[str, Any], outcome: ModelOutcome) -> dict:
        prepared = self.load(intent_ref)
        intent = self.store.get(**intent_ref)
        body = intent["body"]
        if (outcome.call_id != prepared.request.call_id
            or outcome.attempt_id != prepared.request.attempt_id
            or outcome.request_sha256 != prepared.request.request_sha256):
            raise SessionError("Provider outcome does not match the prepared request")
        if (outcome.status in {"complete", "tool_requested"}
            and outcome.returned_model != MODEL):
            raise SessionError("Successful provider outcome did not return the selected model")
        actor = self.store.get("agent_instance", body["instance_id"], 1)
        context = self._context(intent, actor)
        if outcome.reasoning_status in {"absent", "unknown", "unsupported"} and outcome.reasoning is not None:
            raise SessionError("Unavailable reasoning cannot contain text")
        if outcome.reasoning_status == "returned" and not outcome.reasoning:
            raise SessionError("Returned reasoning needs provider text")
        safe_reasoning, redacted = (redact_text(outcome.reasoning)
                                    if outcome.reasoning is not None else (None, False))
        reasoning_blob = (self.store.put_blob(safe_reasoning.encode())
                          if safe_reasoning is not None else None)
        raw_reasoning_blob = (self.store.put_blob(outcome.reasoning.encode("utf-8"))
                              if outcome.reasoning is not None else None)
        raw_reasoning_ref = ({"kind": "restricted_reasoning_raw",
                              "id": f"model-reasoning-raw:{intent['id']}", "version": 1}
                             if raw_reasoning_blob is not None else None)
        reasoning_ref = {"kind": "restricted_reasoning",
                         "id": f"model-reasoning:{intent['id']}", "version": 1}
        reasoning_body = {**asdict(context), "schema": "ora.reasoning.v1",
                          "status": outcome.reasoning_status, "blob": reasoning_blob,
                          "raw_ref": raw_reasoning_ref,
                          "redacted": bool(redacted), "origin": "provider_returned",
                          "access": "restricted_diagnostics"}
        private_payload = {"content": outcome.content, "tool_calls": outcome.tool_calls}
        if outcome.provider_metadata is not None:
            private_payload["provider_metadata"] = outcome.provider_metadata
        if outcome.provider_error is not None:
            # Error text can contain account details. Retain it only with the
            # private response, separate from findings and ordinary trace summaries.
            private_payload["provider_error"] = outcome.provider_error
        private_blob = self.store.put_blob(canonical(private_payload).encode())
        receipt_body = {
            "schema": "ora.model_response_receipt.v1", "mission_id": body["mission_id"],
            "task_id": body["task_id"], "attempt_id": body["attempt_id"],
            "call_ref": body["call_ref"], "configuration_ref": body["configuration_ref"],
            "intent_ref": intent_ref, "request_sha256": body["request_sha256"],
            "effective_reasoning_effort": prepared.request.body["reasoning_effort"],
            "reasoning_effort_basis": prepared.request.manifest()["reasoning_effort_basis"],
            "response_sha256": outcome.response_sha256,
            "response_id": outcome.response_id,
            "provider_request_id": outcome.provider_request_id,
            "returned_model": outcome.returned_model,
            "status": outcome.status, "finish_reason": outcome.finish_reason,
            "reasoning_status": outcome.reasoning_status,
            "reasoning_ref": reasoning_ref,
            "blob": private_blob,
            "partial_response_sha256": (_sha(outcome.partial_response)
                                        if outcome.partial_response is not None else None),
            "usage": outcome.usage, "cache_observation": outcome.cache_observation,
            "token_measurement": measure_tokens(
                estimate=body.get("request_token_estimate"), usage=outcome.usage,
                http_status=outcome.http_status, provider_error=outcome.provider_error,
                model=prepared.request.body["model"],
            ),
            "rate_headers": outcome.rate_headers,
            "timings": outcome.timings, "error_kind": outcome.error_kind,
            "http_status": outcome.http_status,
            "retry_after_seconds": outcome.retry_after_seconds,
            "credential_profile": prepared.request.credential_profile,
            "quota_domain": prepared.request.quota_domain,
        }
        usage = [{"receipt_id": f"{intent['id']}:{outcome.provider_request_id or outcome.response_sha256 or 'unknown'}",
                  "scope": "groq:selected-model", "unit": key, "amount": value,
                  "basis": "observed"}
                 for key, value in outcome.usage.items() if key.endswith("_tokens")]
        receipt_id = f"model-response:{intent['id']}"

        def save(tx):
            saved_intent = tx.get(**intent_ref)
            if saved_intent != intent:
                raise SessionError("Prepared request intent changed before capture")
            raw_record = None
            if raw_reasoning_ref is not None:
                raw_record = tx.put("restricted_reasoning_raw", raw_reasoning_ref["id"],
                                    {"schema": "ora.reasoning_raw.v1",
                                     "mission_id": body["mission_id"],
                                     "run_id": context.run_id,
                                     "intent_ref": intent_ref,
                                     "request_sha256": body["request_sha256"],
                                     "call_ref": body["call_ref"],
                                     "instance_id": body["instance_id"],
                                     "attempt_id": body["attempt_id"],
                                     "blob": raw_reasoning_blob,
                                     "origin": "provider_returned",
                                     "access": "restricted_diagnostics"})
                if ref(raw_record) != raw_reasoning_ref:
                    raise SessionError("Raw reasoning reference changed")
            reasoning_record = tx.put("restricted_reasoning", reasoning_ref["id"], reasoning_body)
            if ref(reasoning_record) != reasoning_ref:
                raise SessionError("Restricted reasoning reference changed")
            if raw_record is not None:
                tx.link(reasoning_record, raw_record, "has_exact_provider_text")
            receipt = tx.put("model_response_receipt", receipt_id, receipt_body)
            tx.link(receipt, saved_intent, "responds_to")
            tx.link(receipt, reasoning_record, "has_restricted_reasoning")
            marker = tx.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}")
            if marker is not None:
                if marker["body"].get("intent_ref") != intent_ref:
                    raise SessionError("Dispatch marker belongs to another intent")
                resolved = tx.put("model_dispatch_attempt", marker["id"],
                                  {**marker["body"], "status": "captured",
                                   "receipt_ref": ref(receipt)},
                                  expected_version=marker["version"])
                tx.link(resolved, marker, "derived_from")
                tx.link(resolved, receipt, "resolved_by")
            self.journal.append_tx(
                tx, f"model-outcome:{intent['id']}", context=context,
                category="model", status=outcome.status,
                action="Record selected model response",
                summary="The selected model call has a saved provider outcome.",
                inputs=[body["configuration_ref"], body["call_ref"], intent_ref,
                        *body["source_refs"]], outputs=[ref(receipt)],
                timing={"wall_seconds": outcome.timings.get("wall_seconds", 0),
                        "basis": "observed_local"}, usage=usage,
                proof="provider_response" if outcome.response_sha256 else "local_transport_observation",
            )
            return ref(receipt)

        saved_ref = self.store.transact(f"model-response-save:{intent['id']}", receipt_body, save,
                                       mission_id=body["mission_id"])
        try:
            export_receipt_reasoning(self.store, saved_ref)
        except Exception as exc:
            record_export_failure(self.store, saved_ref, exc)
        return saved_ref

    def invoke_capture(
        self, intent_ref: dict[str, Any], *,
        pre_dispatch_check: Callable[[Transaction, dict], None] | None = None,
    ) -> dict:
        """Executor callback after the host has admitted this exact call."""
        _exact_ref(intent_ref, "model_request_intent")
        prepared = self.load(intent_ref)
        existing = self.store.get("model_response_receipt", f"model-response:{intent_ref['id']}")
        if existing is not None:
            if (existing["body"].get("intent_ref") != intent_ref
                or existing["body"].get("request_sha256") != prepared.request.request_sha256):
                raise SessionError("Saved response does not match this exact intent")
            return ref(existing)
        prepared = self.load(intent_ref, for_dispatch=True)
        intent = self.store.get(**intent_ref)
        marker_id = f"model-dispatch:{intent['id']}"
        marker_body = {"schema": "ora.model_dispatch_attempt.v1",
                       "mission_id": intent["body"]["mission_id"],
                       "intent_ref": intent_ref,
                       "call_ref": intent["body"]["call_ref"],
                       "request_sha256": prepared.request.request_sha256,
                       "credential_profile": prepared.request.credential_profile,
                       "quota_domain": prepared.request.quota_domain,
                       "status": "attempted_external_effect"}

        def mark(tx):
            receipt = tx.get("model_response_receipt", f"model-response:{intent['id']}")
            if receipt is not None:
                if receipt["body"].get("intent_ref") != intent_ref:
                    raise SessionError("Saved response belongs to another intent")
                return {"receipt_ref": ref(receipt)}
            if tx.get("model_dispatch_attempt", marker_id) is not None:
                raise DispatchUncertain("Model dispatch already attempted; reconcile before retry")
            saved_intent = tx.get(**intent_ref)
            call = tx.get(**intent["body"]["call_ref"])
            if saved_intent != intent or call is None:
                raise SessionError("Prepared request lineage changed before dispatch")
            pinned = {(item["kind"], item["id"], item["version"]): item["sha256"]
                      for item in call["body"]["input_refs"]
                      if item["kind"] == "source_copy"}
            policy_refs = []
            quality_refs = []
            for pointer in intent["body"]["source_refs"]:
                key = (pointer["kind"], pointer["id"], pointer["version"])
                expected_hash = pinned.pop(key, None)
                copy = tx.get(**pointer)
                if expected_hash is None or copy is None or content_hash(copy["body"]) != expected_hash:
                    raise SessionError("Source copy version or body hash changed before dispatch")
                if copy["body"].get("mission_id") not in (None, intent["body"]["mission_id"]):
                    actor = tx.get("agent_instance", intent["body"]["instance_id"], 1)
                    if not actor or not inherited_public_source(
                        tx, mission_id=intent["body"]["mission_id"],
                        run_id=actor["body"].get("run_id"), copy_ref=pointer,
                        actor_id=actor["id"], task_id=intent["body"]["task_id"],
                    ):
                        raise SessionError("Source copy belongs to another mission")
                if _explicit_prohibition(copy["body"]):
                    raise SessionError("Saved copy prohibits model input")
                current = tx.get("source_copy", pointer["id"])
                if current is not None and _explicit_prohibition(current["body"]):
                    raise SessionError("Current saved copy-use record revoked model input")
                decision = next(item for item in intent["body"]["source_decisions"]
                                if item["copy_ref"] == pointer)
                if decision["allowed_for_model"]:
                    policy_ref = _current_model_use(tx, pointer)
                    if policy_ref:
                        policy_refs.append(policy_ref)
                    try:
                        quality_refs.append(attest_local_text_quality_tx(
                            tx, pointer, policy_ref,
                            require_bound_quality=bool(decision.get("passage_refs")),
                        ))
                    except EvidenceError as exc:
                        raise SessionError(str(exc)) from exc
            if pinned:
                raise SessionError("Prepared request omitted a pinned source copy")
            for pointer in intent["body"].get("context_source_dependency_refs", []):
                policy_ref = _current_context_dependency_use(
                    tx, pointer, intent["body"]["mission_id"], intent["body"]["call_ref"]
                )
                if policy_ref is not None:
                    policy_refs.append(policy_ref)
                try:
                    quality_refs.append(attest_local_text_quality_tx(
                        tx, pointer, policy_ref,
                    ))
                except EvidenceError as exc:
                    raise SessionError(str(exc)) from exc
            _check_visible_shared_materials(tx, saved_intent, prepared.request.body)
            if pre_dispatch_check is not None:
                pre_dispatch_check(tx, saved_intent)
            saved = tx.put("model_dispatch_attempt", marker_id,
                           {**marker_body, "source_use_refs": policy_refs})
            tx.link(saved, saved_intent, "attempts_request")
            for quality_ref in {canonical(item): item for item in quality_refs}.values():
                tx.link(saved, tx.get(**quality_ref), "depends_on")
            return {"marker_ref": ref(saved)}

        decision = self.store.transact(f"model-dispatch-mark:{uuid.uuid4().hex}", marker_body,
                                       mark, mission_id=marker_body["mission_id"])
        if "receipt_ref" in decision:
            return decision["receipt_ref"]
        try:
            outcome = self.adapter.invoke(prepared.request)
        except HTTPDispatchBlocked:
            # The transport guard has confirmed that the opener was never
            # entered. Keep that proof on the exact dispatch marker so the
            # host can release its reservation without inventing a response.
            def mark_blocked(tx):
                marker = tx.get("model_dispatch_attempt", marker_id)
                if (marker is None or marker["body"].get("intent_ref") != intent_ref
                    or marker["body"].get("request_sha256") != prepared.request.request_sha256
                    or marker["body"].get("status") != "attempted_external_effect"
                    or tx.get("model_response_receipt", f"model-response:{intent['id']}") is not None):
                    raise DispatchUncertain("Model dispatch changed during local admission")
                blocked = tx.put("model_dispatch_attempt", marker_id,
                                 {**marker["body"], "status": "blocked_before_external_effect"},
                                 expected_version=marker["version"])
                tx.link(blocked, marker, "derived_from")
                return ref(blocked)

            self.store.transact(f"model-dispatch-blocked:{intent['id']}",
                                {"intent_ref": intent_ref}, mark_blocked,
                                mission_id=marker_body["mission_id"])
            raise
        return self.capture(intent_ref, outcome)
