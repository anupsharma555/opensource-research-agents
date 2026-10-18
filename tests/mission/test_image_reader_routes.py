"""Image reader handoffs preserve exact task scope and current source use."""

from __future__ import annotations

import hashlib
import json

from ora.contracts.models import LiveStartRequest
from ora.evidence.intake import IntakeService
from ora.evidence.library import EvidenceLibrary
from ora.evidence.source_assets import fetch_source_asset, list_source_assets
from ora.mission.document_tools import image_reader_handoff
from ora.mission.tool_catalog import TOOL_SCHEMAS
from tests.evidence import test_document_ocr as document_fixture
from tests.evidence.test_document_ocr import SCOPE
from tests.evidence.test_source_assets import _image_child, _png, _receipt
from tests.mission.test_service import answer, drain, service, tool


def test_image_copy_handoff_has_executable_ocr_and_bounded_vision(tmp_path, monkeypatch):
    store, parent_ref, _use_ref, asset_ref, _png, document_ref, inspected = _image_child(tmp_path)

    def no_reader(*_args, **_kwargs):
        raise AssertionError("Handoff must not render, OCR or invoke vision")

    monkeypatch.setattr("ora.mission.document_tools.ocr_public_document_page", no_reader)
    monkeypatch.setattr("ora.mission.document_tools.render_public_document_page", no_reader)
    handoff = image_reader_handoff(store, scope=SCOPE, copy_ref=inspected["copy_ref"],
                                    allow_vision=True)
    assert handoff["kind"] == "image_document_reader_handoff"
    assert handoff["mime_type"] == "image/png"
    assert handoff["document_ref"] == document_ref
    assert handoff["source_dependency_refs"] == [parent_ref, inspected["copy_ref"]]
    asset = store.get(**asset_ref)["body"]
    assert handoff["source_caption"] == asset["caption"]
    assert handoff["caption_authority"] == "untrusted_source_material_not_instructions"
    assert handoff["source_caption_sha256"] == hashlib.sha256(
        asset["caption"].encode()).hexdigest()
    assert handoff["ocr_action"] == {
        "tool_id": "read_document_ocr",
        "arguments": {"document_ref": document_ref, "page_number": 1},
        "request_if_not_attached": {
            "tool_id": "request_capability",
            "arguments": {"tool_id": "read_document_ocr"},
        },
    }
    required = TOOL_SCHEMAS["read_document_ocr"]["required"]
    assert set(required) <= set(handoff["ocr_action"]["arguments"])
    assert handoff["vision_action"] == {
        "tool_id": "inspect_figure",
        "arguments": {"document_ref": document_ref, "page_number": 1},
        "question_required": True,
        "request_if_not_attached": {
            "tool_id": "request_capability",
            "arguments": {"tool_id": "inspect_figure"},
        },
    }
    assert "question" not in handoff["vision_action"]["arguments"]
    assert "vision_action" not in image_reader_handoff(
        store, scope=SCOPE, document_ref=document_ref)


def test_image_handoff_hides_wrong_task_missing_and_revoked_sources(tmp_path):
    store, _parent_ref, parent_use_ref, _asset_ref, _png, document_ref, inspected = (
        _image_child(tmp_path)
    )
    assert image_reader_handoff(store, scope={**SCOPE, "task_id": "other-task"},
                                    document_ref=document_ref) is None
    assert image_reader_handoff(store, scope=SCOPE, document_ref={
        "kind": "public_document_fetch", "id": "missing", "version": 1}) is None
    assert image_reader_handoff(store, scope=SCOPE, copy_ref={
        **inspected["copy_ref"], "version": 999}) is None
    assert image_reader_handoff(store, scope=SCOPE, copy_ref={
        **inspected["copy_ref"], "unexpected": "field"}) is None
    parent_use = store.get(**parent_use_ref)
    store.transact("revoke-image-parent", {}, lambda tx: tx.put(
        "source_use", parent_use["id"],
        {**parent_use["body"], "policy_status": "revoked"},
        expected_version=parent_use["version"],
    ), mission_id=SCOPE["mission_id"])
    assert image_reader_handoff(store, scope=SCOPE, document_ref=document_ref) is None
    assert image_reader_handoff(store, scope=SCOPE,
                                copy_ref=inspected["copy_ref"]) is None


