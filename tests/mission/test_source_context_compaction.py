"""Selected-source packing keeps factual fetch feedback and recovery options."""

from ora.mission.context import WorkingContext


def test_selected_html_fetch_removes_only_repeated_aliases_and_advice():
    copy_ref = {"kind": "source_copy", "id": "synthetic-copy", "version": 2}
    document_ref = {"kind": "public_document_fetch", "id": "synthetic-document", "version": 1}
    feedback = {"retained_document_reused": True, "retained_copy_reused": True,
                "new_http_request": False, "latest_read": {"next_cursor": 3}}
    fetched = {
        "status": "inspected", "classification": "web_document",
        "copy_ref": copy_ref, "source_id": copy_ref["id"],
        "source_version": copy_ref["version"], "document_ref": document_ref,
        "source_url": "https://example.org/synthetic-guidance", "hit_id": "synthetic-hit",
        "fetch_feedback": feedback, "next_action": "Read the retained text",
    }

    compacted = WorkingContext._compact_selected_fetch_result(
        fetched, (copy_ref,), mime_type="text/html",
    )
    assert compacted == {key: value for key, value in fetched.items()
                         if key not in {"source_id", "source_version", "next_action"}}
    assert compacted["fetch_feedback"] == feedback
    assert compacted["source_url"] == fetched["source_url"]
    assert compacted["document_ref"] == document_ref

    # An unselected or changed copy must retain the complete saved result.
    assert WorkingContext._compact_selected_fetch_result(
        fetched, (), mime_type="text/html",
    ) is None
    assert WorkingContext._compact_selected_fetch_result(
        {**fetched, "source_version": 1}, (copy_ref,), mime_type="text/html",
    ) is None


def test_non_html_fetch_keeps_specific_reader_guidance():
    copy_ref = {"kind": "source_copy", "id": "synthetic-scan", "version": 1}
    fetched = {
        "status": "inspected", "classification": "scanned_document",
        "copy_ref": copy_ref, "source_id": copy_ref["id"],
        "source_version": 1, "next_action": "Use OCR for this scanned page",
    }
    assert WorkingContext._compact_selected_fetch_result(
        fetched, (copy_ref,), mime_type="application/pdf",
    ) is None
    # web_document can include a text-bearing PDF; classification alone is insufficient.
    assert WorkingContext._compact_selected_fetch_result(
        {**fetched, "classification": "web_document"}, (copy_ref,),
        mime_type="application/pdf",
    ) is None
