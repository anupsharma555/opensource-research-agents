"""Selected synthetic format passages reach the saved serialized model request.

This exercises controlled source selection and model preparation without a
provider call. It does not test natural tool choice or semantic understanding.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from ora.agents import COMMON_TOOL_IDS
from ora.agents.capabilities import content_hash
from ora.coordination.board import ref
from ora.evidence.document_ocr import ocr_public_document_page, render_public_document_page
from ora.evidence.intake import checked_model_text, extract_blocks
from ora.evidence.windows import read_source_window
from ora.mission.context import WorkingContext
from ora.models import SourceSelection
from ora.state.store import canonical
from tests.evidence import test_document_ocr as document_fixture
from tests.evidence import test_source_format_walkthroughs as format_fixture
from tests.evidence.test_source_format_walkthroughs import _inspected, _pdf, _raster_page
from tests.models.test_session import _preview_task, setup

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "source_formats"


@pytest.mark.parametrize("case,mime,content,target,required", [
    ("H1", "text/html", (FIXTURES / "h1_navigation.html").read_bytes(),
     "reduced annual electricity use", ["winter weekends"]),
    ("H2", "text/html", (FIXTURES / "h2_table.html").read_bytes(),
     "Before (kWh): 1,200", ["Annual electricity use, kWh", "Site: Library",
                           "After (kWh): 984", "excludes winter weekends"]),
    ("P1", "application/pdf", _pdf([
        ["Annual electricity use fell by 18 percent."],
        ["Qualification: winter weekends are excluded."],
    ]), "18 percent", ["winter weekends are excluded"]),
    ("P2", "application/pdf", _pdf([[
        (38, 745, "LEFT COLUMN: demand fell."),
        (320, 745, "RIGHT COLUMN: winter exception."),
        (38, 722, "Savings lasted through autumn."),
        (320, 722, "Weekends were not measured."),
        (38, 700, "Annual electricity use (kWh)"),
        (38, 677, "Site"), (170, 677, "Before (kWh)"), (350, 677, "After (kWh)"),
        (38, 654, "Library"), (170, 654, "1,200"), (350, 654, "984"),
        (38, 631, "Footnote: winter weekends excluded."),
    ]]), "Library", ["1,200", "984", "Before (kWh)", "After (kWh)",
                      "winter weekends excluded", "Savings lasted through autumn."]),
    ("X1", "application/jats+xml", (FIXTURES / "x1_article.xml").read_bytes(),
     "from 1,200 to 984 kWh", ["excludes winter weekends"]),
    ("T1", "text/plain", (FIXTURES / "t1_notes.txt").read_bytes(),
     "later checked result", ["excludes winter weekends"]),
])
def test_selected_format_evidence_is_in_saved_model_request(
    tmp_path, monkeypatch, case, mime, content, target, required,
):
    if mime == "application/pdf" and not shutil.which("pdftotext"):
        pytest.skip("Native Poppler unavailable")
    scope = {"mission_id": "mission", "run_id": "run",
             "actor_id": "synthetic-peer", "task_id": "task"}
    monkeypatch.setattr(document_fixture, "SCOPE", scope)
    monkeypatch.setattr(format_fixture, "SCOPE", scope)
    source_store, library, _document, inspected = _inspected(tmp_path, content, mime)
    blocks, _parser = extract_blocks(content, mime)
    indices = [index for index, block in enumerate(blocks)
               if target in block.text or any(item in block.text for item in required)]
    if case == "H2":
        indices = sorted(set(indices + [1, 2]))  # Caption and header precede row.
    char_offset = content.decode("utf-8").index("The later checked result") if case == "T1" else 0
    windows = [read_source_window(
        library, f"delivery-{case}-{index}", inspected["copy_ref"],
        block_offset=index, block_limit=1, char_offset=char_offset, char_limit=4000,
    ) for index in indices]
    passage_refs = [window["passage_refs"][0] for window in windows]
    expected = checked_model_text(library, inspected["copy_ref"], passage_refs)
    assert target in expected["text"]
    assert all(item in expected["text"] for item in required)
    assert source_store.get(**inspected["copy_ref"])["body"]["content_sha256"] == (
        hashlib.sha256(content).hexdigest()
    )

    # Reopen the same saved state through the actual model-session request builder.
    store, _registry, bridge, call, _config, copy_ref, captured = setup(
        tmp_path, source_ref=inspected["copy_ref"],
    )
    selection = (SourceSelection(
        copy_ref, "permitted", True, "direct", tuple(passage_refs),
    ),)
    prepared = bridge.prepare(
        call_id=call["id"], call_version=call["version"], phase="collect",
        messages=[{"role": "user", "content": "Compare the saved synthetic result and exception."}],
        sources=selection,
    )
    assert captured == []  # Preparing and saving the exact request makes no API call.
    saved = store.get(**prepared.intent_ref)["body"]
    request_bytes = store.read_blob(saved["blob"]["sha256"])
    assert hashlib.sha256(request_bytes).hexdigest() == saved["request_sha256"]
    request = json.loads(request_bytes)
    packets = [json.loads(message["content"])["source_packets"]
               for message in request["messages"] if message.get("role") == "user"
               and '"source_packets"' in message.get("content", "")]
    assert len(packets) == 1 and len(packets[0]) == 1
    packet = packets[0][0]
    assert packet["id"] == copy_ref["id"] and packet["version"] == copy_ref["version"]
    assert packet["copy_sha256"] == hashlib.sha256(content).hexdigest()
    assert packet["passage_refs"] == passage_refs
    assert packet["text"] == expected["text"]
    assert packet["text"].count(target) == 1, case
    if case == "T1":
        assert "first estimate" not in packet["text"]
    assert all(item in packet["text"] for item in required)
    assert bridge.load(prepared.intent_ref, for_dispatch=True).request.body == request


@pytest.mark.skipif(
    not all(shutil.which(name) for name in ("pdftotext", "pdftoppm", "pdfinfo", "tesseract")),
    reason="Native Poppler/Tesseract unavailable",
)
@pytest.mark.parametrize("case,page_number", [("O1", 1), ("O2", 3)])
def test_ocr_result_is_labelled_in_saved_model_request(tmp_path, monkeypatch, case, page_number):
    store, registry, bridge, base_call, _base_config, _base_copy, captured = setup(
        tmp_path / "state", unknown_policy="allow_metadata_only",
    )
    _preview_task(store)
    scope = {"mission_id": "mission", "run_id": "run",
             "actor_id": base_call["body"]["instance_id"], "task_id": "task"}
    monkeypatch.setattr(document_fixture, "SCOPE", scope)
    monkeypatch.setattr(format_fixture, "SCOPE", scope)
    raster = _raster_page(tmp_path, [
        "OCR result: 12.5 kWh saved per day.",
        "Not measured on winter weekends.",
    ])
    content = _pdf([raster] if case == "O1" else [
        ["Selectable page: the retrofit began in spring."], None, raster,
    ])
    store, _library, document, inspected = _inspected(
        tmp_path / "state", content, "application/pdf",
    )
    rendered = render_public_document_page(
        store, document, **scope, page_number=page_number, operation_id="render-for-model",
    )
    ocr = ocr_public_document_page(
        store, rendered["page_image_ref"], **scope, operation_id="ocr-for-model",
    )
    assert ocr["status"] == "ocr_unverified"
    assert "12.5 kWh" in ocr["text"] and "Not measured" in ocr["text"]
    copy_ref = inspected["copy_ref"]
    config = registry.save_configuration(
        "ocr-source-configuration", instance_id=scope["actor_id"], mission_id="mission",
        task_scope="source-comparison", provider="groq",
        endpoint="https://api.groq.com/openai/v1/chat/completions",
        model_id="openai/gpt-oss-120b", settings={
            "max_completion_tokens": 8192, "stream": False,
            "unknown_source_policy": "allow_metadata_only",
        }, reasoning_settings={"reasoning_effort": "high", "include_reasoning": True},
        instructions="Use only saved synthetic evidence.", skill_choices={},
        grants={f"common:{name}" for name in COMMON_TOOL_IDS},
        input_refs=[{**copy_ref, "sha256": content_hash(store.get(**copy_ref)["body"])}],
    )
    call = registry.start_call(
        "ocr-first-call", call_id="ocr-first", configuration_id=config["id"],
        mission_id="mission", task_id="task", attempt_id="ocr-attempt-1",
        loaded_skill_ids=[], exposed_tool_ids=["evidence_lookup"], require_common_tools=False,
    )
    call_ref = ref(call)
    # The image-only copy contributes metadata; OCR remains a separate tool result.
    selection = (SourceSelection(copy_ref, "unknown", False, "direct"),)
    bridge.prepare(
        call_id=call["id"], call_version=call["version"], phase="collect",
        messages=[{"role": "user", "content": "Read the selected synthetic PDF page."}],
        sources=selection,
    )
    response_blob = store.put_blob(canonical({
        "content": None, "tool_calls": [{"id": "ocr-call", "type": "function",
            "function": {"name": "read_document_ocr", "arguments": canonical({
                "document_ref": document, "page_number": page_number,
            })}}],
    }).encode())
    response_ref = store.transact("ocr-model-response", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "ocr-model-response", {
            "mission_id": "mission", "task_id": "task", "call_ref": call_ref,
            "status": "tool_requested", "blob": response_blob,
        },
    )), mission_id="mission")
    context = WorkingContext(store)
    context.save_tool_result(
        "ocr-model-tool-result", response_ref=response_ref, tool_call_id="ocr-call",
        name="read_document_ocr", content=canonical(ocr),
    )
    continuation = registry.start_call(
        "ocr-continuation-call", call_id="ocr-continuation", configuration_id=config["id"],
        mission_id="mission", task_id="task", attempt_id="ocr-attempt-2",
        loaded_skill_ids=[], exposed_tool_ids=["evidence_lookup"], require_common_tools=False,
    )
    task_ref = {"kind": "board_proposal", "id": "task", "version": 1}
    pack = context.assemble(
        ref(continuation), task_ref, source_refs=[copy_ref], tool_turn_refs=[response_ref],
    )
    prepared = bridge.prepare(
        call_id=continuation["id"], call_version=continuation["version"], phase="collect",
        messages=list(pack.messages), sources=selection,
    )
    assert captured == []
    saved = store.get(**prepared.intent_ref)["body"]
    request = json.loads(store.read_blob(saved["blob"]["sha256"]))
    delivered = [json.loads(message["content"]) for message in request["messages"]
                 if message.get("role") == "tool" and message.get("name") == "read_document_ocr"]
    assert len(delivered) == 1
    assert delivered[0]["status"] == "ocr_unverified"
    assert delivered[0]["text"] == ocr["text"]
    assert delivered[0]["ocr_ref"] == ocr["ocr_ref"]
    assert "page=" + str(page_number) in store.get(**ocr["ocr_ref"])["body"]["locator"]
    assert store.records("source_passage") == []
    assert bridge.load(prepared.intent_ref, for_dispatch=True).request.body == request
