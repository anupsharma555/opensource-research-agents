"""Numerical guidance appears only in saved requests with usable analysis tools."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission.analysis_extended import EXTENDED_ANALYSIS_TOOL_PURPOSES
from tests.mission.availability_assertions import catalog_tool_names
from tests.mission.test_capability_catalog import _index
from tests.mission.test_service import answer, drain, service

LABEL = "## Numerical checks"
PURPOSES = {
    **EXTENDED_ANALYSIS_TOOL_PURPOSES,
    "analyze_values": (
        "Descriptive summaries or independent Welch p-values, degrees of freedom "
        "and optional two-sided intervals"
    ),
    "convert_units": "Conversions among supported physical mass, length, time and volume units",
}


@pytest.mark.parametrize("case,question,expected_tools", [
    ("group_summary", "Summarize the supplied values 2, 4, 8, and 10.",
     set(PURPOSES)),
    ("unit_conversion", "Convert a supplied distance of 250 centimeters to meters.",
     set(PURPOSES)),
    ("legacy_v1", "Summarize the supplied values 2, 4, 8, and 10.",
     {"analyze_values", "convert_units"}),
    ("only_conversion", "Convert a supplied distance of 250 centimeters to meters.",
     {"convert_units"}),
    ("old_policy", "Summarize the supplied values 2, 4, 8, and 10.", set()),
    ("missing_catalog", "Convert a supplied distance of 250 centimeters to meters.",
     set()),
])
def test_saved_numerical_guidance_tracks_actual_tools_and_replays_exact_bytes(
    tmp_path, monkeypatch, case, question, expected_tools,
):
    preparing = service(tmp_path, monkeypatch, lambda *_a, **_k: pytest.fail(
        "Preparing the frozen request must not dispatch a model"
    ))
    try:
        run = preparing.start(LiveStartRequest(
            request_id=f"synthetic-numerical-guidance-{case}", question=question,
            agent_ids=["agent-synthesis"],
        ))
        control = preparing._control(run.run_id, "agent-synthesis")
        if case in {"old_policy", "legacy_v1"}:
            saved = preparing._run(run.run_id)
            body = {key: value for key, value in saved["body"].items()
                    if key != "analysis_tool_version"}
            if case == "legacy_v1":
                body["analysis_tool_version"] = 1
            preparing.store.transact("synthetic-pre-analysis-run", {}, lambda tx: tx.put(
                "mission_run", run.run_id, body, expected_version=saved["version"],
            ))
        elif case in {"only_conversion", "missing_catalog"}:
            catalog, _ = preparing._tool_registry(control)
            for name in PURPOSES:
                if case == "missing_catalog" or name != "convert_units":
                    catalog.tools.pop(name)

        assert preparing._schedule_model(control)
        call = preparing.store.get(**preparing._control(run.run_id, "agent-synthesis")[
            "body"]["call_ref"])
        intent = preparing.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        original_bytes = preparing.store.read_blob(intent["body"]["blob"]["sha256"])
        packet = json.loads(original_bytes)
        instructions = packet["messages"][0]["content"]
        assert packet["messages"][0]["role"] == "developer"
        catalog = _index(instructions)
        catalog_tools = catalog_tool_names(catalog)
        assert set(PURPOSES) & catalog_tools == expected_tools
        assert set(PURPOSES) & set(call["body"][
            "permitted_tools"]) == expected_tools
        attached = {item["function"]["name"] for item in packet["tools"]}
        assert expected_tools.isdisjoint(attached)
        assert "request_capability" in attached
        if expected_tools:
            assert instructions.count(LABEL) == 1
            section = instructions.split(LABEL + "\n", 1)[1].split("\n\n", 1)[0]
            assert len(section.split()) <= (100 if case in {"legacy_v1", "only_conversion"} else 180)
            assert expected_tools <= catalog_tools
            assert catalog["schema"] == "ora.role_capability_catalog.purposes.v2"
            assert {name: catalog["tools"][name] for name in expected_tools} == {
                name: PURPOSES[name] for name in expected_tools
            }
            assert "Applicable numerical tools and their purposes are listed" in section
            assert "discover_capabilities refreshes access status" in instructions
            assert "Available local tools:" not in section
            assert "nontrivial numerical claims" in section
            if case == "legacy_v1":
                assert "request its tool_id if the schema is not attached" in section
            else:
                assert "if unattached, call request_capability(tool_id) and wait for its schema" in section
            assert "Label unchecked numbers as estimates" in section
            assert "preserve its method, alternative, units and precision" in section
            assert "Match stated assumptions to the selected method" in section
            assert "does not validate source values, independence, study design" in section
        else:
            assert LABEL not in instructions
            assert "nontrivial numerical claims" not in instructions
    finally:
        preparing.close()

    dispatched = []

    def transport(request, *, timeout):
        dispatched.append(request.data)
        return answer("The synthetic numerical request remains bounded.", len(dispatched))

    resumed = service(tmp_path, monkeypatch, transport)
    try:
        drain(resumed, run.run_id)
        assert resumed._run(run.run_id)["body"]["status"] == "completed"
        assert dispatched == [original_bytes]
        assert resumed.store.read_blob(intent["body"]["blob"]["sha256"]) == original_bytes
    finally:
        resumed.close()
