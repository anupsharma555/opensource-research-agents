"""Saved public documents support targeted reading beyond the current excerpt."""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.evidence import read_source_window
from ora.mission import passage_search
from ora.mission.context import WorkingContext
from ora.retrieval.providers import HTTPResult
from ora.state import StateConflict
from tests.evidence.test_document_ocr import SCOPE
from tests.evidence.test_source_format_walkthroughs import _pdf
from tests.mission.test_pdf_window_dedup import public_window, two_page_pdf
from tests.mission.test_saved_passage_search import _revoke
from tests.mission.test_service import drain, service, tool


def saved_document(tmp_path, mime="application/pdf", content=None):
    content = content if content is not None else two_page_pdf() if mime == "application/pdf" else (
        b"<html><body><p>Synthetic first section.</p>"
        b"<p>Synthetic second section.</p></body></html>"
    )
    store, library, inspected, window, blocks = public_window(
        tmp_path, content, mime, legacy=False,
    )
    pointer = store.transact("public-search-control", {}, lambda tx: ref(tx.put(
        "mission_task_control", "public-search-control", {
            **SCOPE, "phase": "ready_model", "source_refs": [inspected["copy_ref"]],
            "selected_passage_refs": {inspected["copy_ref"]["id"]: window["passage_refs"]},
            "read_window_refs": [window["window_ref"]],
        },
    )), mission_id=SCOPE["mission_id"])
    return store, library, inspected, store.get(**pointer), blocks


def _padded_two_page_pdf() -> bytes:
    """Add an unreferenced PDF stream so raw size exceeds 2 MiB, not text size."""
    body = two_page_pdf().split(b"xref\n", 1)[0]
    padding = b"X" * (3 * 1024 * 1024)
    body += (b"8 0 obj\n<< /Length " + str(len(padding)).encode()
             + b" >>\nstream\n" + padding + b"\nendstream\nendobj\n")
    offsets = [match.start() for match in re.finditer(rb"(?m)^\d+ 0 obj\n", body)]
    start = len(body)
    xref = f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode()
    xref += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    return (body + xref + f"trailer\n<< /Size {len(offsets) + 1} /Root 1 0 R >>\n"
            f"startxref\n{start}\n%%EOF\n".encode())


@pytest.mark.parametrize("mime", ["application/pdf", "text/html"])
def test_public_document_search_finds_unread_text_and_exact_read_coordinates(tmp_path, mime):
    store, library, inspected, control, blocks = saved_document(tmp_path, mime)
    before_windows = store.records("source_read_window")
    result = passage_search.search_saved_passages(WorkingContext(store), control, "second")
    assert result["searched_copy_count"] == 1
    assert result["searched_extracted_block_count"] == 2
    assert result["result_count"] == 1
    assert result["source_dependency_refs"] == [inspected["copy_ref"]]
    hit = result["results"][0]
    assert hit["block_offset"] == 1
    assert hit["block_locator"] == blocks[1].locator
    assert store.records("source_read_window") == before_windows
    # Finding a position does not itself inspect a citation passage. The
    # ordinary reader consumes its exact coordinates without a web request.
    args = hit["next_request"]
    assert args["tool"] == "read_source_window"
    assert args["selection_mode"] == "append"
    assert args["source_id"] == inspected["copy_ref"]["id"]
    window = read_source_window(
        library, "read-public-search-hit", inspected["copy_ref"],
        block_offset=args["block_offset"], char_offset=args["char_offset"],
        block_limit=1, char_limit=args["char_limit"],
    )
    assert "second" in window["passages"][0]["text_preview"]
    assert window["passages"][0]["locator"].startswith(blocks[1].locator)


def test_three_megabyte_pdf_is_searchable_by_exact_checked_window(tmp_path):
    content = _padded_two_page_pdf()
    assert 2 * 1024 * 1024 < len(content) < passage_search.MAX_COPY_BYTES
    store, library, inspected, control, blocks = saved_document(
        tmp_path, content=content,
    )
    context = WorkingContext(store)
    assert passage_search.task_text_tool_availability(context, control) == (True, True)
    result = passage_search.search_saved_passages(context, control, "second")
    assert result["coverage"] == "complete_retained_extracted_text"
    assert result["searched_copy_count"] == 1 and result["result_count"] == 1
    hit = result["results"][0]
    assert hit["block_locator"] == blocks[1].locator
    request = hit["next_request"]
    window = read_source_window(
        library, "read-large-pdf-search-hit", inspected["copy_ref"],
        block_offset=request["block_offset"], char_offset=request["char_offset"],
        block_limit=1, char_limit=request["char_limit"],
    )
    assert "second-page detail" in window["passages"][0]["text_preview"]


