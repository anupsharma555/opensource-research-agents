"""Offline mission turns connect PDF reading, local OCR and selected vision."""

from __future__ import annotations

import hashlib
import io
import json
import time
import urllib.error
from datetime import datetime

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission.service import MissionService
from ora.models import GroqSelectedAdapter, ModelSession
from ora.models.profiles import QWEN_MODEL, model_quota_scope
from ora.retrieval.providers import HTTPResult
from ora.state import StateStore
from tests.evidence.test_document_ocr import synthetic_pdf
from tests.mission.test_service import ROOT, answer, drain, tool


def _tool_reply(packet: dict, name: str) -> dict:
    return next(json.loads(item["content"]) for item in reversed(packet["messages"])
                if item.get("role") == "tool" and item.get("name") == name)


def _tools(packet: dict) -> set[str]:
    return {item["function"]["name"] for item in packet["tools"]}


class VisionReply:
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return json.dumps({
            "model": "qwen/qwen3.8-27b",
            "choices": [{"message": {"content": "The chart label reads Synthetic PDF detail."}}],
            "usage": {"prompt_tokens": 2100, "completion_tokens": 20, "total_tokens": 2120},
        }).encode()


def test_vision_is_not_requestable_without_run_opt_in(tmp_path, monkeypatch):
    packets = []

    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        notice = next(json.loads(item["content"]) for item in packet["messages"]
                      if item.get("role") == "user"
                      and "trusted_host_attached_tool_availability" in item.get("content", ""))
        assert "inspect_figure" not in _tools(packet)
        assert "inspect_figure" not in notice["call_now_local_functions"]
        assert "Vision is not authorized" in notice["not_ready_local_functions"]["inspect_figure"]
        return answer("Vision was not enabled for this run.", 1)

    monkeypatch.setenv("GROQ_API_KEY", "synthetic-model-key")
    store = StateStore(tmp_path)
    session = ModelSession(store, ROOT, adapter=GroqSelectedAdapter(ROOT, transport=model))
    host = MissionService(
        ROOT, tmp_path, model_session=session, execution_mode="offline_test",
        start_supervisor=False,
    )
    try:
        run = host.start(LiveStartRequest(
            request_id="document-reader-default-off", question="Describe a synthetic page",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert len(packets) == 1
    finally:
        host.close()


@pytest.mark.parametrize("vision_case", ["success", "rate_limited", "uncertain"])
def test_pdf_source_window_then_local_ocr_and_opted_in_figure(
    tmp_path, monkeypatch, vision_case,
):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-model-key")
    monkeypatch.setenv("EXA_API_KEY", "synthetic-search-key")
    pdf = synthetic_pdf("Synthetic PDF detail")
    url = "https://example.org/synthetic-public.pdf"
    calls = {"search": 0, "fetch": 0, "vision": 0}
    packets = []
    state = {}

    def fetch(requested_url):
        assert requested_url == url
        calls["fetch"] += 1
        return {"status": 200, "content": pdf,
                "content_sha256": hashlib.sha256(pdf).hexdigest(),
                "byte_length": len(pdf), "content_type": "application/pdf",
                "requested_url": url, "final_url": url, "redirect_chain": [],
                "access_basis": "public_https_unauthenticated", "authenticated": False,
                "retrieved_at": "2026-10-15T00:00:00Z"}

    monkeypatch.setattr("ora.mission.service.fetch_public_source", fetch)

    def vision(request, *, timeout):
        calls["vision"] += 1
        body = json.loads(request.data)
        assert body["model"] == "qwen/qwen3.8-27b"
        assert body["reasoning_effort"] == "none"
        assert body["messages"][0]["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )
        if vision_case == "rate_limited":
            raise urllib.error.HTTPError(
                "https://api.groq.com/openai/v1/chat/completions", 429, "synthetic limit",
                {"Retry-After": "3"}, io.BytesIO(b"{}"),
            )
        if vision_case == "uncertain":
            raise TimeoutError("synthetic unknown dispatch")
        return VisionReply()

    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        attached = _tools(packet)
        if step == 1:
            assert "inspect_figure" not in attached
            return tool("request_capability", {"tool_id": "search_web"}, step)
        if step == 2:
            return tool("search_web", {
                "mode": "quick", "information_need": "Find one public PDF",
                "queries": ["synthetic public PDF"],
            }, step)
        if step == 3:
            assert "read_search_results" in attached
            operation = _tool_reply(packet, "search_web")["operation_ref"]["id"]
            return tool("read_search_results", {"operation_id": operation, "wait": True}, step)
        if step == 4:
            result = _tool_reply(packet, "read_search_results")
            hit = result["available_results"][0]["result"]["hits"][0]
            assert "fetch_source" in attached
            return tool("fetch_source", {"hit_id": hit["hit_id"]}, step)
        if step == 5:
            index = next(json.loads(item["content"]) for item in packet["messages"]
                         if item.get("role") == "user"
                         and item.get("content", "").startswith("{")
                         and json.loads(item["content"]).get("authority")
                         == "saved_host_source_index_not_source_instruction")
            selected = index["inventory"]["items"][0]
            state["document"] = selected["ordinary_document_ref"]
            return tool("read_source_window", {
                "source_id": selected["fetch"]["source_id"],
                "source_version": selected["fetch"]["source_version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 1000,
            }, step)
        if step == 6:
            assert "Synthetic PDF detail" in json.dumps(packet)
            window_result = _tool_reply(packet, "read_source_window")
            assert window_result["source_text_status"] == (
                "exact_checked_passages_in_this_model_input"
            )
            assert "PDF page 1" in window_result["reading_progress"]
            assert window_result["source_url"] == url
            assert "whole source reading unverified" in window_result["reading_progress"]
            return tool("request_capability", {"tool_id": "read_document_ocr"}, step)
        if step == 7:
            assert "read_document_ocr" in attached
            schema = next(item["function"]["parameters"] for item in packet["tools"]
                          if item["function"]["name"] == "read_document_ocr")
            assert "region" in schema["properties"] and "layout" in schema["properties"]
            return tool("read_document_ocr", {
                "document_ref": state["document"], "page_number": 1,
                **({"region": {"x": 0.02, "y": 0.02, "width": 0.96, "height": 0.2},
                    "layout": "block"} if vision_case == "success" else {}),
            }, step)
        if step == 8:
            ocr = _tool_reply(packet, "read_document_ocr")
            assert ocr["status"] == "ocr_unverified"
            assert ocr["evidence_status"] == "ocr_unverified_not_a_checked_passage"
            assert "synthetic pdf detail" in ocr["text"].lower()
            assert ocr["source_dependency_refs"]
            assert ocr["source_url"] == url and ocr["reader"] == "local_ocr"
            assert "ocr_preparation" not in ocr and "engine_identity" not in ocr
            saved_ocr = store.get(**ocr["ocr_ref"])["body"]
            assert saved_ocr["ocr_preparation"]["pdf_scale_to"] == 3200
            assert saved_ocr["engine_identity"]["traineddata_sha256"]
            if vision_case == "success":
                assert ocr["region"]["height"] == 0.2 and ocr["layout"] == "block"
                assert saved_ocr["psm"] == 6 and ocr["restriction_screen_scope"] == "selected_region"
            return tool("request_capability", {"tool_id": "inspect_figure"}, step)
        if step == 9:
            assert "inspect_figure" in attached
            return tool("inspect_figure", {
                "document_ref": state["document"], "page_number": 1,
                "question": "What does the chart label say?",
            }, step)
        assert step == 10 and vision_case != "uncertain"
        figure = _tool_reply(packet, "inspect_figure")
        assert figure["status"] == (
            "rate_limited" if vision_case == "rate_limited" else "generated_unverified"
        )
        assert figure["evidence_status"] == "generated_unverified"
        assert figure["ocr_ref"] and figure["source_dependency_refs"]
        if vision_case == "success":
            assert "Synthetic PDF detail" in figure["interpretation"]
        else:
            assert figure["interpretation"] is None
            assert figure["retry_after_seconds"] == 3
        return answer("The checked text and derived figure observation were distinguished.", step)

    store = StateStore(tmp_path)
    session = ModelSession(store, ROOT, adapter=GroqSelectedAdapter(ROOT, transport=model))
    host = MissionService(
        ROOT, tmp_path, model_session=session, execution_mode="offline_test",
        start_supervisor=False, vision_transport=vision,
    )
    host.web_search.transport = lambda *_args, **_kwargs: (
        calls.__setitem__("search", calls["search"] + 1)
        or HTTPResult(200, json.dumps({"results": [{
            "id": "synthetic-pdf", "url": url, "title": "Synthetic PDF",
            "highlights": ["Uninspected PDF candidate"],
        }]}).encode(), {})
    )
    try:
        run = host.start(LiveStartRequest(
            request_id="document-readers-pdf-vision",
            question="Inspect the synthetic PDF and its selected figure.",
            agent_ids=["agent-synthesis"], allow_vision=True,
        ))
        if vision_case == "rate_limited":
            for _ in range(300):
                host._drive_run(host._run(run.run_id))
                if any((record["body"].get("effect_ref") or {}).get("kind")
                       == "figure_response_receipt"
                       for record in host.store.records("context_tool_result")):
                    break
                time.sleep(0.01)
            else:
                pytest.fail("Captured figure rejection was not accepted as a tool result")
            receipt = host.store.records("figure_response_receipt")[0]
            quota = host.store.get("capacity_quota", model_quota_scope("groq-selected", QWEN_MODEL))["body"]
            receipt_time = datetime.fromisoformat(
                receipt["created_at"].replace("Z", "+00:00")
            ).timestamp()
            assert quota["blocked_until"] >= receipt_time + 3
            assert quota["blocked_until"] > host.store.clock()
            for _ in range(5):
                host._drive_run(host._run(run.run_id))
            drain(host, run.run_id, limit=15)
            assert len(packets) == 10  # A Qwen hold does not block GPT-OSS continuation.
        else:
            drain(host, run.run_id, limit=15)
        assert len(packets) == (9 if vision_case == "uncertain" else 10), {
            "steps": [[item.get("name") for item in packet["messages"]
                       if item.get("role") == "tool"] for packet in packets],
            "control": host._control(run.run_id, "agent-synthesis")["body"]["phase"],
        }
        assert calls == {"search": 1, "fetch": 1, "vision": 1}
        if vision_case == "success":
            assert host.state(run.run_id).snapshot.mission.status == "completed"
        receipts = host.store.records("figure_response_receipt")
        if vision_case == "uncertain":
            assert receipts == []
            assert len(host.store.records("figure_dispatch_attempt")) == 1
        else:
            assert len(receipts) == 1
            if vision_case == "success":
                assert receipts[0]["body"]["usage"]["total_tokens"] == 2120
            else:
                assert receipts[0]["body"]["status"] == "rate_limited"
                assert receipts[0]["body"]["retry_after_seconds"] == 3
        figure_work = [record for record in host.store.records("executor_work")
                       if record["body"].get("spec", {}).get("handler_id") == "mission-figure"]
        assert len(figure_work) == 1
        spec = figure_work[0]["body"]["spec"]
        assert spec["kind"] == "model" and spec["call_id"] == ""
        assert spec["quota_scopes"] == [model_quota_scope("groq-selected", QWEN_MODEL)]
        assert spec["output_reservation_tokens"] == 1024
        assert spec["estimated_tokens"] >= 2048 + 1024
        if vision_case == "uncertain":
            assert figure_work[0]["body"]["phase"] == "pending"
            capacity_id = figure_work[0]["body"]["runtime_segments"][0][
                "capacity_operation_id"
            ]
            capacity = host.store.get("capacity_operation", capacity_id)
            assert capacity["body"]["phase"] == "uncertain"
            assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "waiting"
            assert calls["vision"] == 1
            host.close()
            reopened_store = StateStore(tmp_path)
            reopened_session = ModelSession(
                reopened_store, ROOT,
                adapter=GroqSelectedAdapter(ROOT, transport=model),
            )
            host = MissionService(
                ROOT, tmp_path, model_session=reopened_session,
                execution_mode="offline_test", start_supervisor=False,
                vision_transport=vision,
            )
            drain(host, run.run_id, limit=4)
            assert calls["vision"] == 1
            assert host.store.get("capacity_operation", capacity_id)["body"]["phase"] == (
                "uncertain"
            )
    finally:
        host.close()