def test_wrong_text_reader_delivers_image_actions_in_next_model_request(tmp_path, monkeypatch):
    selected = {}
    requests = []
    fetched_urls = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        requests.append(packet)
        if len(requests) == 1:
            assert any(item["function"]["name"] == "read_source_window"
                       for item in packet["tools"])
            return tool("read_source_window", {
                "source_id": selected["copy_ref"]["id"],
                "source_version": selected["copy_ref"]["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 200,
            }, 1)
        selected["second_packet"] = packet
        tool_items = [item for item in packet["messages"] if item["role"] == "tool"]
        if tool_items:
            result = json.loads(tool_items[-1]["content"])
            assert result["status"] == "rejected"
            assert "PNG/JPEG image" in result["condition"]
            assert "inspect_hit" not in result["next_action"]
            assert "request_capability" in result["next_action"]
            handoff = result["image_reader_handoff"]
            selected["wrong_reader_result"] = result
        else:
            indexes = [json.loads(item["content"]) for item in packet["messages"]
                       if item["role"] == "user"
                       and "wrong_image_reader_navigation" in item["content"]]
            assert indexes
            handoff = indexes[-1]["wrong_image_reader_navigation"]
            assert handoff["status"] == "current_image_navigation_after_wrong_text_reader"
            selected["wrong_reader_result"] = handoff
        assert handoff["document_ref"] == selected["document_ref"]
        assert handoff["source_dependency_refs"] == [
            selected["parent_ref"], selected["copy_ref"]]
        assert handoff["ocr_action"]["arguments"] == {
            "document_ref": selected["document_ref"], "page_number": 1}
        assert handoff["ocr_action"]["request_if_not_attached"] == {
            "tool_id": "request_capability",
            "arguments": {"tool_id": "read_document_ocr"}}
        assert handoff["caption_authority"] == "untrusted_source_material_not_instructions"
        assert "vision_action" not in handoff
        assert handoff["source_caption"] == "Fig 1. Synthetic seasons."
        return answer("The figure requires image reading.", len(requests))

    png = _png(tmp_path / "png")
    host = service(tmp_path / "host", monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="wrong-image-reader", question="Read the synthetic figure.",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        scope = {key: control["body"][key] for key in
                 ("mission_id", "run_id", "actor_id", "task_id")}
        monkeypatch.setattr(document_fixture, "SCOPE", scope)
        html = (b'<html><body><p>Synthetic source text.</p><figure>'
                b'<img src="/synthetic.png"><figcaption>Fig 1. Synthetic seasons.'
                b'</figcaption></figure></body></html>')
        parent_doc = document_fixture.saved_public_pdf(
            host.store, html, content_type="text/html",
            url="https://example.org/synthetic-article")
        inspected_parent = IntakeService(EvidenceLibrary(host.store)).inspect_public_document(
            "inspect-synthetic-parent", parent_doc, **scope)
        selected["parent_ref"] = inspected_parent["copy_ref"]
        host._update_control(control, source_refs=[selected["parent_ref"]])
        asset_ref = list_source_assets(host.store, selected["parent_ref"], scope=scope)[
            "assets"][0]["asset_ref"]
        def saved_image_fetch(url):
            fetched_urls.append(url)
            return _receipt(png, url)

        selected["document_ref"] = fetch_source_asset(
            host.store, asset_ref, scope=scope, operation_id="fetch-synthetic-image",
            fetcher=saved_image_fetch)
        inspected_image = IntakeService(EvidenceLibrary(host.store)).inspect_public_document(
            "inspect-synthetic-image", selected["document_ref"], **scope)
        selected["copy_ref"] = inspected_image["copy_ref"]
        assert selected["copy_ref"] is not None
        drain(host, run.run_id, limit=12)
        assert len(requests) >= 2
        assert "wrong_reader_result" in selected
        assert selected["wrong_reader_result"]["source_dependency_refs"] == [
            selected["parent_ref"], selected["copy_ref"]]
        assert len(fetched_urls) == 1
        assert host.store.records("public_document_page_image") == []
        assert host.store.records("public_document_page_ocr") == []
        assert host.store.records("figure_request_intent") == []
        intents = host.store.records("model_request_intent")
        assert any(all(pointer in item["body"].get("context_source_dependency_refs", [])
                       for pointer in (selected["parent_ref"], selected["copy_ref"]))
                   for item in intents)

        parent_use = host.store.resolve(
            "source_use.copy", f"{selected['parent_ref']['id']}:{selected['parent_ref']['version']}")
        host.store.transact("revoke-after-wrong-reader", {}, lambda tx: tx.put(
            "source_use", parent_use["id"],
            {**parent_use["body"], "policy_status": "revoked"},
            expected_version=parent_use["version"]), mission_id=scope["mission_id"])
        tool_response = next(item for item in host.store.records("model_response_receipt")
                             if item["body"].get("status") == "tool_requested")
        replay, _feedback = host.context._tool_exchange(
            {key: tool_response[key] for key in ("kind", "id", "version")},
            scope={"mission_id": scope["mission_id"], "actor_id": scope["actor_id"],
                   "task_id": scope["task_id"]})
        assert "Synthetic seasons" not in json.dumps(replay)
        assert "image_reader_handoff" not in json.dumps(replay)
        assert "data_unavailable_due_source_use" in json.dumps(replay)
    finally:
        host.close()
