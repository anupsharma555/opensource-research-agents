"""New board reads expose no arguments without rewriting saved v1 calls."""

from __future__ import annotations

import json
import socket

from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import answer, drain, service


def _board_tool(packet: dict) -> dict:
    return next(item["function"] for item in packet["tools"]
                if item["function"]["name"] == "board_read")


def test_current_serialized_board_read_has_no_topic(tmp_path, monkeypatch):
    def no_socket(*_args, **_kwargs):
        raise AssertionError("Board schema test must stay offline")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", no_socket)
    seen = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        seen.append(_board_tool(packet))
        return answer("The synthetic question has no supplied evidence.", 1)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="board-read-no-arguments", question="Review a synthetic claim.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert seen
        assert seen[0]["parameters"] == {
            "type": "object", "properties": {}, "required": [],
            "additionalProperties": False,
        }
        control = host._control(run.run_id, "agent-synthesis")
        call = host.store.get(**control["body"]["call_ref"])
        assert call["body"]["permitted_tools"]["board_read"]["version"] == "2"
    finally:
        host.close()


def test_saved_v1_board_read_pin_keeps_its_serialized_schema(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packets.append(json.loads(request.data))
        return answer("The synthetic question has no supplied evidence.", 1)

    host = service(tmp_path, monkeypatch, transport)
    try:
        current = host.catalog, host.configurations
        host.catalog = host.board_read_v1_catalogs[0]
        host.configurations = host.board_read_v1_configurations[0]
        run = host.start(LiveStartRequest(
            request_id="board-read-saved-v1", question="Review a synthetic claim.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert packets
        old_tool = _board_tool(packets[0])
        assert old_tool["parameters"]["properties"] == {
            "topic": {"type": "string"},
        }
        control = host._control(run.run_id, "agent-synthesis")
        call = host.store.get(**control["body"]["call_ref"])
        pinned = call["body"]["permitted_tools"]["board_read"]
        assert pinned["version"] == "1"
        host.catalog, host.configurations = current
        catalog, configurations = host._tool_registry(control)
        assert catalog.tools["board_read"].descriptor() == pinned
        assert configurations.catalog is catalog
        assert host._configuration(control)["id"] == call["body"]["configuration_id"]
        assert _board_tool(packets[0]) == old_tool
    finally:
        host.close()
