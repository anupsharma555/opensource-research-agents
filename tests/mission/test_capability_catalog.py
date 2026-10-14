"""The model sees a compact, role-granted capability index on every turn."""

from __future__ import annotations

import json
import urllib.error
from copy import deepcopy
from dataclasses import replace
from io import BytesIO

import pytest

from ora.agents.capabilities import COMMON_TOOL_IDS, Capability, content_hash
from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.coordination.executor import WorkSpec
from ora.mission import service as service_module
from ora.mission.service import TOOL_PURPOSES, UNHANDLED_COMMON_TOOLS
from ora.mission.tool_catalog import TOOL_DESCRIPTIONS, TOOL_SCHEMAS, tool_contract_matches
from ora.models.session import ModelSession
from tests.mission.test_service import answer, drain, service, tool


def _index(instructions: str) -> dict:
    marker = "Role capability catalog: "
    assert instructions.count(marker) == 1
    start = instructions.index(marker) + len(marker)
    value, _end = json.JSONDecoder().raw_decode(instructions[start:])
    return value


def test_tool_contract_comparison_ignores_only_valid_description_changes():
    saved = Capability("synthetic", "1", {
        "type": "object", "properties": {
            "description": {"type": "string", "description": "Earlier wording"},
            "items": {"type": "array", "items": {"type": "string"}},
        }, "required": ["description"], "additionalProperties": False,
    }, "synthetic:read", implementation_ref="build:synthetic").descriptor()
    original = deepcopy(saved)
    current = deepcopy(saved)
    current["description"] = "Clearer tool purpose"
    current["schema"]["description"] = "Clearer object explanation"
    current["schema"]["properties"]["description"]["description"] = "Clearer parameter explanation"
    current["schema"]["properties"]["items"]["items"]["description"] = "Clearer item explanation"
    current["schema_sha256"] = content_hash(current["schema"])
    assert tool_contract_matches(current, saved)
    assert saved == original
    for key, value in {
        "permission": "synthetic:write", "implementation_ref": "build:changed",
        "version": "2", "execution_model": "provider_builtin", "side_effects": "external",
        "schema_sha256": "invalid",
    }.items():
        assert not tool_contract_matches({**current, key: value}, saved)
    changed_schema = deepcopy(current)
    changed_schema["schema"]["properties"]["description"]["type"] = "integer"
    changed_schema["schema_sha256"] = content_hash(changed_schema["schema"])
    assert not tool_contract_matches(changed_schema, saved)


