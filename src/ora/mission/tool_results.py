"""Bound JSON projections of saved tool results without rerunning their effects.

Only host-selected projections reach this layer. Hash-bound character windows let
one very large item remain recoverable while the complete serialized reply stays
within a fixed input allowance.
"""

from __future__ import annotations

import hashlib

from ora.state import StateError
from ora.state.store import canonical

PAGEABLE_TOOLS = frozenset({"read_note", "read_plan", "search_notes", "board_read", "list_optional_work"})
RESULT_PAGE_SCHEMA = "ora.saved_tool_result_window.v1"
MAX_RESULT_PAGE_CHARS = 3000
MAX_RESULT_WINDOW_CHARS = 1800


class ResultProjectionChanged(StateError):
    """A continuation belongs to a projection no longer authorized for delivery."""


def result_window(
    projection: dict, *, result_ref: dict, original_content_sha256: str,
    offset: int, limit: int, projection_sha256: str, note_records: list[dict],
) -> dict:
    """Return an exact window; budget includes JSON escaping and continuation."""
    if type(offset) is not int or offset < 0:
        raise StateError("Saved result offset must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= MAX_RESULT_WINDOW_CHARS:
        raise StateError("Saved result limit must be between 1 and 1800")
    if not isinstance(projection_sha256, str) or (offset and not projection_sha256):
        raise StateError("Saved result continuation requires its projection hash")
    encoded = canonical(projection)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    if projection_sha256 and projection_sha256 != digest:
        raise ResultProjectionChanged("Saved result projection changed; restart at offset 0 with an empty hash")
    if offset > len(encoded):
        raise StateError("Saved result offset exceeds its projection")

    # Track only notes whose serialized text overlaps this window. An exact
    # record can appear more than once in a search result; retain every span.
    spans = []
    for note in note_records:
        if not isinstance(note.get("body", {}).get("text"), str):
            continue
        record_text = canonical(note)
        field = '"text":' + canonical(note["body"]["text"])
        inner = record_text.index(field) + len('"text":')
        after = 0
        while (start := encoded.find(record_text, after)) >= 0:
            spans.append((start + inner, start + inner + len(canonical(note["body"]["text"])),
                          {key: note[key] for key in ("kind", "id", "version")}))
            after = start + len(record_text)

    def page(end: int) -> dict:
        refs = {canonical(pointer): pointer for start, stop, pointer in spans
                if start < end and stop > offset}
        return {
            "schema": RESULT_PAGE_SCHEMA,
            "saved_tool_result_ref": result_ref,
            "original_content_sha256": original_content_sha256,
            "projection_sha256": digest,
            "encoding": "canonical_json_character_window",
            "offset": offset, "end_offset": end, "total_chars": len(encoded),
            "text": encoded[offset:end], "note_text_refs": list(refs.values()),
            "next_request": ({
                "tool": "read_tool_result", "result_id": result_ref["id"],
                "offset": end, "limit": limit, "projection_sha256": digest,
            } if end < len(encoded) else None),
        }

    end = min(offset + limit, len(encoded))
    result = page(end)
    while len(canonical(result)) > MAX_RESULT_PAGE_CHARS and end > offset:
        end -= max(1, (len(canonical(result)) - MAX_RESULT_PAGE_CHARS) // 2)
        end = max(offset, end)
        result = page(end)
    if len(canonical(result)) > MAX_RESULT_PAGE_CHARS or (end == offset and offset < len(encoded)):
        raise StateError("Saved result reference exceeds the reply allowance")
    return result
