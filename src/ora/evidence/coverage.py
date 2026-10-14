"""Read-only coverage of exact retained extracted text, without source text output.

Coverage means saved selected passages and reading windows span the extracted
text in this retained copy. It says nothing about publication completeness,
scientific support, or whether a model comprehended the displayed text.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ora.state import IntegrityFault, StateStore

from .intake import (
    EXACT_SUBMISSION_PARSER_VERSION,
    EXACT_SUBMISSION_VALIDATION,
    _exact_submitted_text,
    _is_exact_submitted_passage,
    extract_blocks,
)
from .library import EvidenceError
from .windows import _window_slices

_COPY_CLASSES = frozenset({"abstract", "passage", "partial", "full_text"})
_DISPLAY_INTERVALS = 6


class _UnknownCoverage(ValueError):
    """Saved evidence cannot support a factual coverage calculation."""


def _pointer(pointer: object, kind: str) -> bool:
    return (
        type(pointer) is dict
        and set(pointer) == {"kind", "id", "version"}
        and pointer["kind"] == kind
        and type(pointer["id"]) is str
        and bool(pointer["id"])
        and type(pointer["version"]) is int
        and pointer["version"] >= 1
    )


def _record(store: StateStore, pointer: object, kind: str) -> dict:
    if not _pointer(pointer, kind):
        raise _UnknownCoverage("invalid_exact_reference")
    record = store.get(**pointer)
    if record is None:
        raise _UnknownCoverage("missing_exact_record")
    return record


def _interval(block_index: int, start: int, end: int) -> dict[str, int]:
    return {"block_offset": block_index, "char_start": start, "char_end": end}


def _passage_interval(store: StateStore, pointer: object, copy_ref: dict, blocks: list) -> dict[str, int]:
    passage = _record(store, pointer, "source_passage")
    body = passage["body"]
    if body.get("copy_ref") != copy_ref or body.get("inspection_state") != "checked":
        raise _UnknownCoverage("passage_identity_mismatch")
    locator = body.get("locator")
    text = body.get("text")
    if not isinstance(locator, str) or not isinstance(text, str):
        raise _UnknownCoverage("invalid_passage_locator")
    prefix, marker, bounds = locator.rpartition("; chars=")
    if not marker or not re.fullmatch(r"\d+:\d+", bounds):
        raise _UnknownCoverage("invalid_passage_locator")
    start, end = map(int, bounds.split(":"))
    matches = [
        index for index, block in enumerate(blocks)
        if block.locator == prefix and 0 <= start < end <= len(block.text)
        and block.text[start:end] == text
    ]
    if len(matches) != 1:
        raise _UnknownCoverage("passage_locator_not_unique_or_exact")
    return _interval(matches[0], start, end)


def _merged_intervals(blocks: list, intervals: list[dict[str, int]]) -> tuple[list[dict], list[dict]]:
    grouped: list[list[tuple[int, int]]] = [[] for _ in blocks]
    for item in intervals:
        grouped[item["block_offset"]].append((item["char_start"], item["char_end"]))
    covered, uncovered = [], []
    for index, (block, spans) in enumerate(zip(blocks, grouped, strict=True)):
        merged: list[list[int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        previous = 0
        for start, end in merged:
            if previous < start:
                uncovered.append(_interval(index, previous, start))
            covered.append(_interval(index, start, end))
            previous = end
        if previous < len(block.text):
            uncovered.append(_interval(index, previous, len(block.text)))
    return covered, uncovered


def extracted_text_coverage(
    store: StateStore,
    copy_ref: dict,
    selected_passage_refs: list[dict],
    read_window_refs: list[dict],
) -> dict[str, Any]:
    """Return compact verified intervals for one checked, retained source copy.

    Every supplied passage and window must resolve against the exact retained
    bytes. An unsupported or inconsistent copy is `unknown`, not a mission-wide
    refusal. Returned intervals are display-bounded; all intervals are counted.
    """
    result: dict[str, Any] = {
        "schema": "ora.evidence.extracted_text_coverage.v1",
        "copy_ref": copy_ref,
        "scope": "retained_extracted_text_only",
        "status": "unknown",
        "reason": None,
        "original_source_partial": None,
        "source_surface": None,
        "total_blocks": None,
        "total_chars": None,
        "covered_chars": None,
        "uncovered_chars": None,
        "covered_interval_count": None,
        "uncovered_interval_count": None,
        "covered_intervals": [],
        "uncovered_intervals": [],
        "next_unread_cursor": None,
        "selected_passage_count": len(selected_passage_refs) if isinstance(selected_passage_refs, list) else None,
        "window_count": None,
    }
    try:
        if not isinstance(selected_passage_refs, list) or not isinstance(read_window_refs, list):
            raise _UnknownCoverage("invalid_reference_lists")
        copy = _record(store, copy_ref, "source_copy")
        body = copy["body"]
        blob = body.get("blob")
        if body.get("inspection_state") != "checked" or not isinstance(blob, dict):
            raise _UnknownCoverage("no_checked_retained_text")
        content = store.read_blob(blob["sha256"])
        digest = hashlib.sha256(content).hexdigest()
        if digest != body.get("content_sha256") or len(content) != blob.get("size"):
            raise _UnknownCoverage("source_hash_mismatch")
        use = store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
        if use is None or use["body"].get("copy_ref") != copy_ref:
            raise _UnknownCoverage("source_quality_unavailable")
        quality = _record(store, use["body"].get("quality_ref"), "intake_quality")
        quality_body = quality["body"]
        if (use["body"].get("source_sha256") != digest
                or quality_body.get("source_sha256") != digest
                or quality_body.get("classification") not in _COPY_CLASSES
                or type(quality_body.get("truncated")) is not bool):
            raise _UnknownCoverage("source_quality_mismatch")
        result["source_surface"] = quality_body["classification"]
        result["original_source_partial"] = (
            quality_body["truncated"]
            or quality_body["classification"] in {"passage", "partial"}
            or quality_body.get("document_structure") in {"jats_partial_body", "html_partial_body"}
        )
        blocks, parser = extract_blocks(
            content, body["mime_type"],
            allow_partial_xml=(body.get("mapping_version") or "").endswith("partial_xml=True"),
        )
        inspected_mapping = (
            quality_body.get("parser") == parser
            and quality_body.get("inspected_blocks") == len(blocks)
            and quality_body.get("inspected_text_length")
            == sum(len(block.text) for block in blocks)
        )
        supplied_without_mapping = False
        if not inspected_mapping and body.get("mime_type") == "text/plain":
            encounter = _record(store, body.get("encounter"), "source_encounter")
            supplied_without_mapping = (
                encounter["body"].get("route") == "user_supplied"
                and quality_body.get("parser") is None
                and quality_body.get("inspected_blocks") is None
                and quality_body.get("inspected_text_length") is None
            )
        submitted_text = _exact_submitted_text(store, copy, content)
        exact_submission_mapping = (
            submitted_text is not None
            and quality_body.get("parser") == EXACT_SUBMISSION_VALIDATION
            and quality_body.get("parser_version") == EXACT_SUBMISSION_PARSER_VERSION
            and quality_body.get("copy_ref") == copy_ref
            and quality_body.get("byte_length") == len(content)
            and quality_body.get("stored_content_type") == "text/plain"
            and quality_body.get("source_version") == body.get("source_version")
            and quality_body.get("inspected_text_length") == len(submitted_text)
            and quality_body.get("selected_locators")
            == [f"submitted text; chars=0:{len(submitted_text)}"]
            and quality_body.get("completeness_scope") == "exact_submitted_text_only"
        )
        if not blocks or not (
            inspected_mapping or supplied_without_mapping or exact_submission_mapping
        ):
            raise _UnknownCoverage("extraction_mapping_unavailable")

        intervals = []
        for pointer in selected_passage_refs:
            passage = _record(store, pointer, "source_passage")
            if _is_exact_submitted_passage(passage, copy, submitted_text):
                # The raw submission covers every extracted block, including
                # when UTF-8 extraction removes an initial byte-order mark.
                intervals.extend(_interval(index, 0, len(block.text))
                                 for index, block in enumerate(blocks))
            else:
                intervals.append(_passage_interval(store, pointer, copy_ref, blocks))
        matching_windows = 0
        for window_ref in read_window_refs:
            window = _record(store, window_ref, "source_read_window")
            saved = window["body"]
            if saved.get("copy_ref") != copy_ref:
                continue
            matching_windows += 1
            window_use = _record(store, saved.get("source_use_ref"), "source_use")
            window_quality = _record(store, saved.get("quality_ref"), "intake_quality")
            if (saved.get("content_sha256") != digest
                    or window_use["body"].get("copy_ref") != copy_ref
                    or window_use["body"].get("source_sha256") != digest
                    or window_use["body"].get("quality_ref") != saved.get("quality_ref")
                    or window_quality["body"].get("source_sha256") != digest
                    or saved.get("parser") != parser
                    or saved.get("total_blocks") != len(blocks)):
                raise _UnknownCoverage("window_identity_mismatch")
            offsets = (saved.get("block_offset"), saved.get("block_limit"),
                       saved.get("char_offset"), saved.get("char_limit"))
            if any(type(value) is not int or value < 0 for value in offsets) or offsets[1] < 1 or offsets[3] < 1:
                raise _UnknownCoverage("invalid_window_offsets")
            selected, next_cursor, _ = _window_slices(
                blocks, block_offset=offsets[0], block_limit=offsets[1],
                char_offset=offsets[2], char_limit=offsets[3],
            )
            if (saved.get("next_cursor") != next_cursor
                    or saved.get("selected_locators") != [item["locator"] for item in selected]
                    or len(saved.get("passage_refs", [])) != len(selected)):
                raise _UnknownCoverage("window_selection_mismatch")
            for pointer, item in zip(saved["passage_refs"], selected, strict=True):
                passage = _record(store, pointer, "source_passage")
                if (passage["body"].get("copy_ref") != copy_ref
                        or passage["body"].get("inspection_state") != "checked"
                        or passage["body"].get("locator") != item["locator"]
                        or passage["body"].get("text") != item["text"]):
                    raise _UnknownCoverage("window_passage_mismatch")
                intervals.append(_interval(item["block_index"], item["start"], item["end"]))

        covered, uncovered = _merged_intervals(blocks, intervals)
        total_chars = sum(len(block.text) for block in blocks)
        if total_chars == 0:
            raise _UnknownCoverage("empty_extracted_text")
        covered_chars = sum(item["char_end"] - item["char_start"] for item in covered)
        uncovered_chars = total_chars - covered_chars
        result.update(
            status="complete_extracted_text" if uncovered_chars == 0 else "incomplete_extracted_text",
            total_blocks=len(blocks), total_chars=total_chars,
            covered_chars=covered_chars, uncovered_chars=uncovered_chars,
            covered_interval_count=len(covered), uncovered_interval_count=len(uncovered),
            covered_intervals=covered[:_DISPLAY_INTERVALS],
            uncovered_intervals=uncovered[:_DISPLAY_INTERVALS],
            window_count=matching_windows,
            next_unread_cursor=(
                {"block_offset": uncovered[0]["block_offset"], "char_offset": uncovered[0]["char_start"]}
                if uncovered else None
            ),
        )
    except _UnknownCoverage as exc:
        result["reason"] = str(exc)
    except (EvidenceError, IntegrityFault, KeyError, TypeError, ValueError):
        result["reason"] = "invalid_or_unsupported_evidence"
    return result
