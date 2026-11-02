"""New numerical capabilities remain pinned, callable and durable after selection.

Injected responses check the real request, dispatch and saved-result boundaries;
they do not establish natural model selection or scientific design validity.
"""

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission.analysis_extended import (
    EXTENDED_ANALYSIS_TOOL_DESCRIPTIONS,
    EXTENDED_ANALYSIS_TOOL_SCHEMAS,
)
from ora.mission.analysis_tools import ANALYSIS_TOOL_SCHEMAS
from ora.state import StateStore
from tests.mission.test_research_tool_integration import _catalog_names, _last_result, _names
from tests.mission.test_service import answer, drain, service, tool


def pairs(values):
    return [{"pair_id": f"pair-{index}", "value_a": a, "value_b": b}
            for index, (a, b) in enumerate(values)]


CASES = [
    ("compare_summary_statistics", {
        "group_a": {"n": 10, "mean": 4, "sample_sd": 2},
        "group_b": {"n": 10, "mean": 2, "sample_sd": 2}, "alternative": "two-sided",
    }, "mean_difference_a_minus_b", "2"),
    ("analyze_contingency", {
        "event_a": 10, "nonevent_a": 90, "event_b": 20, "nonevent_b": 80,
        "operation": "risk_ratio", "zero_cell_policy": "reject",
    }, "estimate", "0.5"),
    ("analyze_paired_values", {
        "pairs": pairs([(3, 1), (6, 2), (5, 2)]), "alternative": "two-sided",
    }, "mean_difference_a_minus_b", "3"),
    ("correlate_values", {
        "pairs": pairs([(1, 1), (2, 2), (3, 4), (4, 3)]),
        "method": "pearson", "alternative": "two-sided",
    }, "coefficient_r", "0.8"),
    ("adjust_pvalues", {
        "family_id": "synthetic-family", "alpha": 0.05,
        "method": "benjamini_hochberg",
        "tests": [{"test_id": f"test-{index}", "p_value": value}
                  for index, value in enumerate([0.01, 0.04, 0.03])],
    }, "n_tests", 3),
    ("describe_values", {"values": [1, 2, 3, 4], "quantile_method": "linear"},
     "median", "2.5"),
    ("convert_values_units", {
        "values": [1, 2.5], "from_unit": "gram", "to_unit": "milligram",
    }, "converted_values", ["1000", "2500"]),
]


@pytest.mark.parametrize("name,arguments,result_field,expected", CASES)
def test_selected_v2_tool_reaches_model_and_survives_reopen(
    tmp_path, monkeypatch, name, arguments, result_field, expected,
):
    requests = []
    delivered = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        requests.append(packet)
        step = len(requests)
        if step == 1:
            assert name in _catalog_names(packet)
            assert name not in _names(packet)
            return tool("request_capability", {"tool_id": name}, step)
        declaration = next(row["function"] for row in packet["tools"]
                           if row.get("function", {}).get("name") == name)
        assert declaration["parameters"] == EXTENDED_ANALYSIS_TOOL_SCHEMAS[name]
        assert declaration["description"] == EXTENDED_ANALYSIS_TOOL_DESCRIPTIONS[name]
        if step == 2:
            return tool(name, arguments, step)
        assert step == 3
        result = _last_result(packet, name)
        assert result[result_field] == expected
        assert result["source_dependency_refs"] == []
        assert len(result["input_sha256"]) == 64
        assert result["assumptions_not_checked"]
        delivered.append(result)
        return answer("The supplied synthetic calculation is complete; design is unverified.", step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"synthetic-v2-{name}", question="Check the supplied synthetic calculation.",
            agent_ids=["agent-synthesis"],
        ))
        assert host._run(run.run_id)["body"]["analysis_tool_version"] == 2
        drain(host, run.run_id)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(requests) == 3
    finally:
        host.close()
    reopened = StateStore(tmp_path, read_only=True)
    results = [json.loads(reopened.read_blob(row["body"]["blob"]["sha256"]))
               for row in reopened.records("context_tool_result")]
    saved = [json.loads(row["content"]) for row in results if row["name"] == name]
    assert saved == delivered


@pytest.mark.parametrize("policy", [None, 1, 2])
def test_saved_analysis_policy_controls_discovery_grants_and_attachment(tmp_path, monkeypatch, policy):
    host = service(tmp_path, monkeypatch, lambda *_a, **_k: pytest.fail("No model dispatch"))
    try:
        run = host.start(LiveStartRequest(
            request_id=f"synthetic-pinned-analysis-{policy}", question="Inspect pinned capabilities.",
            agent_ids=["agent-synthesis"],
        ))
        row = host._run(run.run_id)
        body = dict(row["body"])
        if policy is None:
            body.pop("analysis_tool_version")
        else:
            body["analysis_tool_version"] = policy
        host.store.transact("synthetic-analysis-policy", {}, lambda tx: tx.put(
            row["kind"], row["id"], body, expected_version=row["version"]
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        call = host.store.get(**host._control(run.run_id, "agent-synthesis")["body"]["call_ref"])
        intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        expected = (set(ANALYSIS_TOOL_SCHEMAS) if policy in {1, 2} else set())
        if policy == 2:
            expected |= EXTENDED_ANALYSIS_TOOL_SCHEMAS.keys()
        numerical = set(ANALYSIS_TOOL_SCHEMAS) | EXTENDED_ANALYSIS_TOOL_SCHEMAS.keys()
        assert numerical & _catalog_names(packet) == expected
        assert numerical & call["body"]["permitted_tools"].keys() == expected
        assert numerical.isdisjoint(_names(packet))
        assert "request_capability" in _names(packet)
    finally:
        host.close()
