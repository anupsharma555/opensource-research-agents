"""Mixed HTML evidence retains exact text from public intake to model input."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.evidence import intake
from ora.evidence.intake import IntakeService, checked_model_text, extract_copy_blocks
from ora.evidence.library import EvidenceError
from ora.evidence.windows import read_source_window
from ora.models import SessionError, SourceSelection
from tests.evidence import test_document_ocr as document_fixture
from tests.evidence import test_source_format_walkthroughs as format_fixture
from tests.evidence.test_source_format_walkthroughs import _inspected
from tests.models.test_session import setup

HTML = b"""<html><body><main><article>
<h1>Synthetic dose comparison</h1>
<p>Dose was <span>2.5</span> mg/kg; change was 6 points.</p>
<pre>\nconst dose = 2.5;\n\nreturn dose * 2;\n</pre>
<table><caption>Score change (points)</caption>
<thead><tr><th>Arm</th><th>Baseline</th><th>Week 6</th></tr></thead>
<tbody><tr><th scope="row">Active</th><td>20</td><td>14<sup>a</sup></td></tr></tbody>
<tfoot><tr><td colspan="3">a Only completers were counted.</td></tr></tfoot>
</table>
<details><summary>Limitations</summary><p>Week 6 excludes missing visits.</p></details>
<dl><dt>Metric</dt><dd>6 points at week 6.</dd>
<dt>Limit</dt><dd>Only completers counted.</dd></dl>
<p style="white-space: pre-wrap">Line one\n  indented line two\n</p>
</article></main></body></html>"""
SCOPE = {"mission_id": "mission", "run_id": "run",
         "actor_id": "synthetic-peer", "task_id": "task"}


def _checked(tmp_path, monkeypatch):
    monkeypatch.setattr(document_fixture, "SCOPE", SCOPE)
    monkeypatch.setattr(format_fixture, "SCOPE", SCOPE)
    return _inspected(tmp_path, HTML, "text/html")


def test_mixed_html_source_reaches_exact_saved_model_request(tmp_path, monkeypatch):
    store, library, _document, inspected = _checked(tmp_path, monkeypatch)
    copy_ref = inspected["copy_ref"]
    copy_body = store.get(**copy_ref)["body"]
    assert copy_body["content_sha256"] == hashlib.sha256(HTML).hexdigest()
    assert store.read_blob(copy_body["blob"]["sha256"]) == HTML
    blocks, parser = extract_copy_blocks(HTML, copy_body)
    assert parser == "stdlib-htmlparser-structured-v5"
    assert [blocks[index].text for index in range(1, 11)] == [
        "Dose was 2.5 mg/kg; change was 6 points.",
        "const dose = 2.5;\n\nreturn dose * 2;\n",
        "Table: Score change (points) | Row: Arm: Active | Baseline: 20",
        "Table: Score change (points) | Row: Arm: Active | Week 6: 14a",
        "Table: Score change (points) | a Only completers were counted.",
        "Limitations",
        "Week 6 excludes missing visits.",
        "Term: Metric | Definition: 6 points at week 6.",
        "Term: Limit | Definition: Only completers counted.",
        "Line one\n  indented line two\n",
    ]
    selected = list(range(1, 11))
    windows = [read_source_window(
        library, f"mixed-html-{index}", copy_ref,
        block_offset=index, block_limit=1, char_limit=4000,
    ) for index in selected]
    refs = [window["passage_refs"][0] for window in windows]
    for index, window, passage_ref in zip(selected, windows, refs, strict=True):
        assert window["passages"][0]["text_preview"] == blocks[index].text
        assert window["passages"][0]["locator"] == (
            f"{blocks[index].locator}; chars=0:{len(blocks[index].text)}"
        )
        resolved = IntakeService(library).source_for_model(passage_ref)
        assert resolved["text"] == blocks[index].text
        assert resolved["source_use_ref"] == inspected["source_use_ref"]
    checked = checked_model_text(library, copy_ref, refs)
    assert checked["content_sha256"] == hashlib.sha256(HTML).hexdigest()
    assert all(checked["text"].count(blocks[index].text) == 1 for index in selected)

    reopened, _registry, bridge, call, _config, selected_copy, captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    assert selected_copy == copy_ref
    prepared = bridge.prepare(
        call_id=call["id"], call_version=call["version"], phase="collect",
        messages=[{"role": "user", "content": "Read the selected values and caveat."}],
        sources=(SourceSelection(copy_ref, "permitted", True, "direct", tuple(refs)),),
    )
    assert captured == []
    intent = reopened.get(**prepared.intent_ref)["body"]
    request_bytes = reopened.read_blob(intent["blob"]["sha256"])
    assert hashlib.sha256(request_bytes).hexdigest() == intent["request_sha256"]
    request = json.loads(request_bytes)
    packets = [json.loads(message["content"])["source_packets"]
               for message in request["messages"] if message.get("role") == "user"
               and '"source_packets"' in message.get("content", "")]
    assert len(packets) == 1 and len(packets[0]) == 1
    packet = packets[0][0]
    assert packet["copy_sha256"] == hashlib.sha256(HTML).hexdigest()
    assert packet["passage_refs"] == refs
    assert packet["text"] == checked["text"]
    assert all(packet["text"].count(blocks[index].text) == 1 for index in selected)
    assert bridge.load(prepared.intent_ref, for_dispatch=True).request.body == request

    use = reopened.get(**inspected["source_use_ref"])
    reopened.transact("revoke-mixed-html", {}, lambda tx: tx.put(
        "source_use", use["id"], {
            **use["body"], "allowed_uses": [], "policy_status": "revoked",
        }, expected_version=use["version"]), mission_id=SCOPE["mission_id"])
    with pytest.raises(EvidenceError, match="does not grant model input"):
        read_source_window(library, "mixed-html-after-revocation", copy_ref, block_offset=1)
    with pytest.raises(EvidenceError, match="does not grant model input"):
        IntakeService(library).source_for_model(refs[0])
    with pytest.raises(SessionError):
        bridge.load(prepared.intent_ref, for_dispatch=True)


def test_historical_v3_copy_replays_its_original_text_and_locators(tmp_path, monkeypatch):
    original_extract = intake.extract_blocks

    def v3_extract(content, content_type, *, allow_partial_xml=False):
        return original_extract(
            content, content_type, allow_partial_xml=allow_partial_xml,
            html_parser_version="stdlib-htmlparser-structured-v3",
        )

    with monkeypatch.context() as patch:
        patch.setattr(intake, "extract_blocks", v3_extract)
        store, library, _document, inspected = _checked(tmp_path, monkeypatch)
    copy_ref = inspected["copy_ref"]
    copy_body = store.get(**copy_ref)["body"]
    assert "stdlib-htmlparser-structured-v3" in copy_body["mapping_version"]
    old_blocks, parser = extract_copy_blocks(HTML, copy_body)
    assert parser == "stdlib-htmlparser-structured-v3"
    assert old_blocks[6].text == "LimitationsWeek 6 excludes missing visits."
    assert old_blocks[11].text == "Line one indented line two"
    window = read_source_window(
        library, "mixed-html-v3-details", copy_ref,
        block_offset=6, block_limit=1,
    )
    passage_ref = window["passage_refs"][0]
    assert window["passages"][0]["locator"].startswith(old_blocks[6].locator)
    assert IntakeService(library).source_for_model(passage_ref)["text"] == old_blocks[6].text
    packet = checked_model_text(library, copy_ref, [passage_ref])
    assert old_blocks[6].text in packet["text"]
    reopened, _registry, bridge, call, _config, _copy_ref, captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    prepared = bridge.prepare(
        call_id=call["id"], call_version=call["version"], phase="collect",
        messages=[{"role": "user", "content": "Read this historical selected passage."}],
        sources=(SourceSelection(copy_ref, "permitted", True, "direct", (passage_ref,)),),
    )
    assert captured == []
    intent = reopened.get(**prepared.intent_ref)["body"]
    request = json.loads(reopened.read_blob(intent["blob"]["sha256"]))
    packets = [json.loads(message["content"])["source_packets"]
               for message in request["messages"] if message.get("role") == "user"
               and '"source_packets"' in message.get("content", "")]
    assert len(packets) == 1 and len(packets[0]) == 1
    assert packets[0][0]["text"] == packet["text"]
    assert packets[0][0]["text"].count(old_blocks[6].text) == 1
