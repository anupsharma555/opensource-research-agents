"""Small, exact-location navigation aids for retained extracted source text.

These labels and offsets help choose a later read. They do not inspect a
passage, establish relevance, or change the source's immutable blocks.
"""

from __future__ import annotations

from typing import Any

_MAX_SECTION_ENTRIES = 8


def source_navigation(blocks: list[Any], *, mime_type: str, block_offset: int) -> dict[str, Any]:
    """Describe observed section transitions without guessing document meaning."""
    result: dict[str, Any] = {
        "scope": "navigation_only_not_checked_claim_evidence",
        "total_blocks": len(blocks),
        "current_block_offset": block_offset,
    }
    if mime_type not in {"text/html", "application/xml", "text/xml", "application/jats+xml"}:
        return result
    transitions: list[dict[str, Any]] = []
    previous = None
    for index, block in enumerate(blocks):
        section = block.locator.split(";", 1)[0]
        if not section.startswith("section=") or section == previous:
            continue
        previous = section
        transitions.append({
            "label": section[len("section="):],
            "block_offset": index,
            "locator": block.locator,
        })
    # Show nearby headings first, with a bounded view of the beginning and end.
    nearby = [entry for entry in transitions if entry["block_offset"] >= block_offset]
    chosen = nearby[:_MAX_SECTION_ENTRIES]
    if len(chosen) < _MAX_SECTION_ENTRIES:
        preceding = [entry for entry in transitions if entry["block_offset"] < block_offset]
        chosen = preceding[-(_MAX_SECTION_ENTRIES - len(chosen)):] + chosen
    result["section_count"] = len(transitions)
    result["sections"] = chosen
    result["omitted_section_count"] = len(transitions) - len(chosen)
    return result
