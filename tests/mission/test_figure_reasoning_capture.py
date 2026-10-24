"""Figure reasoning stays exact, private, and tied to one permitted response."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ora.mission.figure_reading import FigureDispatchUncertain
from ora.state import StateStore
from tests.evidence.test_document_ocr import SCOPE
from tests.mission.test_figure_reading import Reply, _granted_page, _prepare, _reader


def _response(reasoning: str | None = None, *, finish_reason: str = "stop") -> dict:
    message = {"content": "The synthetic label is visible."}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return {
        "id": "synthetic-vision-response",
        "model": "qwen/qwen3.8-27b",
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 2048, "completion_tokens": 12, "total_tokens": 2060},
    }


@pytest.mark.parametrize("finish_reason,expected_status", [
    ("stop", "returned"), ("length", "truncated"),
])
def test_explicit_figure_reasoning_is_exact_private_and_exported(
    tmp_path, monkeypatch, finish_reason, expected_status,
):
    store = StateStore(tmp_path)
    document, page, ocr = _granted_page(store)
    raw_reasoning = "  Δοκιμή  \n\tline two 🙂 api_key=synthetic_fixture  "
    calls = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        return Reply(_response(raw_reasoning, finish_reason=finish_reason))

    reader = _reader(store, transport, monkeypatch)
    intent = _prepare(reader, document, page, ocr, operation=f"reasoning-{finish_reason}")
    outcome = reader.invoke_capture(intent, allow_live_calls=False)
    assert len(calls) == 1
    assert calls[0]["reasoning_effort"] == "none"
    receipt = store.get(**outcome.response_ref)
    assert receipt["body"]["reasoning_status"] == expected_status
    assert receipt["body"]["response_id"] == "synthetic-vision-response"
    reasoning_ref = receipt["body"]["reasoning_ref"]
    reasoning = store.get(**reasoning_ref)
    assert reasoning["body"]["status"] == expected_status
    assert reasoning["body"]["redacted"] is True
    raw_ref = reasoning["body"]["raw_ref"]
    raw_record = store.get(**raw_ref)
    assert store.read_blob(raw_record["body"]["blob"]["sha256"]) == raw_reasoning.encode()
    assert b"synthetic_fixture" not in store.read_blob(reasoning["body"]["blob"]["sha256"])
    assert any(link["relation"] == "has_restricted_reasoning"
               and link["target"] == reasoning_ref for link in store.links(receipt))
    assert any(link["relation"] == "has_exact_provider_text"
               and link["target"] == raw_ref for link in store.links(reasoning))
    assert raw_reasoning not in str(receipt)
    assert raw_reasoning not in str(reader.read_result(outcome.response_ref, scope=SCOPE))
    exported = list((Path(tmp_path) / "reasoning").glob("**/reasoning_trace.txt"))
    assert len(exported) == 1
    assert exported[0].read_bytes() == raw_reasoning.encode()
    assert reader.invoke_capture(intent, allow_live_calls=False) == outcome
    assert len(calls) == 1
    assert len(list((Path(tmp_path) / "reasoning").glob("**/reasoning_trace.txt"))) == 1
    if finish_reason == "stop":
        backup_path = Path(tmp_path) / "backup"
        store.backup(backup_path)
        restored = StateStore.restore(backup_path, Path(tmp_path) / "restored")
        saved_raw = restored.get(**raw_ref)
        assert restored.read_blob(saved_raw["body"]["blob"]["sha256"]) == raw_reasoning.encode()


def test_omitted_figure_reasoning_reports_not_requested_without_text(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    document, page, ocr = _granted_page(store)
    reader = _reader(store, lambda *_args, **_kwargs: Reply(_response()), monkeypatch)
    outcome = reader.invoke_capture(
        _prepare(reader, document, page, ocr, operation="reasoning-omitted"),
        allow_live_calls=False,
    )
    receipt = store.get(**outcome.response_ref)
    reasoning = store.get(**receipt["body"]["reasoning_ref"])
    assert receipt["body"]["reasoning_status"] == "not_requested"
    assert receipt["body"]["effective_reasoning_effort"] == "none"
    assert reasoning["body"]["raw_ref"] is None
    assert reasoning["body"]["blob"] is None
    assert not list((Path(tmp_path) / "reasoning").glob("**/reasoning_trace.txt"))


def test_uncertain_figure_dispatch_has_no_invented_reasoning(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    document, page, ocr = _granted_page(store)

    def lost(_request, *, timeout):
        raise TimeoutError("synthetic interrupted response")

    reader = _reader(store, lost, monkeypatch)
    intent = _prepare(reader, document, page, ocr, operation="reasoning-uncertain")
    with pytest.raises(FigureDispatchUncertain, match="unknown"):
        reader.invoke_capture(intent, allow_live_calls=False)
    assert reader.operation_state("reasoning-uncertain", scope=SCOPE)["dispatch_ref"]
    assert store.records("figure_response_receipt") == []
    assert store.records("restricted_reasoning") == []
    assert store.records("restricted_reasoning_raw") == []
    assert not list((Path(tmp_path) / "reasoning").glob("**/reasoning_trace.txt"))


@pytest.mark.parametrize("failure_type", [OSError, RuntimeError])
def test_local_export_failure_preserves_captured_figure_reasoning(
    tmp_path, monkeypatch, failure_type,
):
    store = StateStore(tmp_path)
    document, page, ocr = _granted_page(store)
    reader = _reader(store, lambda *_args, **_kwargs: Reply(_response("exact synthetic text")),
                     monkeypatch)

    def unavailable_export(*_args, **_kwargs):
        raise failure_type("synthetic local write failure")

    monkeypatch.setattr("ora.mission.figure_reading.export_receipt_reasoning",
                        unavailable_export)
    outcome = reader.invoke_capture(
        _prepare(reader, document, page, ocr, operation="reasoning-export-failed"),
        allow_live_calls=False,
    )
    assert outcome.status == "generated_unverified"
    receipt = store.get(**outcome.response_ref)
    reasoning = store.get(**receipt["body"]["reasoning_ref"])
    raw = store.get(**reasoning["body"]["raw_ref"])
    assert store.read_blob(raw["body"]["blob"]["sha256"]) == b"exact synthetic text"
    failures = store.records("reasoning_export_failure")
    assert len(failures) == 1
    assert failures[0]["body"]["error_class"] == failure_type.__name__
