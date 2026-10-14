"""Synthetic captured-400 feedback compaction and preparation-retry boundaries."""

from __future__ import annotations

import json
import time
import urllib.error
from io import BytesIO
from types import SimpleNamespace

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission import service as service_module
from ora.models.groq import rejected_unregistered_local_tool_name
from tests.mission.test_service import service


def _error(name: str = "read_source_inventory") -> dict:
    return {
        "type": "invalid_request_error", "code": "tool_use_failed",
        "message": ("Tool call validation failed: tool call validation failed: "
                    f"attempted to call tool '{name}' which was not in request.tools"),
        "failed_generation": "SYNTHETIC_PRIVATE_GENERATION_MUST_NOT_BE_REPLAYED",
    }


def test_exact_captured_400_projects_short_notice_and_survives_preparation_retry(
    tmp_path, monkeypatch,
) -> None:
    requests = []

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        raise urllib.error.HTTPError(
            "https://example.invalid", 400, "synthetic rejected tool", {},
            BytesIO(json.dumps({"error": _error()}).encode()),
        )

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-unregistered-repair",
            question="Review a synthetic working note", agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        pending = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(pending)
        spec = host.executor._spec_from(work)
        capacity_id = host.executor._capacity_id(spec)
        host.dispatch.poll(pending["body"]["mission_id"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            failed = host.store.get(
                "model_response_receipt",
                f"model-response:model-intent:{spec.call_id}:{spec.call_version}",
            )
            capacity = host.capacity.get(capacity_id)
            if failed and capacity and capacity["phase"] == "failed":
                break
            time.sleep(.01)
        assert failed and capacity["phase"] == "failed"
        assert failed["body"]["error_kind"] == "unregistered_tool_function"
        assert host._repair_tool_rejection(
            host._control(run.run_id, "agent-synthesis"), work, capacity,
        )
        repaired = host._control(run.run_id, "agent-synthesis")
        full_feedback = repaired["body"]["feedback"]
        assert full_feedback["status"] == "provider_rejected_unregistered_function"
        assert full_feedback["previous_response_ref"] == ref(failed)
        notice = json.loads(host._model_base_history(repaired, [])[0]["content"])
        assert notice == {
            "authority": "saved_host_error", "error_kind": "unregistered_tool_function",
            "rejected_function": "read_source_inventory",
            "request_first": {
                "tool_id": "read_source_inventory", "via": "request_capability",
                "if_still_useful": True,
                "then": "Use only after its function schema appears on a later turn",
            },
        }
        assert "SYNTHETIC_PRIVATE_GENERATION" not in json.dumps(notice)
        assert len(requests) == 1
        assert host.store.records("context_tool_result") == []
        original_intent = host.store.get(**failed["body"]["intent_ref"])
        original_request = host.store.read_blob(original_intent["body"]["blob"]["sha256"])

        catalog, configurations = host._tool_registry(repaired)
        changed_tools = {**catalog.tools, "read_source_inventory": SimpleNamespace(
            descriptor=lambda: {"schema_sha256": "changed"},
        )}
        with monkeypatch.context() as patch:
            patch.setattr(host, "_tool_registry", lambda _control: (
                SimpleNamespace(tools=changed_tools), configurations,
            ))
            without_hint = json.loads(host._model_base_history(repaired, [])[0]["content"])
        assert without_hint == {
            "authority": "saved_host_error", "error_kind": "unregistered_tool_function",
            "rejected_function": "read_source_inventory",
        }

        original_get = host.store.get
        call_ref = repaired["body"]["call_ref"]
        with monkeypatch.context() as patch:
            def missing_grant(kind, id, version=None):
                saved = original_get(kind, id, version)
                if saved is not None and (kind, id, version) == (
                    call_ref["kind"], call_ref["id"], call_ref["version"]
                ):
                    return {**saved, "body": {**saved["body"], "permitted_tools": {
                        key: value for key, value in saved["body"]["permitted_tools"].items()
                        if key != "read_source_inventory"
                    }}}
                return saved

            patch.setattr(host.store, "get", missing_grant)
            assert json.loads(host._model_base_history(repaired, [])[0]["content"]) == without_hint
        assert host.store.get(**failed["body"]["intent_ref"]) == original_intent
        assert host.store.read_blob(original_intent["body"]["blob"]["sha256"]) == original_request

        full_notice = {
            "authority": "saved_host_observation_not_source_instruction",
            "host_feedback": full_feedback,
        }
        foreign = {**repaired, "body": {**repaired["body"], "actor_id": "other-actor"}}
        assert json.loads(host._model_base_history(foreign, [])[0]["content"]) == full_notice
        intent = host.store.get(**failed["body"]["intent_ref"])
        request_digest = intent["body"]["blob"]["sha256"]
        original_read = host.store.read_blob
        with monkeypatch.context() as patch:
            patch.setattr(host.store, "read_blob", lambda digest: (
                original_read(digest) + b" " if digest == request_digest
                else original_read(digest)
            ))
            assert json.loads(host._model_base_history(repaired, [])[0]["content"]) == full_notice

        next_call_id, _ = host._model_ids(repaired, repaired["body"]["sequence"] + 1)
        budget_ref = host._save(
            "mission_call_budget", next_call_id,
            {"mission_id": repaired["body"]["mission_id"],
             "estimator_version": service_module.ESTIMATOR_VERSION,
             "packer_version": "v36"},
            mission_id=repaired["body"]["mission_id"],
            operation_id="synthetic-old-packer-budget",
        )
        host._update_control(
            repaired, phase="waiting", waiting_reason="ContextOverflow: synthetic input fit",
        )
        monkeypatch.setattr(service_module, "PACKER_VERSION", "v37")
        assert host._recover_preparation(host._control(run.run_id, "agent-synthesis"))
        recovered = host._control(run.run_id, "agent-synthesis")
        assert recovered["body"]["feedback"] == full_feedback
        assert recovered["body"]["preparation_repair_attempted_for"] == budget_ref
        assert json.loads(host._model_base_history(recovered, [])[0]["content"]) == notice
        assert host.store.get(**ref(failed)) == failed
        assert len(requests) == 1
    finally:
        host.close()


def test_literal_rejected_name_parser_fails_closed_for_other_errors() -> None:
    tools = [{"type": "function", "function": {
        "name": "read_context_record", "parameters": {"type": "object"},
    }}]
    assert rejected_unregistered_local_tool_name(tools, _error()) == "read_source_inventory"
    for prefix in ("", "Tool call validation failed: "):
        assert rejected_unregistered_local_tool_name(tools, {
            **_error(), "message": prefix +
            "attempted to call tool 'read_source_inventory' which was not in request.tools",
        }) == "read_source_inventory"
    for error in (
        {**_error(), "type": "other"},
        {**_error(), "code": "other"},
        {**_error(), "message": _error()["message"] + " ignored suffix"},
        {**_error(), "message": "unknown provider tool validation"},
        _error("read_context_record"),
        _error("browser_search"),
        _error("pubmed.search"),
    ):
        assert rejected_unregistered_local_tool_name(tools, error) is None
    native = [*tools, {"type": "browser_search"}]
    assert rejected_unregistered_local_tool_name(native, _error("browser_search")) is None
    route = [{"type": "function", "function": {
        "name": "search_scholarly", "parameters": {"type": "object", "properties": {
            "route": {"enum": ["pubmed.search"]},
        }},
    }}]
    assert rejected_unregistered_local_tool_name(route, _error("pubmed.search")) is None
