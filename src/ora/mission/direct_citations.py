"""Preserve explicit passage links in direct answers using the producing call's checked inputs.

Only exact citation markers are resolved. This establishes a navigable source
reference, not whether a passage scientifically supports the surrounding claim.
"""

from __future__ import annotations

import re

from ora.coordination.board import ref
from ora.state import StateStore

_MARKER = re.compile(r"【([^【】\n]+)】|\[([^\[\]\n]+)\](?!\()")


def resolve_direct_citations(
    store: StateStore, text: str, intent: dict, eligible_sources: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Resolve bracketed IDs only against unambiguous, checked input passages."""
    candidates: dict[str, dict | None] = {}
    spans: dict[tuple[str, str], dict | None] = {}
    for decision in intent["body"].get("source_decisions", []):
        copy_ref = decision.get("copy_ref")
        if (
            copy_ref not in eligible_sources
            or decision.get("use") != "permitted"
            or decision.get("allowed_for_model") is not True
        ):
            continue
        for pointer in decision.get("passage_refs", []):
            passage = store.get(**pointer)
            if passage is None:
                continue
            body = passage["body"]
            span_id = body.get("span_id")
            if (
                passage["kind"] != "source_passage"
                or body.get("inspection_state") != "checked"
                or body.get("copy_ref") != copy_ref
                or not isinstance(span_id, str)
                or not span_id
            ):
                continue
            item = {
                "source_id": copy_ref["id"],
                "span_id": span_id,
                "copy_ref": copy_ref,
                "passage_ref": ref(passage),
                "producing_intent_ref": ref(intent),
                "input_status": "exact_passage_in_producing_call",
                "link_origin": "explicit_direct_response_marker",
            }
            key = (copy_ref["id"], span_id)
            if key not in spans:
                spans[key] = item
            elif spans[key] != item:
                spans[key] = None
            for marker in (passage["id"], f"{copy_ref['id']}::{span_id}"):
                if marker not in candidates:
                    candidates[marker] = item
                elif candidates[marker] != item:
                    candidates[marker] = None

    selected = []
    for match in _MARKER.finditer(text):
        item = candidates.get(match.group(1) or match.group(2))
        if (
            item is not None
            and spans.get((item["source_id"], item["span_id"])) == item
            and item not in selected
        ):
            selected.append(item)
    return (
        [{"sourceId": item["source_id"], "spanId": item["span_id"]} for item in selected],
        selected,
    )


def unresolved_source_markers(text: str, provenance: list[dict]) -> int:
    """Count explicit source-like markers left unlinked, without guessing their target."""
    resolved = {
        marker for item in provenance
        for marker in (item["passage_ref"]["id"], f"{item['source_id']}::{item['span_id']}")
    }
    return len({
        marker for match in _MARKER.finditer(text)
        if (marker := match.group(1) or match.group(2)).startswith(
            ("source-", "span-", "passage-")
        ) and marker not in resolved
    })
