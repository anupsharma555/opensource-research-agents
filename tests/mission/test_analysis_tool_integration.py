"""Follow advertised analysis tools through actual request schemas and saved outcomes.

Injected model replies keep this a mechanical mission/recovery check. It does
not establish natural tool selection or a scientifically appropriate analysis.
"""

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.analysis_tools import ANALYSIS_TOOL_SCHEMAS
from ora.state import StateStore
from tests.mission.test_research_tool_integration import _catalog_names, _last_result, _names
from tests.mission.test_service import answer, drain, service, tool


@pytest.mark.parametrize("name,arguments,expected", [
    ("analyze_values", {"operation": "welch_ttest", "group_a": [1, 2, 3, 4],
                        "group_b": [3, 4, 5, 6], "alternative": "two-sided"},
     {"operation": "welch_ttest", "group_a": {"n": 4, "mean": "2.5"}}),
    ("convert_units", {"value": 2.5, "from_unit": "gram", "to_unit": "milligram"},
     {"operation": "convert_units", "result": "2500"}),
])
def test_selected_tool_is_callable_executed_and_persisted_after_reopen(
    tmp_path, monkeypatch, name, arguments, expected,
):
    packets = []
    saved_result = None

    def transport(request, *, timeout):
        nonlocal saved_result
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        if step == 1:
            assert name in _catalog_names(packet)
            assert name not in _names(packet)
            return tool("request_capability", {"tool_id": name}, step)
        assert name in _names(packet)
        if step == 2:
            declared = next(row["function"] for row in packet["tools"]
                            if row["function"]["name"] == name)
            assert declared["parameters"] == ANALYSIS_TOOL_SCHEMAS[name]
            return tool(name, arguments, step)
        assert step == 3
        saved_result = _last_result(packet, name)
        for key, value in expected.items():
            if isinstance(value, dict):
                assert value.items() <= saved_result[key].items()
            else:
                assert saved_result[key] == value
        assert len(saved_result["input_sha256"]) == 64
        assert saved_result["source_dependency_refs"] == []
        assert saved_result["assumptions_not_checked"]
        return answer("Synthetic supplied values were computed; study assumptions are unverified.", step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(request_id=f"synthetic-{name}",
            question="Compute the prespecified synthetic values.", agent_ids=["agent-synthesis"]))
        drain(host, run.run_id)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(packets) == 3
        rows = [row for row in host.store.records("context_tool_result")
                if json.loads(host.store.read_blob(row["body"]["blob"]["sha256"]))["name"] == name]
        assert len(rows) == 1
        pointer = ref(rows[0])
    finally:
        host.close()
    reopened = StateStore(tmp_path)
    row = reopened.get(**pointer)
    message = json.loads(reopened.read_blob(row["body"]["blob"]["sha256"]))
    assert json.loads(message["content"]) == saved_result


def test_saved_run_without_analysis_version_does_not_gain_new_tools(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_k: pytest.fail("No provider dispatch"))
    try:
        run = host.start(LiveStartRequest(request_id="synthetic-old-analysis-policy",
            question="Preserve saved runtime permissions.", agent_ids=["agent-synthesis"]))
        row = host._run(run.run_id)
        body = {key: value for key, value in row["body"].items()
                if key != "analysis_tool_version"}
        host.store.transact("synthetic-old-runtime", {}, lambda tx: tx.put(
            row["kind"], row["id"], body, expected_version=row["version"]))
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        call = host.store.get(**host._control(run.run_id, "agent-synthesis")["body"]["call_ref"])
        assert not ANALYSIS_TOOL_SCHEMAS.keys() & call["body"]["permitted_tools"].keys()
    finally:
        host.close()
