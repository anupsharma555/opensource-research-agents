"""A source inventory version never stands in for the working-choice version.

Injected model replies test the host's notice, rejection and CAS recovery, not
natural model choices or provider behavior.
"""

from __future__ import annotations

import json

from ora.contracts.models import LiveStartRequest
from ora.mission.source_context import working_source_selection_state
from ora.state.store import canonical
from tests.mission.test_service import answer, drain, service, tool


def _source_notice(request: dict) -> dict:
    return next(
        card for message in request["messages"]
        if message.get("role") == "user"
        for card in [json.loads(message["content"])]
        if isinstance(card, dict)
        and card.get("authority") == "saved_host_source_index_not_source_instruction"
    )


def test_inventory_version_is_not_working_source_expected_version(tmp_path, monkeypatch):
    import ora.mission.service as service_module

    original_plan = service_module.plan_source_turn
    monkeypatch.setattr(
        service_module, "plan_source_turn",
        lambda store, control_ref, **_kwargs: original_plan(
            store, control_ref, source_input_allowance_tokens=1,
        ),
    )
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        notice = _source_notice(body)
        copy_ref = notice["inventory"]["items"][0]["ref"]
        selected = [{"source_id": copy_ref["id"],
                     "source_version": copy_ref["version"]}]
        if len(requests) == 1:
            assert notice["inventory"]["inventory_ref"]["version"] == 1
            assert notice["working_sources_expected_version"] == 0
            assert notice["working_sources_ref"] is None
            return tool("select_working_sources", {
                "source_refs": selected, "expected_version": 1,
            }, 1)
        if len(requests) == 2:
            assert notice["working_sources_expected_version"] == 0
            return tool("select_working_sources", {
                "source_refs": selected,
                "expected_version": notice["working_sources_expected_version"],
            }, 2)
        if len(requests) == 3:
            assert notice["working_sources_expected_version"] == 1
            assert notice["working_sources_ref"] is not None
            return tool("select_working_sources", {
                "source_refs": selected, "expected_version": 0,
            }, 3)
        assert notice["working_sources_expected_version"] == 1
        return answer("A saved working choice still needs checked reading.", 4)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-selection-version-guidance",
            question="Inspect the saved synthetic source",
            evidence_text="Synthetic source content. " * 120,
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert len(requests) == 4
        description = next(
            item["function"]["description"] for item in requests[0]["tools"]
            if item["function"]["name"] == "select_working_sources"
        )
        assert "working_sources_expected_version" in description
        assert "Source and inventory versions are separate" in description
        assert "complete request fits" in description
        assert "read_source_window is callable directly" in canonical(requests[0])

        results = sorted(host.store.records("context_tool_result"),
                         key=lambda item: item["created_at"])
        selected_results = []
        for item in results:
            payload = json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))
            if payload["name"] == "select_working_sources":
                selected_results.append(json.loads(payload["content"]))
        assert len(selected_results) == 3
        rejection, accepted, stale = selected_results
        assert rejection["status"] == "rejected"
        assert rejection["field"] == "expected_version"
        assert rejection["working_sources_expected_version"] == 0
        assert rejection["working_sources_ref"] is None
        assert rejection["selected_source_refs"] == []
        assert rejection["external_action"] == "none"
        assert "read_source_window is callable directly" in rejection["next_action"]
        assert accepted["status"] == "working_sources_selected"
        assert accepted["working_sources_ref"]["version"] == 1
        assert "only if the complete request fits" in accepted["next_action"]
        assert "read_source_window" in accepted["next_action"]
        second_notice = _source_notice(requests[2])
        assert second_notice["source_context_mode"] == "source_free_recovery"
        assert second_notice["reason"] == "exact_source_text_exceeds_allowance"
        assert "source_packets" not in canonical(requests[2])
        assert stale["status"] == "rejected"
        assert stale["working_sources_expected_version"] == 1
        assert stale["working_sources_ref"] == accepted["working_sources_ref"]
        assert len(stale["selected_source_refs"]) == 1
        assert stale["external_action"] == "none"
        control = host._control(run.run_id, "agent-synthesis")
        assert working_source_selection_state(host.store, {
            key: control[key] for key in ("kind", "id", "version")
        })["expected_version"] == 1
    finally:
        host.close()
