"""Visible draft recovery uses exact saved model lineage and current source policy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ora.agents import COMMON_TOOL_IDS, AgentRegistry, CapabilityCatalog, ConfigurationRegistry
from ora.agents.capabilities import content_hash
from ora.evidence import EvidenceLibrary
from ora.mission.output_recovery import (
    MAX_OUTPUT_WINDOW_CHARS,
    OutputRecoveryError,
    read_partial_output_window,
)
from ora.models import GroqSelectedAdapter, ModelSession, SourceSelection
from ora.state import StateStore
from ora.state.store import canonical

ROOT = Path(__file__).resolve().parents[2]
MODEL = "openai/gpt-oss-120b"
URL = "https://api.groq.com/openai/v1/chat/completions"
VISIBLE = "A visible but unfinished synthesis from the selected abstract."


def _copy(store: StateStore, *, suffix: str = "first") -> dict:
    library = EvidenceLibrary(store)
    publication = library.register_publication(
        f"publication-{suffix}", identifiers={"doi": f"10.1234/{suffix}"},
        title=f"Synthetic {suffix} publication", verification="Synthetic fixture identity",
    )["publication_ref"]
    encounter = library.record_encounter(
        f"encounter-{suffix}", publication_ref=publication, retrieval_hit_ref=None,
        status="accepted", access_surface="abstract", retrieved_at="2026-01-02T00:00:00Z",
        route="synthetic-fixture",
    )["encounter_ref"]
    text = f"Synthetic {suffix} abstract has a checked result."
    return library.add_copy(
        f"copy-{suffix}", publication, encounter, content=text.encode(), content_sha256=None,
        mime_type="text/plain", source_version="synthetic-v1", access_surface="abstract",
        retrieved_at="2026-01-02T00:00:00Z", rights_basis="Synthetic project-owned fixture",
        retain_bytes=True, reference=f"simulated://{suffix}", passages=[],
        inspection_state="checked", validation_record="synthetic-fixture-check",
    )["copy_ref"]


def _saved_partial(tmp_path: Path, *, visible: str | None = VISIBLE,
                   with_source: bool = True, with_context: bool = True,
                   tool_name: str | None = None,
                   source_turn_mode: str = "all_allowed_sources_fit") -> tuple:
    store = StateStore(tmp_path)
    copy_ref = _copy(store) if with_source else None
    policy_body = ({
        "copy_ref": copy_ref, "rights_status": "permitted", "policy_status": "granted",
        "allowed_uses": ["discovery", "model_input"],
    } if copy_ref else None)

    if copy_ref:
        def grant(tx):
            saved = tx.put("source_use", "policy-first", policy_body)
            tx.bind("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}",
                    "source_use", saved["id"])
            return saved

        store.transact("grant-first", {}, grant)
    actor = AgentRegistry(store, ROOT).register(
        "output-recovery-actor", mission_id="mission", run_id="run",
        role_id="evidence_preparation_synthesis", display_name="Synthetic worker",
    )
    registry = ConfigurationRegistry(store, ROOT, CapabilityCatalog.with_common())
    config = registry.save_configuration(
        "output-recovery-config", instance_id=actor["id"], mission_id="mission",
        task_scope="synthetic-synthesis", provider="groq", endpoint=URL, model_id=MODEL,
        settings={"max_completion_tokens": 8192, "stream": False,
                  "unknown_source_policy": "reject",
                  "response_format": {"type": "json_schema", "json_schema": {
                      "name": "answer", "strict": True,
                      "schema": {"type": "object", "properties": {
                          "answer": {"type": "string"}}, "required": ["answer"],
                          "additionalProperties": False},
                  }}},
        reasoning_settings={"reasoning_effort": "high", "include_reasoning": True},
        instructions="Use only the selected synthetic source.", skill_choices={},
        grants={f"common:{name}" for name in COMMON_TOOL_IDS},
        input_refs=([{"kind": "source_copy", "id": copy_ref["id"],
                      "version": copy_ref["version"],
                      "sha256": content_hash(store.get(**copy_ref)["body"])}]
                    if copy_ref else []),
    )
    call = registry.start_call(
        "output-recovery-call", call_id="call-first", configuration_id=config["id"],
        mission_id="mission", task_id="task", attempt_id="attempt-first",
        loaded_skill_ids=[], exposed_tool_ids=[], require_common_tools=False,
    )
    adapter = GroqSelectedAdapter(ROOT)
    bridge = ModelSession(store, ROOT, adapter=adapter)
    messages = [{"role": "user", "content": "Write a bounded answer."}]
    current_tool_turn_ref = None
    if tool_name:
        call_ref = {key: call[key] for key in ("kind", "id", "version")}
        if tool_name == "read_source_window":
            assert copy_ref is not None
            window = store.transact("seed-source-window", {}, lambda tx: tx.put(
                "source_read_window", "synthetic-window", {"copy_ref": copy_ref},
            ))
            content = canonical({
                "window_ref": {key: window[key] for key in ("kind", "id", "version")},
                "copy_ref": copy_ref,
            })
        else:
            content = canonical({"status": "success", "hits": [{"title": "Synthetic hit"}]})
        tool_message = {"role": "tool", "tool_call_id": "prior-tool",
                        "name": tool_name, "content": content}
        tool_blob = store.put_blob(canonical(tool_message).encode())

        def save_tool(tx):
            response = tx.put("model_response_receipt", "synthetic-prior-tool", {
                "mission_id": "mission", "task_id": "task", "status": "tool_requested",
                "call_ref": call_ref,
            })
            response_ref = {key: response[key] for key in ("kind", "id", "version")}
            tx.put("context_tool_result", f"tool-result:{response['id']}", {
                "mission_id": "mission", "response_ref": response_ref,
                "tool_call_id": "prior-tool", "blob": tool_blob,
                "message_sha256": hashlib.sha256(canonical(tool_message).encode()).hexdigest(),
                "effect_ref": None,
            })
            return response_ref

        current_tool_turn_ref = store.transact("seed-tool-turn", {}, save_tool)
        messages += [
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "prior-tool", "type": "function",
                "function": {"name": tool_name, "arguments": "{}"},
            }]},
            tool_message,
        ]
    prepared = bridge.prepare(
        call_id=call["id"], call_version=call["version"], phase="final",
        messages=messages,
        sources=(SourceSelection(copy_ref, "permitted", True),) if copy_ref else (),
    )
    payload = {
        "id": "synthetic-cutoff", "model": MODEL,
        "choices": [{"message": {"role": "assistant", "content": visible,
                                 "reasoning": "PRIVATE_REASONING_SENTINEL",
                                 "tool_calls": [{"id": "cutoff-tool", "type": "function",
                                                 "function": {"name": "lookup",
                                                              "arguments": '{"incomplete":'}}]},
                     "finish_reason": "length"}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 40, "total_tokens": 70},
    }
    outcome = adapter.decode(prepared.request, payload)
    assert outcome.status == "partial" and outcome.tool_calls == ()
    response_ref = bridge.capture(prepared.intent_ref, outcome)
    if with_context:
        intent = store.get(**prepared.intent_ref)
        call_ref = {key: call[key] for key in ("kind", "id", "version")}
        config_ref = {key: config[key] for key in ("kind", "id", "version")}

        def save_context(tx):
            budget = tx.put("mission_call_budget", call["id"], {
                "mission_id": "mission", "call_ref": call_ref,
                "source_turn_mode": source_turn_mode,
                "retained_source_refs": [copy_ref] if copy_ref else [],
                "current_tool_turn_ref": current_tool_turn_ref,
                "compacted_tool_turn_refs": [],
            })
            inclusion = tx.put("context_inclusion", f"context:{intent['id']}", {
                "mission_id": "mission", "intent_ref": prepared.intent_ref,
                "call_ref": call_ref, "configuration_ref": config_ref,
                "source_refs": [copy_ref] if copy_ref else [],
                "request_sha256": intent["body"]["request_sha256"],
                "included_refs": [call_ref, config_ref, *([copy_ref] if copy_ref else [])],
                "parent_run_ref": None, "followup_target_ref": None,
                "tool_result_compactions": [],
            })
            return {"budget_ref": {key: budget[key] for key in ("kind", "id", "version")},
                    "inclusion_ref": {key: inclusion[key] for key in ("kind", "id", "version")}}

        store.transact("save-context", {}, save_context)
    return store, response_ref, actor["id"], copy_ref, policy_body


def _read(store, response_ref, actor_id, selected, *, offset=0, limit=15):
    return read_partial_output_window(
        store, response_ref, mission_id="mission", task_id="task", actor_id=actor_id,
        current_call_source_refs=selected, offset=offset, limit=limit,
    )


def test_exact_partial_visible_text_pages_without_reasoning_or_tool_fragments(tmp_path: Path) -> None:
    store, response_ref, actor_id, source, _ = _saved_partial(tmp_path)
    first = _read(store, response_ref, actor_id, [source], limit=15)
    assert first["status"] == "available" and first["text"] == VISIBLE[:15]
    assert first["content_sha256"] == hashlib.sha256(VISIBLE.encode()).hexdigest()
    assert first["window_sha256"] == hashlib.sha256(VISIBLE[:15].encode()).hexdigest()
    assert first["source_refs"] == [source] and len(first["source_use_refs"]) == 1
    chunks = [first["text"]]
    cursor = first["next_offset"]
    while cursor is not None:
        page = _read(store, response_ref, actor_id, [source], offset=cursor, limit=15)
        chunks.append(page["text"])
        cursor = page["next_offset"]
    assert "".join(chunks) == VISIBLE
    assert "PRIVATE_REASONING_SENTINEL" not in json.dumps(first)
    assert "cutoff-tool" not in json.dumps(first)
    assert "incomplete" not in json.dumps(first)


def test_foreign_actor_task_and_reference_cannot_read_partial(tmp_path: Path) -> None:
    store, response_ref, actor_id, source, _ = _saved_partial(tmp_path)
    with pytest.raises(OutputRecoveryError, match="lineage"):
        _read(store, response_ref, "foreign-actor", [source])
    with pytest.raises(OutputRecoveryError, match="length cutoff"):
        read_partial_output_window(
            store, response_ref, mission_id="mission", task_id="foreign-task",
            actor_id=actor_id, current_call_source_refs=[source],
        )
    with pytest.raises(OutputRecoveryError, match="Exact model_response_receipt"):
        _read(store, {**response_ref, "kind": "agent_call"}, actor_id, [source])


def test_revoked_policy_and_compacted_source_set_return_no_draft(tmp_path: Path) -> None:
    store, response_ref, actor_id, source, policy = _saved_partial(tmp_path)
    compacted = _read(store, response_ref, actor_id, [])
    assert compacted["status"] == "reissue_required"
    assert compacted["reason"] == "source_context_changed"
    assert "text" not in compacted and "content_sha256" not in compacted
    other = _copy(store, suffix="other")
    missing = _read(store, response_ref, actor_id, [other])
    assert missing["status"] == "reissue_required" and "text" not in missing
    store.transact("revoke-first", {}, lambda tx: tx.put(
        "source_use", "policy-first",
        {**policy, "policy_status": "revoked", "allowed_uses": ["discovery"]},
        expected_version=1,
    ))
    revoked = _read(store, response_ref, actor_id, [source])
    assert revoked["status"] == "reissue_required"
    assert revoked["reason"] == "source_use_changed" and "text" not in revoked


def test_newer_unknown_copy_use_cannot_replay_prior_visible_text(tmp_path: Path) -> None:
    store, response_ref, actor_id, source, _ = _saved_partial(tmp_path)
    original = store.get(**source)
    store.transact("newer-unknown-copy", {}, lambda tx: tx.put(
        "source_copy", source["id"],
        {**original["body"], "rights_status": "unknown"}, expected_version=source["version"],
    ))
    result = _read(store, response_ref, actor_id, [source])
    assert result["status"] == "reissue_required" and "text" not in result


def test_source_free_call_without_evidence_can_resume_visible_text(tmp_path: Path) -> None:
    store, response_ref, actor_id, source, _ = _saved_partial(tmp_path, with_source=False)
    assert source is None
    result = _read(store, response_ref, actor_id, [])
    assert result["status"] == "available" and result["text"] == VISIBLE[:15]
    assert result["source_refs"] == []


def test_requested_window_mode_preserves_partial_output_source_checks(tmp_path: Path) -> None:
    store, response_ref, actor_id, source, _ = _saved_partial(
        tmp_path, source_turn_mode="requested_window_recovery",
    )
    allowed = _read(store, response_ref, actor_id, [source])
    assert allowed["status"] == "available" and allowed["text"] == VISIBLE[:15]

    other = _copy(store, suffix="omitted-requested-window")
    budget = store.get("mission_call_budget", "call-first")
    store.transact("requested-window-retained-copy", {}, lambda tx: tx.put(
        "mission_call_budget", budget["id"], {
            **budget["body"], "retained_source_refs": [source, other],
        }, expected_version=budget["version"],
    ))
    omitted = _read(store, response_ref, actor_id, [source])
    assert omitted["status"] == "reissue_required"
    assert omitted["reason"] == "source_context_changed" and "text" not in omitted


def test_missing_context_or_unselected_retained_source_never_replays_draft(tmp_path: Path) -> None:
    store, response_ref, actor_id, source, _ = _saved_partial(
        tmp_path / "missing", with_context=False,
    )
    unknown = _read(store, response_ref, actor_id, [source])
    assert unknown["status"] == "reissue_required"
    assert unknown["reason"] == "context_dependencies_unverified" and "text" not in unknown

    store, response_ref, actor_id, source, _ = _saved_partial(tmp_path / "extra")
    other = _copy(store, suffix="retained-but-unselected")
    budget = store.get("mission_call_budget", "call-first")
    store.transact("more-retained-sources", {}, lambda tx: tx.put(
        "mission_call_budget", budget["id"],
        {**budget["body"], "retained_source_refs": [source, other]}, expected_version=1,
    ))
    excluded = _read(store, response_ref, actor_id, [source])
    assert excluded["status"] == "reissue_required"
    assert excluded["reason"] == "source_context_changed" and "text" not in excluded


def test_exact_source_window_tool_dependency_must_remain_selected(tmp_path: Path) -> None:
    store, response_ref, actor_id, source, _ = _saved_partial(
        tmp_path, tool_name="read_source_window",
    )
    allowed = _read(store, response_ref, actor_id, [source])
    assert allowed["status"] == "available" and allowed["source_refs"] == [source]
    excluded = _read(store, response_ref, actor_id, [])
    assert excluded["status"] == "reissue_required" and "text" not in excluded


def test_unknown_tool_or_unattributed_note_blocks_visible_draft(tmp_path: Path) -> None:
    store, response_ref, actor_id, _, _ = _saved_partial(
        tmp_path / "search", with_source=False, tool_name="search_scholarly",
    )
    unknown_tool = _read(store, response_ref, actor_id, [])
    assert unknown_tool["status"] == "reissue_required"
    assert unknown_tool["reason"] == "context_dependencies_unverified"
    assert "text" not in unknown_tool

    store, response_ref, actor_id, source, _ = _saved_partial(tmp_path / "note")
    inclusion = store.get("context_inclusion", "context:model-intent:call-first:1")

    def include_note(tx):
        note = tx.put("working_note", "synthetic-note", {
            "mission_id": "mission", "actor_id": actor_id, "task_id": "task",
            "text": "A source-derived note without recorded source inputs.",
        })
        pointer = {key: note[key] for key in ("kind", "id", "version")}
        return tx.put("context_inclusion", inclusion["id"], {
            **inclusion["body"], "included_refs": [*inclusion["body"]["included_refs"], pointer],
        }, expected_version=1)

    store.transact("include-unattributed-note", {}, include_note)
    unknown_note = _read(store, response_ref, actor_id, [source])
    assert unknown_note["status"] == "reissue_required"
    assert unknown_note["reason"] == "context_dependencies_unverified"
    assert "text" not in unknown_note


def test_no_visible_content_and_invalid_windows_do_not_fabricate_draft(tmp_path: Path) -> None:
    store, response_ref, actor_id, source, _ = _saved_partial(tmp_path, visible=None)
    result = _read(store, response_ref, actor_id, [source])
    assert result["status"] == "content_unavailable" and "text" not in result
    with pytest.raises(OutputRecoveryError, match="bounded range"):
        _read(store, response_ref, actor_id, [source], limit=MAX_OUTPUT_WINDOW_CHARS + 1)
    with pytest.raises(OutputRecoveryError, match="nonnegative"):
        _read(store, response_ref, actor_id, [source], offset=-1)


def test_saved_response_with_changed_intent_lineage_is_rejected(tmp_path: Path) -> None:
    store, response_ref, actor_id, source, _ = _saved_partial(tmp_path)
    original = store.get(**response_ref)
    changed = store.transact("changed-lineage", {}, lambda tx: tx.put(
        "model_response_receipt", original["id"],
        {**original["body"], "request_sha256": "0" * 64}, expected_version=1,
    ))
    with pytest.raises(OutputRecoveryError, match="lineage"):
        _read(store, {k: changed[k] for k in ("kind", "id", "version")}, actor_id, [source])