def test_old_prepared_call_can_select_a_tool_after_description_updates(tmp_path, monkeypatch):
    old_host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: pytest.fail(
        "Only prepare the historical request"))
    try:
        for name in (*sorted(COMMON_TOOL_IDS), "calculate"):
            item = old_host.catalog.tools[name]
            old_host.catalog.tools[name] = replace(item, description="")
        run = old_host.start(LiveStartRequest(
            request_id="saved-description-compatibility", question="Calculate one plus one.",
            agent_ids=["agent-synthesis"],
        ))
        control = old_host._control(run.run_id, "agent-synthesis")
        assert old_host._schedule_model(control)
        control = old_host._control(run.run_id, "agent-synthesis")
        pointer = control["body"]["call_ref"]
        intent = old_host.store.get("model_request_intent", f"model-intent:{pointer['id']}:{pointer['version']}")
        original_request = old_host.store.read_blob(intent["body"]["blob"]["sha256"])
    finally:
        old_host.close()

    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            assert request.data == original_request
            return tool("request_capability", {"tool_id": "calculate"}, 1)
        selection = next(json.loads(item["content"]) for item in packet["messages"]
                         if item.get("role") == "tool" and item.get("name") == "request_capability")
        assert selection["status"] == "selected_for_next_turn"
        calculation = next(item["function"] for item in packet["tools"]
                           if item["function"]["name"] == "calculate")
        assert calculation["description"] == TOOL_DESCRIPTIONS["calculate"]
        return answer("One plus one equals two.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        drain(host, run.run_id)
        assert len(packets) == 2
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert host.store.get("model_request_intent", intent["id"]) == intent
        assert host.store.read_blob(intent["body"]["blob"]["sha256"]) == original_request
    finally:
        host.close()


def test_every_available_tool_has_a_label_and_serialized_description(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: pytest.fail(
        "Catalog inspection does not call a provider"))
    try:
        expected = (COMMON_TOOL_IDS | TOOL_SCHEMAS.keys()) - UNHANDLED_COMMON_TOOLS
        for catalog in (host.catalog, host.legacy_catalog):
            descriptors = {name: catalog.tools[name].descriptor() for name in expected}
            assert all(TOOL_PURPOSES[name].strip() for name in expected)
            assert all(descriptors[name].get("description", "").strip() for name in expected)
            serialized = ModelSession._tools({"body": {
                "exposed_tools": sorted(expected - {"browser_search"}),
                "permitted_tools": descriptors,
            }})
            assert {item["function"]["name"] for item in serialized} == expected - {"browser_search"}
            assert all(item["function"]["description"] ==
                       descriptors[item["function"]["name"]]["description"] for item in serialized)
            assert ModelSession._tools({"body": {
                "exposed_tools": ["browser_search"], "permitted_tools": descriptors,
            }}) == ({"type": "browser_search"},)
    finally:
        host.close()


def test_common_descriptions_reach_an_actual_request_with_unchanged_tool_access(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        functions = {item["function"]["name"]: item["function"] for item in packet["tools"]}
        for name in ("board_read", "peer_request", "discover_capabilities", "request_capability"):
            assert functions[name]["description"] == TOOL_DESCRIPTIONS[name]
        notice = next(json.loads(item["content"]) for item in packet["messages"]
                      if item.get("role") == "user"
                      and "trusted_host_attached_tool_availability" in item["content"])
        assert set(notice["call_now_local_functions"]) == set(functions)
        assert not UNHANDLED_COMMON_TOOLS.intersection(functions)
        assert "write_note" not in functions
        if len(packets) == 1:
            return tool("board_read", {}, 1)
        result = next(json.loads(item["content"]) for item in packet["messages"]
                      if item.get("role") == "tool" and item.get("name") == "board_read")
        assert set(result) == {"open_work", "optional_work"}
        return answer("The current open work was reviewed.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="common-description-delivery", question="Review the currently open work.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert len(packets) == 2
        assert host._run(run.run_id)["body"]["status"] == "completed"
    finally:
        host.close()


@pytest.mark.parametrize("mode", ["memo", "source", "explicit_request"])
def test_source_reader_schema_follows_selected_evidence_or_explicit_request(tmp_path, monkeypatch, mode):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: pytest.fail(
        "This check only prepares requests"))
    try:
        run = host.start(LiveStartRequest(
            request_id=f"source-reader-schema-{mode}", question="Summarize the available record",
            evidence_text="A synthetic source records 12 observations." if mode == "source" else "",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        if mode == "explicit_request":
            host._update_control(control, requested_capabilities=["read_source_window"])
            control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        control = host._control(run.run_id, "agent-synthesis")
        call = host.store.get(**control["body"]["call_ref"])
        intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        attached = {item["function"]["name"] for item in packet["tools"]}
        notice = next(json.loads(item["content"]) for item in packet["messages"]
                      if item.get("role") == "user"
                      and "trusted_host_attached_tool_availability" in item["content"])
        assert ("read_source_window" in attached) is (mode != "memo")
        assert ("read_source_window" in notice["request_first_local_functions"]) is (mode == "memo")
        assert set(notice["call_now_local_functions"]) == attached
        if mode == "memo":
            config = host.store.get("agent_configuration", call["body"]["configuration_id"], 1)
            recovering, _ = host._exposed_tools(control, config, source_recovery=True)
            assert "read_source_window" in recovering
    finally:
        host.close()


def test_research_peers_can_read_shared_work_without_requesting_access(tmp_path, monkeypatch):
    packets = []
    reads_by_peer = set()

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        peer = packet["messages"][0]["content"].split("\n", 1)[0]
        attached = {item["function"]["name"] for item in packet["tools"]}
        # Reproduce provider validation at the actual request boundary.
        assert "board_read" in attached
        availability = next(json.loads(item["content"]) for item in packet["messages"]
                            if item.get("role") == "user"
                            and "trusted_host_attached_tool_availability" in item["content"])
        assert "board_read" in availability["call_now_local_functions"]
        if peer not in reads_by_peer:
            reads_by_peer.add(peer)
            return tool("board_read", {}, len(packets))
        result = next(json.loads(item["content"]) for item in packet["messages"]
                      if item.get("role") == "tool" and item.get("name") == "board_read")
        assert set(result) == {"open_work", "optional_work"}
        assert isinstance(result["open_work"], list)
        assert isinstance(result["optional_work"], list)
        assert "write_note" not in attached  # Unrelated optional tools remain deferred.
        return answer("Reviewed the shared work; no scientific conclusion is claimed.", len(packets))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="direct-shared-work-read",
            question="Review the current shared work from your respective roles.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        drain(host, run.run_id)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(reads_by_peer) == 2 and len(packets) == 4
        assert len(host.store.records("report")) == 2
        assert all(not row["body"].get("requested_capabilities")
                   for row in host._controls(run.run_id))
    finally:
        host.close()


def test_critic_can_request_granted_note_tool_without_discovery(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        instructions = packet["messages"][0]["content"]
        paragraphs = instructions.split("\n\n")
        assert paragraphs[0].startswith("You are ")
        assert paragraphs[1].startswith("CALL NOW: ")
        assert "REQUEST FIRST: " in paragraphs[1]
        assert "NEXT TURN: " in paragraphs[1]
        assert "DATA FIRST: " in paragraphs[1]
        assert "request_first_local_functions" in paragraphs[1]
        assert "requesting access does not create that result" in paragraphs[1]
        assert instructions.count("CALL NOW: ") == 1
        assert instructions.index("NEXT TURN: ") < instructions.index(
            "Work on the saved question"
        ) < instructions.index("Role capability catalog: ")
        assert "call request_capability with the exact tool_id" in paragraphs[1]
        assert "Call an attached local function directly; do not request it just to use it" in paragraphs[1]
        assert "attached schemas show what is callable now" in paragraphs[1]
        assert "Wait for selected_for_next_turn and the tool's attached schema" in paragraphs[1]
        assert "browser_search, request_capability selects a native provider turn" in paragraphs[1]
        assert "never call browser_search as a local function" in instructions
        assert "Local tool requests persist across skill release until release_capability" in instructions
        index = _index(instructions)
        tools = index["tools"]
        skills = index["skills"]
        exposed = {item["function"]["name"] for item in packet["tools"]}
        availability = next(json.loads(item["content"]) for item in packet["messages"]
                            if item.get("role") == "user"
                            and "trusted_host_attached_tool_availability" in item["content"])
        assert tools["write_note"] == TOOL_PURPOSES["write_note"]
        assert all(name not in tools for name in UNHANDLED_COMMON_TOOLS)
        assert skills["study-appraisal"]["state"] == "available_to_activate"
        assert skills["study-appraisal"]["purpose"]
        assert set(skills["study-appraisal"]["tools_on_activation"]) == {
            "write_note", "read_note", "search_notes",
        }
        assert "Skills provide reusable workflow guidance; tools perform individual operations" in instructions
        assert "not merely to call an already available tool" in instructions
        assert skills["evidence-linking"]["state"] == "active"
        assert "# Study appraisal" not in instructions
        assert "properties" not in json.dumps(index)
        if step == 1:
            assert "write_note" not in exposed
            assert "write_note" in availability["request_first_local_functions"]
            return tool("request_capability", {"tool_id": "write_note"}, step)
        assert step == 2 and "write_note" in exposed
        assert "write_note" not in availability["request_first_local_functions"]
        assert "write_note" in availability["call_now_local_functions"]
        selection = next(json.loads(item["content"]) for item in packet["messages"]
                         if item.get("role") == "tool"
                         and item.get("name") == "request_capability")
        assert selection["status"] == "selected_for_next_turn"
        assert "already_attached" not in selection
        return answer("A provisional methods review needs the underlying study reports.", step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="catalog-direct-tool-request",
            question="Review the methods of the supplied candidate studies",
            agent_ids=["agent-critique"],
        ))
        drain(host, run.run_id)
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        assert len(packets) == 2
        control = host._control(run.run_id, "agent-critique")
        granted = set(host._configuration(control)["body"]["permitted_tools"])
        visible_catalog = set(_index(packets[0]["messages"][0]["content"])["tools"])
        assert visible_catalog == granted - UNHANDLED_COMMON_TOOLS
    finally:
        host.close()


def test_rejected_tool_now_attached_has_direct_call_guidance(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        attached = {item["function"]["name"] for item in packet["tools"]}
        if len(packets) == 1:
            assert "board_read" not in attached
            error = {"error": {"type": "invalid_request_error", "code": "tool_use_failed",
                               "message": "attempted to call tool 'board_read' which was not in request.tools"}}
            raise urllib.error.HTTPError("https://example.invalid", 400, "bad tool", {},
                                         BytesIO(json.dumps(error).encode()))
        assert "board_read" in attached
        if len(packets) == 2:
            notices = [json.loads(item["content"]) for item in packet["messages"]
                       if item.get("role") == "user"]
            error = next(item for item in notices if item.get("authority") == "saved_host_error")
            assert error["rejected_function"] == "board_read"
            assert error["call_now"]["tool_id"] == "board_read"
            assert "request_first" not in error
            return tool("board_read", {}, 2)
        assert len(packets) == 3
        assert any(item.get("name") == "board_read" and item.get("role") == "tool"
                   for item in packet["messages"])
        return answer("The saved work was inspected; this is a synthetic control.", 3)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="current-menu-after-old-rejection", question="Check the current shared work.",
            agent_ids=["agent-synthesis"],
        ))
        # Preserve an older pinned request whose menu predates direct board access.
        with monkeypatch.context() as patch:
            patch.setattr(service_module, "CORE_RESEARCH_TOOLS",
                          service_module.CORE_RESEARCH_TOOLS - {"board_read"})
            assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        drain(host, run.run_id)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(packets) == 3
        assert not host.store.records("mission_capability_selection")
    finally:
        host.close()


def test_requesting_an_attached_local_tool_reports_direct_call_without_changing_selection(
    tmp_path, monkeypatch,
):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        attached = {item["function"]["name"] for item in packet["tools"]}
        assert "search_scholarly" in attached
        if step == 1:
            return tool("request_capability", {"tool_id": "search_scholarly"}, step)
        if step == 2:
            selection = next(json.loads(item["content"]) for item in packet["messages"]
                             if item.get("role") == "tool"
                             and item.get("name") == "request_capability")
            assert selection["status"] == "selected_for_next_turn"
            assert selection["tool_id"] == "search_scholarly"
            assert selection["already_attached"] is True
            assert "already attached to the request" in selection["next_action"]
            assert "Call it directly" in selection["next_action"]
            return tool("release_capability", {"tool_id": "search_scholarly"}, step)
        assert step == 3
        assert "search_scholarly" in attached  # A core tool survives release.
        return answer("A synthetic abstract has not yet been inspected.", step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="catalog-attached-selection",
            question="Review a synthetic paper identifier", agent_ids=["agent-critique"],
        ))
        drain(host, run.run_id)
        assert len(packets) == 3
        control = host._control(run.run_id, "agent-critique")["body"]
        assert "search_scholarly" not in control["requested_capabilities"]
        selections = host.store.records("mission_capability_selection")
        assert [item["body"]["action"] for item in selections] == [
            "request_capability", "release_capability",
        ]
        assert selections[0]["body"]["next_requested_capabilities"] == ["search_scholarly"]
        assert selections[1]["body"]["next_requested_capabilities"] == []
    finally:
        host.close()


def test_role_catalog_lists_active_mandatory_skill_and_supported_tools(tmp_path, monkeypatch):
    seen = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        seen.append(packet)
        return answer("A source-bound preliminary answer.", 1)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="catalog-mandatory-skill", question="Summarize this synthetic question",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert len(seen) == 1
        instructions = seen[0]["messages"][0]["content"]
        index = _index(instructions)
        assert list(index["skills"]) == ["evidence-linking", "general-web-discovery", "task-planning"]
        assert index["skills"]["task-planning"]["state"] == "available_to_activate"
        assert index["skills"]["general-web-discovery"]["state"] == "active"
        assert "# General web discovery" in instructions
        assert index["skills"]["evidence-linking"]["state"] == "active"
        assert index["skills"]["evidence-linking"]["purpose"]
        assert "# Evidence linking" in instructions
        names = list(index["tools"])
        assert names == sorted(names)
        assert "fetch_source" in names and "claim_work" not in names
    finally:
        host.close()


def test_discovery_distinguishes_callable_tools_from_requestable_tools(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("discover_capabilities", {}, 1)
        result = next(json.loads(item["content"])
                      for item in packet["messages"]
                      if item.get("role") == "tool"
                      and item.get("name") == "discover_capabilities")
        assert "request_capability" in result["exposed"]
        assert "search_saved_passages" in result["granted"]
        assert "search_saved_passages" not in result["exposed"]
        assert "search_saved_passages" in result["requestable"]
        assert not set(result["requestable"]) & set(result["not_handled_here"])
        assert "inspect_hit" not in result["requestable"]
        assert result["not_ready"]["inspect_hit"] == "Get an eligible saved search result first."
        assert "Use an exposed local function directly" in result["next_action"]
        assert "first call request_capability" in result["next_action"]
        assert "provider-managed native turn" in result["next_action"]
        return answer("The synthetic question needs source evidence.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="catalog-callable-vs-requestable",
            question="Review a synthetic candidate study", agent_ids=["agent-critique"],
        ))
        drain(host, run.run_id)
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        assert len(packets) == 2
    finally:
        host.close()


def test_catalog_updates_skill_state_after_activation(tmp_path, monkeypatch):
    states = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        instructions = packet["messages"][0]["content"]
        states.append(_index(instructions)["skills"]["study-appraisal"]["state"])
        if len(states) == 1:
            assert "# Study appraisal" not in instructions
            return tool("activate_skill", {"skill_id": "study-appraisal"}, 1)
        assert "# Study appraisal" in instructions
        assert "search_notes" in {item["function"]["name"] for item in packet["tools"]}
        return answer("Preliminary methods review pending source details.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="catalog-activation-state", question="Review candidate study methods",
            agent_ids=["agent-critique"],
        ))
        drain(host, run.run_id)
        assert states == ["available_to_activate", "active"]
    finally:
        host.close()


def test_streaming_request_does_not_advertise_native_browser(tmp_path, monkeypatch):
    monkeypatch.setitem(service_module.DEFAULT_MODEL_SETTINGS, "stream", True)
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: pytest.fail(
        "Streaming menu inspection must not dispatch a model"))
    try:
        run = host.start(LiveStartRequest(
            request_id="streaming-browser-menu", question="Inspect the current tool menu",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, requested_capabilities=["browser_search"])
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        control = host._control(run.run_id, "agent-synthesis")
        call = host.store.get(**control["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        notice = next(json.loads(item["content"]) for item in packet["messages"]
                      if item.get("role") == "user"
                      and "trusted_host_attached_tool_availability" in item["content"])
        assert packet["stream"] is True
        assert not any(item.get("type") == "browser_search" for item in packet["tools"])
        assert notice["request_first_native_tools"] == []
        assert "browser_search" in notice["requested_but_not_attached"]
        assert "browser_search" not in host._requestable_tools(control, call["body"])

        requested = {"id": "streaming-browser-selection", "name": "request_capability",
                     "arguments": {"tool_id": "browser_search"}}
        host._update_control(control, phase="pending_tool", tool_call=requested)
        control = host._control(run.run_id, "agent-synthesis")
        work_id = host._tool_id(control, requested["id"])
        host._update_control(control, work_id=work_id)
        control = host._control(run.run_id, "agent-synthesis")
        spec = WorkSpec(
            work_id=work_id, mission_id=control["body"]["mission_id"],
            run_id=run.run_id, proposal_ref=control["body"]["proposal_ref"],
            actor_id=control["body"]["actor_id"],
            configuration_ref=ref(host.store.get(
                "agent_configuration", call["body"]["configuration_id"], 1)),
            input_refs=(), handler_id="mission-tool", handler_version="1",
            kind="tool", lane="compute", call_id=call["id"],
            call_version=call["version"],
        )
        rejected = host._tool_work(spec).payload["result"]
        assert rejected["status"] == "rejected"
        assert rejected["field"] == "tool_id"
        assert "non-streaming runs" in rejected["reason"]
        assert rejected["external_action"] == "none"
    finally:
        host.close()
