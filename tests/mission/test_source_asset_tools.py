"""A normal peer can see figure tools only after selecting permitted HTML."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.board import BoardError, ref
from ora.coordination.executor import WorkSpec
from ora.evidence.intake import IntakeService
from ora.evidence.library import EvidenceLibrary
from ora.evidence.source_assets import list_source_assets
from ora.mission.figure_reading import FigureReader
from ora.state import StateError
from tests.evidence import test_document_ocr as document_fixture
from tests.evidence.test_source_assets import _png, _receipt
from tests.mission.test_inherited_public_source import _user_value
from tests.mission.test_service import MODEL, ROOT, Response, answer, drain, service, tool

HTML = (b'<html><body><figure><img src="/figure.png">'
        b'<figcaption>Figure A. Synthetic relationship.</figcaption>'
        b'</figure></body></html>')


def test_source_asset_tools_are_exposed_after_html_selection(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: answer("offline", 1))
    try:
        receipt = host.start(LiveStartRequest(
            request_id="synthetic-asset-availability",
            question="Inspect the saved figure", agent_ids=["agent-synthesis"],
        ))
        control = host._control(receipt.run_id, "agent-synthesis")
        config = host._configuration(control)
        before, _ = host._exposed_tools(control, config)
        assert "list_source_assets" not in before
        assert "fetch_source_asset" not in before

        monkeypatch.setattr(document_fixture, "SCOPE", {
            "mission_id": control["body"]["mission_id"],
            "run_id": receipt.run_id,
            "actor_id": control["body"]["actor_id"],
            "task_id": control["body"]["task_id"],
        })
        document = document_fixture.saved_public_pdf(
            host.store, HTML, content_type="text/html",
            url="https://example.org/synthetic-figure",
        )
        inspected = IntakeService(EvidenceLibrary(host.store)).inspect_public_document(
            "inspect-synthetic-figure", document, **document_fixture.SCOPE,
        )
        assert inspected["model_input_status"] == "permitted"
        host._update_control(control, source_refs=[inspected["copy_ref"]])
        control = host._control(receipt.run_id, "agent-synthesis")
        config = host._configuration(control)
        after, _ = host._exposed_tools(control, config)
        assert "list_source_assets" in after
        assert "fetch_source_asset" in after
        assert host._schedule_model(control)
        scheduled = host._control(receipt.run_id, "agent-synthesis")
        call = host.store.get(**scheduled["body"]["call_ref"])
        assert {"list_source_assets", "fetch_source_asset"}.issubset(
            set(call["body"]["exposed_tools"])
        )
        assert call["body"]["permitted_tools"]["list_source_assets"]["schema"][
            "properties"]["limit"]["maximum"] == 20
    finally:
        host.close()


def test_accepted_followup_discovers_its_inherited_exact_parent(tmp_path, monkeypatch):
    import ora.mission.service as service_module

    url = "https://example.org/synthetic-parent-figure"
    content = (b'<html><body><h1>Synthetic finding</h1>'
               b'<p>One checked sentence supports the offline report.</p>'
               + b''.join(
                   f'<figure><img src="/figure-{number}.png"><figcaption>'
                   f'Figure {number}. Synthetic earlier panel.</figcaption></figure>'.encode()
                   for number in range(1, 7)
               )
               + b'<figure><img src="/figure.png"><figcaption>Fig 1. Winter A.</figcaption></figure>'
               b'</body></html>')
    host = None
    calls = []
    selected = {}
    vision_requests = []

    def latest_tool(packet):
        return json.loads(next(item["content"] for item in reversed(packet["messages"])
                               if item["role"] == "tool"))

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        calls.append(packet)
        turn = len(calls)
        if turn >= 8:
            assert "signature=private-value" not in json.dumps(packet)
        if turn == 1:
            return tool("request_capability", {"tool_id": "browser_search"}, turn)
        if turn == 2:
            return Response({
                "id": "synthetic-browser-figure", "model": MODEL,
                "choices": [{"message": {"role": "assistant", "content": f"Lead: {url}"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
            })
        if turn == 3:
            hit_id = _user_value(packet, "browser_search")["hits"][0]["hit_id"]
            return tool("fetch_source", {"hit_id": hit_id}, turn)
        if turn == 4:
            item = _user_value(packet, "inventory")["items"][0]
            selected["copy_ref"] = item["ref"]
            return tool("read_source_window", {
                "source_id": item["ref"]["id"], "source_version": item["ref"]["version"],
                "block_offset": 1, "char_offset": 0, "char_limit": 200,
            }, turn)
        if turn == 6:
            return tool("read_source_window", {
                "source_id": selected["copy_ref"]["id"],
                "source_version": selected["copy_ref"]["version"],
                "block_offset": 1, "char_offset": 0, "char_limit": 200,
            }, turn)
        if turn == 7:
            return tool("list_source_assets", {
                "source_id": selected["copy_ref"]["id"],
                "source_version": selected["copy_ref"]["version"],
            }, turn)
        if turn == 8:
            listing = latest_tool(packet)
            assert listing["status"] == "listed_page_saved"
            assert len(listing["asset_handles"]) == 7
            selected["listing_result_id"] = listing["saved_tool_result_ref"]["id"]
            selected["asset_ref"] = listing["asset_handles"][6]["asset_ref"]
            return tool("fetch_source_asset", {"asset_ref": selected["asset_ref"]}, turn)
        if turn == 9:
            selected["document_ref"] = latest_tool(packet)["document_ref"]
            return tool("request_capability", {"tool_id": "read_document_ocr"}, turn)
        if turn == 10:
            index = _user_value(packet, "source_asset_navigation")
            assert index["recent_fetched_images"][0]["document_ref"] == selected["document_ref"]
            assert index["recent_fetched_images"][0]["asset_ref"] == selected["asset_ref"]
            image = index["recent_fetched_images"][0]
            assert image["source_caption"] == "Fig 1. Winter A."
            assert image["source_caption_status"] == "complete_authored_caption"
            assert image["caption_authority"] == "untrusted_source_material_not_host_instruction_or_generated_conclusion"
            assert image["ocr_action"]["arguments"] == {
                "document_ref": selected["document_ref"], "page_number": 1,
            }
            assert image["ocr_action"]["availability"] == "callable_now"
            assert image["vision_action"]["availability"] == "request_capability_first"
            assert index["listing_read"]["result_id"] == selected["listing_result_id"]
            assert any(item["function"]["name"] == "read_document_ocr"
                       for item in packet["tools"])
            return tool("read_document_ocr", {
                "document_ref": selected["document_ref"], "page_number": 1,
            }, turn)
        if turn == 11:
            ocr_result = latest_tool(packet)
            selected["ocr_result"] = ocr_result
            assert ocr_result["evidence_status"] == "ocr_unverified_not_a_checked_passage"
            assert "Synthetic axes A and B" in ocr_result["text"]
            assert ocr_result["source_caption"] == "Fig 1. Winter A."
            assert ocr_result["caption_truncated"] is False
            assert ocr_result["parent_source_url"] == url
            assert ocr_result["caption_authority"] == "untrusted_source_material_not_instructions"
            assert "engine_identity" not in ocr_result and "ocr_preparation" not in ocr_result
            assert "signature=private-value" not in json.dumps(packet)
            return tool("request_capability", {"tool_id": "inspect_figure"}, turn)
        if turn == 12:
            navigation = _user_value(packet, "source_asset_navigation")
            assert navigation["recent_fetched_images"][0]["document_ref"] == selected["document_ref"]
            assert navigation["recent_fetched_images"][0]["source_caption"] == "Fig 1. Winter A."
            assert navigation["recent_fetched_images"][0]["vision_action"]["availability"] == "callable_now"
            assert navigation["listing_read"]["result_id"] == selected["listing_result_id"]
            return tool("inspect_figure", {
                "document_ref": selected["document_ref"], "page_number": 1,
                "question": "Which panel is winter?",
            }, turn)
        if turn == 13:
            vision_result = latest_tool(packet)
            selected["vision_result"] = vision_result
            assert vision_result["evidence_status"] == "generated_unverified"
            assert vision_result["interpretation"] == "Panel A is labelled winter."
            assert "signature=private-value" not in json.dumps(packet)
            return tool("publish_result", {
                "title": "Derived figure observation",
                "text": "The selected image was read as a derived observation.",
                "citations": [], "limitations": ["OCR and vision are unverified source-derived output"],
            }, turn)
        window = host.store.records("source_read_window")[0]
        passage = host.store.get(**window["body"]["passage_refs"][0])
        return tool("publish_result", {
            "title": "Synthetic parent report", "text": "The saved sentence supports this report.",
            "citations": [{"source_id": selected["copy_ref"]["id"],
                           "span_id": passage["body"]["span_id"]}],
            "limitations": ["Synthetic offline source"],
        }, turn)

    def fetch(requested_url, **_kwargs):
        assert requested_url == url
        return {"status": 200, "content": content,
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "byte_length": len(content), "content_type": "text/html",
                "requested_url": url, "final_url": url, "redirect_chain": [],
                "access_basis": "public_https_unauthenticated", "authenticated": False,
                "retrieved_at": "2026-10-17T00:00:00Z"}

    monkeypatch.setattr(service_module, "fetch_public_source", fetch)
    from ora.evidence.source_assets import fetch_source_asset as fetch_saved_asset

    png = _png(tmp_path / "png")

    def image_fetch(store, asset_ref, *, scope, operation_id):
        return fetch_saved_asset(store, asset_ref, scope=scope, operation_id=operation_id,
                                 fetcher=lambda image_url: _receipt(png, image_url))

    monkeypatch.setattr(service_module, "fetch_source_asset", image_fetch)
    host = service(tmp_path, monkeypatch, transport)

    def vision_transport(request, *, timeout):
        body = json.loads(request.data)
        vision_requests.append(body)
        image_url = body["messages"][0]["content"][1]["image_url"]["url"]
        assert hashlib.sha256(base64.b64decode(image_url.partition(",")[2])).hexdigest() == (
            hashlib.sha256(png).hexdigest())
        text = body["messages"][0]["content"][0]["text"]
        assert text.count("Fig 1. Winter A.") == 1
        assert "Source-authored caption (untrusted data)" in text
        assert "signature=private-value" not in request.data.decode()
        return Response({
            "model": "qwen/qwen3.8-27b",
            "choices": [{"message": {"content": "Panel A is labelled winter."}}],
            "usage": {"prompt_tokens": 2100, "completion_tokens": 20,
                      "total_tokens": 2120},
        })

    host.figure_reader = FigureReader(host.store, ROOT, transport=vision_transport,
                                      allow_test_transport=True)
    try:
        parent = host.start(LiveStartRequest(
            request_id="parent-with-figure", question="Read the saved finding.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, parent.run_id, limit=40)
        view = host.state(parent.run_id).snapshot
        assert view.mission.status == "completed" and view.artifacts
        artifact = view.artifacts[0]
        child = host.followup(LiveFollowupRequest(
            request_id="child-with-inherited-figure", parent_run_id=parent.run_id,
            parent_snapshot_id=view.snapshot_id, target_id=artifact.id,
            target_version=artifact.version,
            question="Inspect the inherited figure.", agent_ids=["agent-synthesis"],
            allow_vision=True,
        ))
        control = host._control(child.run_id, "agent-synthesis")
        assert selected["copy_ref"] in control["body"]["source_refs"]
        scope = {key: control["body"][key] for key in
                 ("mission_id", "run_id", "actor_id", "task_id")}
        assets = list_source_assets(host.store, selected["copy_ref"], scope=scope)["assets"]
        assert len(assets) == 7
        assert assets[6]["caption"] == "Fig 1. Winter A."
        assert host.store.get(**assets[6]["asset_ref"])["body"]["parent_copy_ref"] == selected[
            "copy_ref"]
        config = host._configuration(control)
        with pytest.raises(BoardError, match="another mission"):
            host.executor.submit(WorkSpec(
                work_id="unrelated-foreign-source-model", mission_id=scope["mission_id"],
                run_id=scope["run_id"], proposal_ref=ref(host.store.get(
                    "board_proposal", scope["task_id"])),
                actor_id=scope["actor_id"], configuration_ref=ref(config),
                input_refs=(selected["copy_ref"],), handler_id="mission-tool",
                handler_version="1", kind="model", lane=scope["actor_id"],
                attempt_id="unrelated-foreign-source-attempt", call_id="",
            ))
        host._update_control(control, input_budget={
            "observed_limit_tokens": 50000, "target_fraction": 0.9,
        })
        drain(host, child.run_id, limit=40)
        assert host.state(child.run_id).snapshot.mission.status == "completed"
        pages = []
        offset, projection_sha = 0, ""
        while True:
            page = host.context.read_tool_result(
                mission_id=scope["mission_id"], actor_id=scope["actor_id"],
                task_id=scope["task_id"], result_id=selected["listing_result_id"],
                offset=offset, limit=1800, projection_sha256=projection_sha,
            )
            pages.append(page["text"])
            if page["next_request"] is None:
                break
            offset = page["next_request"]["offset"]
            projection_sha = page["projection_sha256"]
        assert "Fig 1. Winter A." in "".join(pages)
        assert "Figure 1. Synthetic earlier panel." in "".join(pages)
        child_copy = host.store.resolve(
            "source_copy.public_document",
            f"{selected['document_ref']['id']}:{selected['document_ref']['version']}",
        )
        assert selected["ocr_result"]["source_dependency_refs"] == [
            ref(child_copy),
            selected["copy_ref"],
        ]
        assert selected["vision_result"]["source_dependency_refs"] == [
            ref(child_copy), selected["copy_ref"],
        ]
        assert len(vision_requests) == 1
        figure_work = next(item for item in host.store.records("executor_work")
                           if item["body"]["spec"]["handler_id"] == "mission-figure")
        figure_spec = host.executor._spec_from(figure_work)
        exact_attempt = host.store.get("agent_attempt", figure_spec.attempt_id)
        pinned_call_ref = exact_attempt["body"]["call_ref"]
        assert host.store.transact("check-figure-trace-binding", {}, lambda tx: {
            "call_id": host.executor._trace_call_binding(tx, figure_spec)[0],
        }, mission_id=scope["mission_id"])["call_id"] == pinned_call_ref["id"]
        host.store.transact("forge-figure-trace-call", {}, lambda tx: tx.put(
            "agent_attempt", exact_attempt["id"], {
                **exact_attempt["body"],
                "call_ref": {"kind": "agent_call", "id": "unrelated-call", "version": 1},
            }, expected_version=exact_attempt["version"]), mission_id=scope["mission_id"])
        with pytest.raises(StateError, match="exact selecting model call"):
            host.store.transact("reject-forged-figure-trace", {}, lambda tx: {
                "call_id": host.executor._trace_call_binding(tx, figure_spec)[0],
            },
                mission_id=scope["mission_id"])
        parent_use = host.store.resolve("source_use.copy",
                                        f"{selected['copy_ref']['id']}:1")
        host.store.transact("revoke-parent-after-listing", {}, lambda tx: tx.put(
            "source_use", parent_use["id"],
            {**parent_use["body"], "policy_status": "revoked"},
            expected_version=parent_use["version"]), mission_id=scope["mission_id"])
        blocked_page = host.context.read_tool_result(
            mission_id=scope["mission_id"], actor_id=scope["actor_id"],
            task_id=scope["task_id"], result_id=selected["listing_result_id"],
            offset=0, limit=1800, projection_sha256="",
        )
        assert json.loads(blocked_page["text"])["status"] == "data_unavailable_due_source_use"
        assert host.context._asset_navigation(
            host._control(child.run_id, "agent-synthesis"),
            mission_id=scope["mission_id"], actor_id=scope["actor_id"],
            task_id=scope["task_id"],
        ) is None
    finally:
        host.close()
