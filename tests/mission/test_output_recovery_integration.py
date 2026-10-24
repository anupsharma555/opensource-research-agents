"""A saved mission can continue repeated cutoffs without executing fragments."""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import MODEL, Response, drain, service, tool


def _reach_next_decision(host, run_id: str, expected_partials: int) -> dict:
    """Stop before dispatching the next model turn so the service can be reopened."""
    for _ in range(200):
        host._drive_run(host._run(run_id))
        control = host._control(run_id, "agent-synthesis")["body"]
        partials = [
            record for record in host.store.records("model_response_receipt")
            if record["body"]["status"] == "partial"
        ]
        if (
            len(partials) == expected_partials
            and control["phase"] == "ready_model"
            and (control.get("feedback") or {}).get("status") == "partial"
        ):
            return control
        time.sleep(0.01)
    pytest.fail(f"Cutoff {expected_partials} did not reach a saved next decision")


def test_repeated_visible_and_tool_cutoffs_reopen_then_publish_once(tmp_path, monkeypatch, request):
    sent_requests: list[dict] = []
    cutoff_payloads: list[dict] = []
    visible = ["Unfinished section one.", "Unfinished section two."]
    private_reasoning = "PRIVATE_REASONING_NEVER_REPLAY"
    incomplete_arguments = "INCOMPLETE_TOOL_ARGUMENTS_NEVER_EXECUTE"
    final_text = "A complete scoped contribution after three interrupted turns."

    def transport(request, *, timeout):
        body = json.loads(request.data)
        sent_requests.append(body)
        ordinal = len(sent_requests)
        assert body["model"] == MODEL
        assert body["reasoning_effort"] == "medium"
        assert body["include_reasoning"] is True
        encoded = json.dumps(body)
        assert private_reasoning not in encoded
        assert incomplete_arguments not in encoded

        if ordinal > 1:
            context = json.loads(body["messages"][-1]["content"])
            recovery = context["partial_visible_output"]
            assert recovery["response_ref"]["id"] == previous_response_ids[ordinal - 2]
            if ordinal <= 3:
                assert recovery["status"] == "available"
                assert recovery["text_preview"] == visible[ordinal - 2]
            else:
                assert recovery["status"] == "content_unavailable"
                assert "text_preview" not in recovery

        if ordinal <= 3:
            message = {
                "role": "assistant",
                "content": visible[ordinal - 1] if ordinal <= 2 else None,
                "reasoning": private_reasoning,
            }
            if ordinal == 3:
                message["tool_calls"] = [{
                    "id": "incomplete-publish", "type": "function",
                    "function": {
                        "name": "publish_result",
                        "arguments": '{"text":"' + incomplete_arguments,
                    },
                }]
            cutoff_payload = {
                "id": f"cutoff-{ordinal}", "model": MODEL,
                "choices": [{"message": message, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14},
            }
            cutoff_payloads.append(cutoff_payload)
            return Response(cutoff_payload)
        assert ordinal == 4
        return tool("publish_result", {
            "title": "Recovered contribution",
            "text": final_text,
            "citations": [],
            "limitations": ["Synthetic source-free fixture"],
        }, ordinal)

    opened_hosts = []

    def close_open_hosts():
        for opened in opened_hosts:
            if not opened._stop.is_set():
                opened.close()

    request.addfinalizer(close_open_hosts)
    previous_response_ids: list[str] = []
    host = service(tmp_path, monkeypatch, transport)
    opened_hosts.append(host)
    receipt = host.start(LiveStartRequest(
        request_id="three-cutoffs-and-reopen",
        question="Produce one complete scoped contribution",
        agent_ids=["agent-synthesis"],
    ))

    for ordinal in range(1, 4):
        control = _reach_next_decision(host, receipt.run_id, ordinal)
        response_ref = control["feedback"]["previous_response_ref"]
        previous_response_ids.append(response_ref["id"])
        saved = host.store.get(**response_ref)["body"]
        assert saved["finish_reason"] == "length"
        assert saved["response_id"] == f"cutoff-{ordinal}"
        assert saved["response_sha256"] == hashlib.sha256(
            json.dumps(cutoff_payloads[ordinal - 1]).encode()
        ).hexdigest()
        assert saved["usage"]["prompt_tokens"] == 5
        assert saved["usage"]["completion_tokens"] == 9
        assert saved["usage"]["total_tokens"] == 14
        assert saved["call_ref"]["id"] == host._control(
            receipt.run_id, "agent-synthesis"
        )["body"]["call_ref"]["id"]
        assert host.store.records("report") == []
        assert host.store.records("context_tool_result") == []
        assert host._run(receipt.run_id)["body"]["status"] == "active"
        host.close()
        host = service(tmp_path, monkeypatch, transport)
        opened_hosts.append(host)
        assert host._control(receipt.run_id, "agent-synthesis")["body"]["feedback"][
            "previous_response_ref"
        ] == response_ref

    drain(host, receipt.run_id, limit=30)
    assert len(sent_requests) == 4
    assert host.state(receipt.run_id).snapshot.mission.status == "completed"
    reports = host.store.records("report")
    assert len(reports) == 1 and reports[0]["body"]["body"] == final_text
    responses = host.store.records("model_response_receipt")
    assert len(responses) == 4
    assert [item["body"]["status"] for item in responses].count("partial") == 3
    assert [item["body"]["status"] for item in responses].count("tool_requested") == 1
    assert len({item["body"]["call_ref"]["id"] for item in responses}) == 4
    assert len({item["body"]["attempt_id"] for item in responses}) == 4
    assert len({item["body"]["task_id"] for item in responses}) == 1
    assert len({
        (item["body"]["configuration_ref"]["id"],
         item["body"]["configuration_ref"]["version"])
        for item in responses
    }) == 1
    configuration = host.store.get(**responses[0]["body"]["configuration_ref"])
    assert configuration["body"]["instance_id"] == host._control(
        receipt.run_id, "agent-synthesis"
    )["body"]["actor_id"]
    for response in responses:
        body = response["body"]
        intent = host.store.get(**body["intent_ref"])
        assert body["request_sha256"] == intent["body"]["request_sha256"]
    assert len(host.store.records("context_tool_result")) <= 1
    assert not any(
        incomplete_arguments in json.dumps(record["body"])
        for record in host.store.records("report") + host.store.records("context_tool_result")
    )
    host.close()
    reopened = service(tmp_path, monkeypatch, transport)
    opened_hosts.append(reopened)
    drain(reopened, receipt.run_id)
    assert len(sent_requests) == 4
    assert len(reopened.store.records("report")) == 1
    assert reopened.state(receipt.run_id).snapshot.mission.status == "completed"
    reopened.close()


def test_revoked_source_cutoff_reopens_without_source_or_derived_draft(
    tmp_path, monkeypatch, request,
):
    source_text = "REVOKED_SYNTHETIC_SOURCE has a source-only observation."
    derived_draft = "REVOKED_SOURCE_DERIVED_DRAFT must be checked before use."
    incomplete_arguments = "REVOKED_INCOMPLETE_TOOL_ARGUMENTS"
    final_text = "The earlier source is unavailable; no source finding is asserted."
    sent_requests: list[dict] = []
    opened_hosts = []

    def close_open_hosts():
        for opened in opened_hosts:
            if not opened._stop.is_set():
                opened.close()

    request.addfinalizer(close_open_hosts)

    def transport(request, *, timeout):
        body = json.loads(request.data)
        sent_requests.append(body)
        encoded = json.dumps(body)
        assert body["model"] == MODEL
        assert body["reasoning_effort"] == "medium"
        assert body["include_reasoning"] is True
        if len(sent_requests) == 1:
            assert source_text in encoded
            return Response({
                "id": "source-bound-cutoff", "model": MODEL,
                "choices": [{"message": {
                    "role": "assistant", "content": derived_draft,
                    "reasoning": "REVOKED_PRIVATE_REASONING",
                    "tool_calls": [{
                        "id": "revoked-fragment", "type": "function",
                        "function": {"name": "publish_result", "arguments":
                                     '{"text":"' + incomplete_arguments},
                    }],
                }, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14},
            })
        assert len(sent_requests) == 2
        assert source_text not in encoded
        assert derived_draft not in encoded
        assert incomplete_arguments not in encoded
        assert "REVOKED_PRIVATE_REASONING" not in encoded
        context = json.loads(body["messages"][-1]["content"])
        recovery = context.get("partial_visible_output")
        assert recovery is None or recovery["status"] == "reissue_required"
        if recovery is not None:
            assert "text_preview" not in recovery
        return tool("publish_result", {
            "title": "Qualified source-free continuation",
            "text": final_text,
            "citations": [],
            "limitations": ["Earlier source permission was revoked"],
        }, 2)

    host = service(tmp_path, monkeypatch, transport)
    opened_hosts.append(host)
    receipt = host.start(LiveStartRequest(
        request_id="source-cutoff-revoke-and-reopen",
        question="Give a bounded source finding only when permitted",
        agent_ids=["agent-synthesis"],
        evidence_text=source_text,
    ))
    control = _reach_next_decision(host, receipt.run_id, 1)
    partial_ref = control["feedback"]["previous_response_ref"]
    source_ref = control["source_refs"][0]
    actor_id = control["actor_id"]
    original_use = host.store.resolve(
        "source_use.copy", f"{source_ref['id']}:{source_ref['version']}"
    )
    assert original_use["body"]["policy_status"] == "granted"
    assert host.store.records("report") == []
    assert host.store.records("context_tool_result") == []

    host.store.transact(
        "revoke-source-after-cutoff", {},
        lambda tx: tx.put("source_use", original_use["id"], {
            **original_use["body"],
            "policy_status": "revoked",
            "allowed_uses": ["storage"],
        }, expected_version=original_use["version"]),
        mission_id=host._run(receipt.run_id)["body"]["mission_id"],
    )
    host.close()
    reopened = service(tmp_path, monkeypatch, transport)
    opened_hosts.append(reopened)
    assert reopened._control(receipt.run_id, "agent-synthesis")["body"][
        "feedback"
    ]["previous_response_ref"] == partial_ref
    drain(reopened, receipt.run_id, limit=30)

    assert len(sent_requests) == 2
    assert reopened.state(receipt.run_id).snapshot.mission.status == "completed"
    reports = reopened.store.records("report")
    assert len(reports) == 1 and reports[0]["body"]["body"] == final_text
    assert reports[0]["body"]["citations"] == []
    responses = reopened.store.records("model_response_receipt")
    assert len(responses) == 2
    assert {item["body"]["status"] for item in responses} == {"partial", "tool_requested"}
    final_response = next(item for item in responses if item["body"]["status"] == "tool_requested")
    final_intent = reopened.store.get(**final_response["body"]["intent_ref"])
    assert all(
        item["copy_ref"] != source_ref
        for item in final_intent["body"].get("source_decisions", [])
    )
    assert all(
        reopened.store.get(**item["body"]["configuration_ref"])["body"]["instance_id"]
        == actor_id for item in responses
    )
    assert all(
        reopened.store.get(**item["body"]["configuration_ref"])["body"]["model_id"]
        == MODEL for item in responses
    )
    assert all(
        reopened.store.get(**item["body"]["configuration_ref"])["body"][
            "reasoning_settings"
        ] == {"reasoning_effort": "medium", "include_reasoning": True}
        for item in responses
    )
    assert not any(
        incomplete_arguments in json.dumps(item["body"])
        for item in reports + reopened.store.records("context_tool_result")
    )
    reopened.close()