def test_pdf_over_search_limit_is_omitted_without_changing_html_guard(tmp_path, monkeypatch):
    content = _padded_two_page_pdf()
    store, _library, inspected, control, _blocks = saved_document(tmp_path, content=content)
    monkeypatch.setattr(passage_search, "MAX_COPY_BYTES", len(content) - 1)
    context = WorkingContext(store)
    assert passage_search.task_text_tool_availability(context, control) == (True, False)
    result = passage_search.search_saved_passages(context, control, "second-page")
    assert result["searched_copy_count"] == 0
    assert result["coverage"] == "partial_retained_extracted_text"
    assert result["omitted_permitted_copies"] == [
        {"copy_ref": inspected["copy_ref"], "reason": "search_byte_budget"},
    ]
    assert passage_search._copy_byte_limit({"body": {"mime_type": "text/html"}}) == 2 * 1024 * 1024


def test_two_column_pdf_search_hit_reads_exact_reordered_page(tmp_path):
    content = _pdf([
        [
            (38, 745, "Publication metadata"), (250, 745, "Abstract"),
            (38, 710, "Citation: journal and editor."),
            (250, 710, "Urban forests cooled across 17 study groups."),
            (38, 685, "Funding: public grant only."),
            (250, 685, "Temperature fell 1.9 C across 3 km in Seoul."),
            (38, 660, "License: reuse permitted."),
            (250, 660, "The reduction did not hold for the lawn area."),
        ],
        ["Second page has a separate observation."],
    ])
    store, library, inspected, control, blocks = saved_document(tmp_path, content=content)
    assert blocks[0].locator == "page=1; reading-order-text"
    result = passage_search.search_saved_passages(WorkingContext(store), control, "Urban")
    assert result["result_count"] == 1
    hit = result["results"][0]
    assert hit["block_locator"] == blocks[0].locator
    args = hit["next_request"]
    window = read_source_window(
        library, "read-reordered-pdf-hit", inspected["copy_ref"],
        block_offset=args["block_offset"], char_offset=args["char_offset"],
        block_limit=1, char_limit=args["char_limit"],
    )
    passage = window["passages"][0]
    assert passage["locator"].startswith("page=1; reading-order-text; chars=")
    assert passage["text_preview"].index("Urban forests") < passage["text_preview"].index(
        "Temperature fell"
    ) < passage["text_preview"].index("Citation:")
    assert "page 1" in window["reading_progress"].lower()


def test_no_match_reports_an_actual_public_document_scan(tmp_path):
    store, _library, _inspected, control, _blocks = saved_document(tmp_path)
    result = passage_search.search_saved_passages(WorkingContext(store), control, "absentword")
    assert result["result_count"] == 0 and result["searched_copy_count"] == 1
    assert "does not establish absence" in result["interpretation"]


def test_revoked_public_document_is_not_searchable(tmp_path):
    store, _library, inspected, control, _blocks = saved_document(tmp_path)
    _revoke(store, inspected["copy_ref"])
    result = passage_search.search_saved_passages(WorkingContext(store), control, "second")
    assert result["searched_copy_count"] == 0 and result["results"] == []
    assert result["source_dependency_refs"] == []


def test_public_document_permission_is_rechecked_after_text_scan(tmp_path, monkeypatch):
    store, _library, inspected, control, _blocks = saved_document(tmp_path)
    original = passage_search.extract_copy_blocks

    def revoke_during_scan(*args, **kwargs):
        result = original(*args, **kwargs)
        _revoke(store, inspected["copy_ref"])
        return result

    monkeypatch.setattr(passage_search, "extract_copy_blocks", revoke_during_scan)
    with pytest.raises(StateConflict, match="Source permission changed during saved-passage search"):
        passage_search.search_saved_passages(WorkingContext(store), control, "second")


