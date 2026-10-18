"""Task-scoped lexical navigation over currently permitted retained source text.

Search results identify exact extracted-block coordinates. They are navigation
aid, not checked citation passages or evidence that a claim is supported.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque

from ora.coordination.board import ref
from ora.evidence.intake import MAX_COPY_BYTES, extract_copy_blocks
from ora.state import StateConflict, StateError
from ora.state.store import canonical

from .context import WorkingContext

_WORDS = re.compile(r"\w+", re.UNICODE)
_MAX_QUERY_CHARS = 160
_MAX_TERMS = 8
_MAX_PAGE_ITEMS = 10
_MAX_COPY_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 8 * 1024 * 1024
_SNIPPET_CHARS = 240
_RANK_WINDOW_CHARS = 320


class SavedPassageSearchError(StateError):
    """A saved-source search request or continuation is no longer valid."""


def _copy_byte_limit(copy: dict) -> int:
    """PDFs use the reader's retained-byte limit; other text keeps its smaller guard."""
    mime = copy["body"].get("mime_type", "").split(";", 1)[0].lower().strip()
    return MAX_COPY_BYTES if mime == "application/pdf" else _MAX_COPY_BYTES


def _best_match(text: str, terms: set[str]) -> tuple[int, int, int] | None:
    """Find the strongest small exact-text window using original char offsets."""
    nearby: deque[tuple[int, str]] = deque()
    counts: dict[str, int] = {}
    best: tuple[int, int, int] | None = None
    for token in _WORDS.finditer(text):
        word = token.group().casefold()
        if word not in terms:
            continue
        position = token.start()
        nearby.append((position, word))
        counts[word] = counts.get(word, 0) + 1
        while nearby and position - nearby[0][0] >= _RANK_WINDOW_CHARS:
            old_position, old_word = nearby.popleft()
            counts[old_word] -= 1
            if not counts[old_word]:
                del counts[old_word]
        first = nearby[0][0]
        candidate = (len(counts), len(nearby), first)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    return best


def _permitted_copy(context: WorkingContext, pointer: dict) -> tuple[dict, dict] | None:
    store = context.store
    if (type(pointer) is not dict or set(pointer) != {"kind", "id", "version"}
            or pointer["kind"] != "source_copy" or not isinstance(pointer["id"], str)
            or not pointer["id"] or type(pointer["version"]) is not int
            or pointer["version"] < 1):
        raise SavedPassageSearchError("Task contains an invalid exact source-copy reference")
    copy = store.get(**pointer)
    if copy is None or copy["kind"] != "source_copy":
        raise SavedPassageSearchError("Task source copy is unavailable")
    status = context._source_status(copy)
    if (status["model_input_status"] != "permitted"
            or status["current_model_input_status"] != "permitted"):
        return None
    if copy["body"].get("inspection_state") != "checked":
        return None
    use_ref = status["selected_source_use_ref"]
    use = store.get(**use_ref) if use_ref else None
    if (use is None or use["body"].get("copy_ref") != pointer
            or use["body"].get("policy_status") != "granted"
            or "model_input" not in use["body"].get("allowed_uses", [])):
        return None
    quality_ref = use["body"].get("quality_ref")
    quality = store.get(**quality_ref) if isinstance(quality_ref, dict) else None
    if (quality is None or quality["kind"] != "intake_quality"
            or quality["body"].get("classification")
            not in {"abstract", "passage", "partial", "full_text", "web_document"}):
        return None
    return copy, use


