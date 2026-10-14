"""Research tools complete their selected mission path with offline model replies."""

from __future__ import annotations

import json

from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import answer, drain, service, tool


def _catalog(packet: dict) -> dict:
    instructions = packet["messages"][0]["content"]
    marker = "Role capability catalog: "
    assert instructions.count(marker) == 1
    return json.JSONDecoder().raw_decode(instructions.split(marker, 1)[1])[0]


def _names(packet: dict) -> set[str]:
    return {item["function"]["name"] for item in packet["tools"]}


def _last_result(packet: dict, name: str) -> dict:
    message = next(
        item for item in reversed(packet["messages"])
        if item["role"] == "tool" and item["name"] == name
    )
    return json.loads(message["content"])


def test_calculation_is_selected_and_invalid_arithmetic_recovers(tmp_path, monkeypatch):
    seen = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        seen.append(packet)
        step = len(seen)
        if step == 1:
            assert "calculate" in _catalog(packet)["tools"]
            assert "calculate" not in _names(packet)
            return tool("request_capability", {"tool_id": "calculate"}, step)
        assert "calculate" in _names(packet)
        if step == 2:
            return tool("calculate", {
                "expression": "responders / enrolled * 100",
                "inputs": [
                    {"name": "responders", "value": 43},
                    {"name": "enrolled", "value": 60},
                ],
                "units": "%",
            }, step)
        if step == 3:
            result = _last_result(packet, "calculate")
            assert result["result"] == "71.66666666666666666666666667"
            assert result["validation_scope"] == (
                "calculated_from_supplied_values_not_source_validation"
            )
            return tool("calculate", {"expression": "1 / 0", "inputs": []}, step)
        assert step == 4
        rejected = _last_result(packet, "calculate")
        assert rejected["status"] == "rejected"
        return answer("The supplied ratio is approximately 71.7%; no source was checked.", step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="research-calculator-integration",
            question="Calculate a supplied synthetic percentage",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=20)
        assert host.state(run.run_id).snapshot.mission.status == "completed", (
            len(seen), host._control(run.run_id, "agent-synthesis")["body"]["waiting_reason"]
        )
        assert len(seen) == 4
    finally:
        host.close()


def test_saved_passage_search_leads_to_exact_source_window(tmp_path, monkeypatch):
    seen = []
    source_ref = None

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        seen.append(packet)
        step = len(seen)
        if step == 1:
            assert "search_saved_passages" in _catalog(packet)["tools"]
            assert "search_saved_passages" not in _names(packet)
            return tool("request_capability", {"tool_id": "search_saved_passages"}, step)
        assert "search_saved_passages" in _names(packet)
        if step == 2:
            return tool("search_saved_passages", {"query": "zephyr"}, step)
        if step == 3:
            search = _last_result(packet, "search_saved_passages")
            assert search["schema"] == "ora.saved_passage_search.v1"
            assert search["result_count"] == 1
            assert search["source_dependency_refs"] == [source_ref]
            hit = search["results"][0]
            assert hit["copy_ref"] == source_ref
            assert "zephyr" in hit["snippet"].casefold()
            next_request = hit["next_request"]
            assert next_request["source_id"] == source_ref["id"]
            assert next_request["source_version"] == source_ref["version"]
            return tool("read_source_window", {
                key: value for key, value in next_request.items() if key != "tool"
            }, step)
        assert step == 4
        window = _last_result(packet, "read_source_window")
        assert window["copy_ref"] == source_ref
        passage = host.store.get(**window["passages"][0]["passage_ref"])
        assert "zephyr" in passage["body"]["text"].casefold()
        return answer("The synthetic source window contains the zephyr observation.", step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="research-passage-search-integration",
            question="Locate zephyr in the supplied synthetic source",
            evidence_text="DOI: 10.1234/zephyr. A unique zephyr observation was retained.",
            agent_ids=["agent-synthesis"],
        ))
        source_ref = host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0]
        drain(host, run.run_id, limit=20)
        assert host.state(run.run_id).snapshot.mission.status == "completed", (
            len(seen), host._control(run.run_id, "agent-synthesis")["body"]["waiting_reason"]
        )
        assert len(seen) == 4
    finally:
        host.close()


def test_skill_resource_read_respects_optional_activation(tmp_path, monkeypatch):
    seen = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        seen.append(packet)
        step = len(seen)
        if step == 1:
            assert "read_skill_resource" in _catalog(packet)["tools"]
            assert "read_skill_resource" not in _names(packet)
            return tool("request_capability", {"tool_id": "read_skill_resource"}, step)
        assert "read_skill_resource" in _names(packet)
        if step == 2:
            return tool("read_skill_resource", {
                "skill_id": "evidence-linking", "resource": "example",
            }, step)
        if step == 3:
            page = _last_result(packet, "read_skill_resource")
            assert page["schema"] == "ora.skill_resource_page.v1"
            assert page["metadata"]["synthetic"] is True
            assert "not fresh source evidence" in page["qualification"]
            return tool("read_skill_resource", {
                "skill_id": "study-appraisal", "resource": "example",
            }, step)
        if step == 4:
            rejected = _last_result(packet, "read_skill_resource")
            assert rejected["status"] == "rejected"
            return tool("activate_skill", {"skill_id": "study-appraisal"}, step)
        if step == 5:
            assert _catalog(packet)["skills"]["study-appraisal"]["state"] == "active"
            return tool("read_skill_resource", {
                "skill_id": "study-appraisal", "resource": "example",
            }, step)
        assert step == 6
        page = _last_result(packet, "read_skill_resource")
        assert page["schema"] == "ora.skill_resource_page.v1"
        assert page["skill_id"] == "study-appraisal"
        assert page["metadata"]["synthetic"] is True
        return answer("The registered synthetic skill examples were read.", step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="research-skill-resource-integration",
            question="Inspect synthetic skill examples for a methods review",
            agent_ids=["agent-critique"],
        ))
        drain(host, run.run_id, limit=25)
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        assert len(seen) == 6
    finally:
        host.close()