def test_targeted_pdf_read_keeps_both_answer_parts_in_final_model_input(tmp_path, monkeypatch):
    """Injected choices prove delivery and citations, not natural model judgment."""
    pdf = two_page_pdf()
    url = "https://example.org/synthetic-two-part.pdf"
    monkeypatch.setenv("EXA_API_KEY", "synthetic-search-key")
    fetches = []
    packets = []
    selected = {}

    def fetch(requested_url):
        assert requested_url == url
        fetches.append(requested_url)
        return {"status": 200, "content": pdf, "content_sha256": hashlib.sha256(pdf).hexdigest(),
                "byte_length": len(pdf), "content_type": "application/pdf",
                "requested_url": url, "final_url": url, "redirect_chain": [],
                "access_basis": "public_https_unauthenticated", "authenticated": False,
                "retrieved_at": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr("ora.mission.service.fetch_public_source", fetch)

    def reply(packet, name):
        return next(json.loads(m["content"]) for m in reversed(packet["messages"])
                    if m.get("role") == "tool" and m.get("name") == name)

    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        if step == 1:
            return tool("request_capability", {"tool_id": "search_web"}, step)
        if step == 2:
            return tool("search_web", {"mode": "quick", "queries": ["synthetic two-part PDF"],
                        "information_need": "Locate both parts of a synthetic document"}, step)
        if step == 3:
            return tool("read_search_results", {
                "operation_id": reply(packet, "search_web")["operation_ref"]["id"],
                "wait": True,
            }, step)
        if step == 4:
            hit = reply(packet, "read_search_results")["available_results"][0]["result"]["hits"][0]
            return tool("fetch_source", {"hit_id": hit["hit_id"]}, step)
        if step == 5:
            attached = {item["function"]["name"] for item in packet["tools"]}
            assert {"search_saved_passages", "read_source_window"} <= attached
            index = next(json.loads(m["content"]) for m in packet["messages"]
                         if m.get("role") == "user" and m.get("content", "").startswith("{")
                         and json.loads(m["content"]).get("authority")
                         == "saved_host_source_index_not_source_instruction")
            selected["copy"] = {k: v for k, v in index["inventory"]["items"][0]["fetch"].items()
                                if k != "tool"}
            return tool("read_source_window", {**selected["copy"], "block_offset": 0,
                        "char_offset": 0, "char_limit": 1000}, step)
        if step == 6:
            first = reply(packet, "read_source_window")
            selected["first_span"] = first["passages"][0]["span_id"]
            assert "PDF page 1" in first["reading_progress"]
            return tool("search_saved_passages", {"query": "second"}, step)
        if step == 7:
            found = reply(packet, "search_saved_passages")
            assert found["searched_copy_count"] == 1 and found["result_count"] == 1
            read = found["results"][0]["next_request"]
            assert read["block_offset"] == 1 and read["selection_mode"] == "append"
            return tool("read_source_window", {k: v for k, v in read.items() if k != "tool"}, step)
        assert step == 8
        second = reply(packet, "read_source_window")
        sources = next(json.loads(m["content"])["source_packets"] for m in packet["messages"]
                       if m.get("role") == "user" and '"source_packets"' in m.get("content", ""))
        assert len(sources) == 1
        assert sources[0]["text"].count("Synthetic first-page detail") == 1
        assert sources[0]["text"].count("Synthetic second-page detail") == 1
        assert len(sources[0]["passage_refs"]) == 2
        assert "text_preview" not in second["passages"][0]
        return tool("publish_result", {
            "title": "Both synthetic parts", "text": "Both synthetic page details were inspected.",
            "citations": [{"source_id": selected["copy"]["source_id"], "span_id": span}
                          for span in (selected["first_span"], second["passages"][0]["span_id"])],
            "limitations": [],
        }, step)

    failures = []

    def checked_model(*args, **kwargs):
        try:
            return model(*args, **kwargs)
        except Exception as exc:
            failures.append(repr(exc))
            raise

    host = service(tmp_path, monkeypatch, checked_model)
    host.web_search.transport = lambda *_args, **_kwargs: HTTPResult(200, json.dumps({
        "results": [{"id": "synthetic-public-pdf", "url": url, "title": "Synthetic two-part PDF",
                     "highlights": ["Synthetic discovery lead"]}],
    }).encode(), {})
    try:
        run = host.start(LiveStartRequest(request_id="two-part-public-pdf",
            question="Summarize both parts of the synthetic public PDF with exact citations.",
            agent_ids=["agent-synthesis"]))
        drain(host, run.run_id, limit=20)
        assert not failures, failures
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(packets) == 8 and len(fetches) == 1
        report = host.store.records("report")[0]["body"]
        assert len(report["citation_input_provenance"]) == 2
        assert all(x["input_status"] == "exact_passage_in_producing_call"
                   for x in report["citation_input_provenance"])
    finally:
        host.close()