def task_text_tool_availability(
    context: WorkingContext, control: dict,
) -> tuple[bool, bool]:
    """Use saved task permission/quality metadata to decide tool attachment.

    Search's byte bound is stricter than reading one window. Neither decision
    parses the retained document; both tools recheck exact use when called.
    """
    task_refs = control.get("body", {}).get("source_refs", [])
    if not isinstance(task_refs, list):
        return False, False
    readable = searchable = False
    for pointer in task_refs:
        if not isinstance(pointer, dict):
            continue
        try:
            eligible = _permitted_copy(context, pointer)
        except SavedPassageSearchError:
            continue
        if eligible is None:
            continue
        copy, use = eligible
        blob = copy["body"].get("blob")
        size = blob.get("size") if isinstance(blob, dict) else None
        quality = context.store.get(**use["body"]["quality_ref"])
        length = quality["body"].get("inspected_text_length") if quality else None
        if (type(size) is not int or size <= 0 or type(length) is not int
                or length <= 0):
            continue
        readable = True
        searchable |= size <= _copy_byte_limit(copy) and length <= _MAX_TOTAL_BYTES
        if readable and searchable:
            break
    return readable, searchable


def search_saved_passages(
    context: WorkingContext,
    control: dict,
    query: str,
    *,
    cursor: int = 0,
    limit: int = 5,
    projection_sha256: str = "",
) -> dict:
    """Search this task's authorized extracted text and page stable read coordinates.

    A zero-hit answer applies only to the searched retained extracted text.
    Oversized or unavailable permitted copies are reported as an incomplete scan.
    """
    if (not isinstance(query, str) or not query.strip()
            or len(query) > _MAX_QUERY_CHARS):
        raise SavedPassageSearchError("Search query must contain 1 to 160 characters")
    terms = set(_WORDS.findall(query.casefold()))
    if not terms or len(terms) > _MAX_TERMS:
        raise SavedPassageSearchError("Search query needs 1 to 8 distinct word terms")
    if (type(cursor) is not int or cursor < 0 or type(limit) is not int
            or not 1 <= limit <= _MAX_PAGE_ITEMS
            or not isinstance(projection_sha256, str)
            or cursor and not projection_sha256):
        raise SavedPassageSearchError("Search needs a bounded page and hash-bound continuation")
    store = context.store
    if (not isinstance(control, dict) or control.get("kind") != "mission_task_control"
            or not isinstance(control.get("body"), dict)):
        raise SavedPassageSearchError("A current task control record is required")
    control_ref = ref(control)
    current_control = store.get("mission_task_control", control_ref["id"])
    if current_control is None or ref(current_control) != control_ref:
        raise StateConflict("Task control changed; restart saved-passage search")
    body = control["body"]
    source_refs = body.get("source_refs", [])
    if not isinstance(source_refs, list):
        raise SavedPassageSearchError("Task source references are invalid")

    authorized = []
    seen = set()
    for pointer in source_refs:
        eligible = _permitted_copy(context, pointer)
        if eligible is None:
            continue
        if (pointer["id"], pointer["version"]) in seen:
            continue
        seen.add((pointer["id"], pointer["version"]))
        authorized.append((pointer, *eligible))

    hits = []
    searched = []
    omitted = []
    scanned_bytes = 0
    ordinary_scanned_bytes = 0
    scanned_text_bytes = 0
    for pointer, copy, use in authorized:
        blob = copy["body"].get("blob")
        size = blob.get("size") if isinstance(blob, dict) else None
        if type(size) is not int or size < 0:
            omitted.append({"copy_ref": pointer, "reason": "retained_bytes_unavailable"})
            continue
        is_pdf = _copy_byte_limit(copy) == MAX_COPY_BYTES
        if (size > _copy_byte_limit(copy) or scanned_bytes + size > MAX_COPY_BYTES
                or not is_pdf and ordinary_scanned_bytes + size > _MAX_TOTAL_BYTES):
            omitted.append({"copy_ref": pointer, "reason": "search_byte_budget"})
            continue
        content = store.read_blob(blob["sha256"])
        digest = hashlib.sha256(content).hexdigest()
        quality_ref = use["body"]["quality_ref"]
        quality = store.get(**quality_ref)
        if (digest != copy["body"].get("content_sha256")
                or digest != use["body"].get("source_sha256")
                or digest != quality["body"].get("source_sha256")):
            raise SavedPassageSearchError("Retained source and checked-use hashes disagree")
        blocks, parser = extract_copy_blocks(content, copy["body"])
        text_bytes = sum(len(block.text.encode("utf-8")) for block in blocks)
        if scanned_text_bytes + text_bytes > _MAX_TOTAL_BYTES:
            omitted.append({"copy_ref": pointer, "reason": "search_byte_budget"})
            continue
        scanned_bytes += size
        scanned_text_bytes += text_bytes
        if not is_pdf:
            ordinary_scanned_bytes += size
        searched.append({
            "copy_ref": pointer, "source_use_ref": ref(use),
            "content_sha256": digest, "parser": parser,
            "total_blocks": len(blocks), "original_source_partial": bool(
                quality["body"].get("truncated")
            ),
        })
        for index, block in enumerate(blocks):
            match = _best_match(block.text, terms)
            if match is None:
                continue
            distinct, occurrences, position = match
            start = max(0, position - 40)
            end = min(len(block.text), start + _SNIPPET_CHARS)
            hits.append({
                "copy_ref": pointer,
                "block_offset": index,
                "block_locator": block.locator,
                "match_char_offset": position,
                "snippet_char_start": start,
                "snippet_char_end": end,
                "snippet": block.text[start:end],
                "matched_query_terms": distinct,
                "term_occurrences_in_rank_window": occurrences,
                "next_request": {
                    "tool": "read_source_window", "source_id": pointer["id"],
                    "source_version": pointer["version"], "block_offset": index,
                    "char_offset": start, "char_limit": 1200,
                    "selection_mode": "append",
                },
            })
    hits.sort(key=lambda item: (
        -item["matched_query_terms"], -item["term_occurrences_in_rank_window"],
        item["copy_ref"]["id"], item["copy_ref"]["version"], item["block_offset"],
    ))
    basis = {
        "control_id": control_ref["id"],
        "scope": {key: body.get(key) for key in ("mission_id", "task_id", "actor_id")},
        "query_terms": sorted(terms),
        "searched": searched, "omitted": omitted,
        "ranked_positions": [
            [item["copy_ref"], item["block_offset"], item["match_char_offset"],
             item["matched_query_terms"], item["term_occurrences_in_rank_window"]]
            for item in hits
        ],
    }
    digest = hashlib.sha256(canonical(basis).encode()).hexdigest()
    if projection_sha256 and projection_sha256 != digest:
        raise StateConflict("Saved-passage search changed; restart at cursor 0")
    if cursor > len(hits):
        raise SavedPassageSearchError("Search cursor exceeds current results")
    # Current source use and control are checked again after reading all bytes.
    current_control = store.get("mission_task_control", control_ref["id"])
    if current_control is None or ref(current_control) != control_ref:
        raise StateConflict("Task control changed during saved-passage search")
    for pointer, _, use in authorized:
        current = _permitted_copy(context, pointer)
        if current is None or ref(current[1]) != ref(use):
            raise StateConflict("Source permission changed during saved-passage search")

    page = hits[cursor:cursor + limit]
    # Counts and no-match statements also depend on the searched text, including
    # copies outside this page. Revocation must invalidate those derived facts.
    dependencies = [item["copy_ref"] for item in searched]
    next_cursor = cursor + len(page) if cursor + len(page) < len(hits) else None
    return {
        "schema": "ora.saved_passage_search.v1", "method": "extracted_block_token_overlap_v1",
        "control_ref": control_ref, "query": query, "projection_sha256": digest,
        "cursor": cursor, "next_cursor": next_cursor,
        "next_request": ({
            "tool": "search_saved_passages", "query": query,
            "cursor": next_cursor, "limit": limit, "projection_sha256": digest,
        } if next_cursor is not None else None),
        "results": page, "source_dependency_refs": dependencies,
        "result_count": len(hits), "searched_copy_count": len(searched),
        "searched_extracted_block_count": sum(item["total_blocks"] for item in searched),
        "omitted_permitted_copies": omitted,
        "coverage": "complete_retained_extracted_text" if not omitted else "partial_retained_extracted_text",
        "interpretation": (
            "Lexical navigation only. No match means no token match in searched extracted text; "
            "it does not establish absence from an original or uninspected source."
        ),
    }
