"""A later direction and a saved peer read survive the same model continuation."""

from __future__ import annotations

import json
import time

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import answer, service, tool
from tests.mission.test_user_input_delivery import _command, _until


def test_post_tool_direction_and_peer_reader_result_share_serialized_request(
    tmp_path, monkeypatch,
):
    direction = (
        "The reviewed synthetic inventory changed after the first pass. "
        "Separate the original count, the returned quantity, and the amount "
        "reserved for training before giving a distributable total. " * 5
    )
    question = "Check the synthetic inventory amendment exactly."
    saved = {}
    packets = []

    def _transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        index = json.loads(packet["messages"][-1]["content"])
        if len(packets) == 1:
            saved["role_policy"] = packet["messages"][0]["content"]
            saved["direction"] = host.submit_user_input(
                _command(host, run.run_id, direction)
            )
            target = host._control(run.run_id, "agent-synthesis")
            sender = host._control(run.run_id, "agent-critique")
            task = host.store.get("board_proposal", target["body"]["task_id"])
            sent = host.mailbox.send_request(
                "combined-peer-request", mission_id=target["body"]["mission_id"],
                request_id="combined-peer-request", message_id="combined-peer-message",
                requester_id=sender["body"]["actor_id"],
                recipient_id=target["body"]["actor_id"], question=question,
                expected_artifact="Synthetic peer check", cause_ref=ref(task),
                payload={"question": question}, task_ref=ref(task),
            )
            assert sent["status"] == "accepted"
            host.mailbox.mark_delivered(
                "combined-peer-delivery", mission_id=target["body"]["mission_id"],
                message_id="combined-peer-message",
            )
            saved["message_ref"] = ref(host.store.get("mail_message", "combined-peer-message"))
            return tool("request_capability", {"tool_id": "read_context_record"}, 1)
        assert packet["messages"][0]["content"] == saved["role_policy"]
        assert index["current_user_direction"]["text"] == direction
        assert index["current_user_direction"]["ref"] == saved["direction"].input_ref.model_dump()
        if not saved.get("read_submitted"):
            assert saved["message_ref"] in index.get("message_refs", []) or any(
                item.get("ref") == saved["message_ref"]
                for item in index.get("omitted", {}).get("items", [])
            ), index.get("omitted")
            assert "read_context_record" in {
                item["function"]["name"] for item in packet["tools"]
            }
            saved["read_submitted"] = True
            return tool("read_context_record", {
                "inclusion_id": index["older_records"]["inclusion_id"],
                **saved["message_ref"], "offset": 0, "limit": 2000,
            }, 2)
        assert len(packets) == 3
        peer = next(
            json.loads(message["content"])
            for message in reversed(packet["messages"])
            if message.get("role") == "tool" and message.get("name") == "read_context_record"
        )
        assert peer["ref"] == saved["message_ref"]
        assert json.loads(peer["text"])["payload"] == {"question": question}
        assert direction in request.data.decode()
        saved["combined_packet"] = request.data
        return answer("The synthetic inventory correction and peer request were both read.", 3)

    def transport(request, *, timeout):
        try:
            return _transport(request, timeout=timeout)
        except BaseException as exc:
            saved.setdefault("transport_errors", []).append(repr(exc))
            raise

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="combined-direction-peer-reader",
            question="Prepare a synthetic inventory note.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        critic = host._control(run.run_id, "agent-critique")
        host._update_control(critic, phase="retry_at", retry_at=time.time() + 3600,
                             waiting_reason="Synthetic independent peer is idle")
        try:
            _until(host, run.run_id, lambda: "combined_packet" in saved, seconds=20)
        except AssertionError as exc:
            raise AssertionError(
                f"{exc}; packets={len(packets)}; errors={saved.get('transport_errors')}; "
                f"phase={host._control(run.run_id, 'agent-synthesis')['body']['phase']}"
            ) from exc
        assert len(packets) == 3
        assert not saved.get("transport_errors")
    finally:
        host.close()
