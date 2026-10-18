"""Choose a bounded Poppler text order per physical PDF page.

Poppler's layout mode retains aligned table rows but can interleave a sidebar
with article prose. Its ordinary reading order keeps independent columns
contiguous. Bounding boxes select that mode only for clear, non-tabular columns;
all other pages retain the proven layout extraction.
"""

from __future__ import annotations

import math
import os
import select
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from xml.etree import ElementTree as ET

PDF_PARSER_VERSION = "poppler-bbox-layout-reading-order-v2"
_XHTML = "{http://www.w3.org/1999/xhtml}"
_MAX_INPUT_BYTES = 32 * 1024 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_MAX_PAGES = 1_000
_MAX_BLOCKS = 500_000
_MAX_WORDS = 2_000_000
_MAX_REORDER_BOXES = 500
_MAX_COLUMN_PAIRS = 256


@dataclass(frozen=True)
class PDFBlock:
    text: str
    locator: str


@dataclass(frozen=True)
class _Box:
    x0: float
    y0: float
    x1: float
    y1: float
    chars: int
    text: str


class PDFExtractionError(ValueError):
    """Poppler could not provide an intact page-level text extraction."""


def _run(content: bytes, *options: str) -> bytes:
    deadline = time.monotonic() + 30
    with tempfile.TemporaryFile() as source:
        source.write(content)
        source.seek(0)
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed local binary and options
                ["pdftotext", *options, "-enc", "UTF-8", "-", "-"],
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise PDFExtractionError("PDF extraction unavailable: pdftotext is not installed") from exc
        try:
            assert process.stdout is not None
            chunks: list[bytes] = []
            size = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    raise PDFExtractionError("PDF extraction timed out")
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_OUTPUT_BYTES:
                    raise PDFExtractionError("PDF extracted output exceeds parser limit")
                chunks.append(chunk)
            try:
                returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise PDFExtractionError("PDF extraction timed out") from exc
            if returncode != 0:
                raise PDFExtractionError("PDF parser rejected source bytes")
            return b"".join(chunks)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()


def _pages(output: bytes) -> list[str]:
    pages = output.decode("utf-8", errors="replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def _bbox_pages(output: bytes) -> list[tuple[float, float, list[_Box]]]:
    if len(output) > _MAX_OUTPUT_BYTES:
        raise PDFExtractionError("PDF geometry output exceeds parser limit")
    try:
        root = ET.fromstring(output)
        pages: list[tuple[float, float, list[_Box]]] = []
        block_count = word_count = 0
        for page in root.iter(f"{_XHTML}page"):
            if len(pages) >= _MAX_PAGES:
                raise PDFExtractionError("PDF page count exceeds parser limit")
            width = float(page.attrib["width"])
            height = float(page.attrib["height"])
            if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                raise PDFExtractionError("PDF page geometry is invalid")
            boxes = []
            for block in page.iter(f"{_XHTML}block"):
                block_count += 1
                if block_count > _MAX_BLOCKS:
                    raise PDFExtractionError("PDF block count exceeds parser limit")
                words = [word.text or "" for word in block.iter(f"{_XHTML}word")]
                word_count += len(words)
                if word_count > _MAX_WORDS:
                    raise PDFExtractionError("PDF word count exceeds parser limit")
                if words:
                    lines = [
                        " ".join(word.text or "" for word in line.iter(f"{_XHTML}word"))
                        for line in block.iter(f"{_XHTML}line")
                    ]
                    x0, y0, x1, y1 = (
                        float(block.attrib[key]) for key in ("xMin", "yMin", "xMax", "yMax")
                    )
                    if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
                        raise PDFExtractionError("PDF block geometry is invalid")
                    boxes.append(_Box(x0, y0, x1, y1, len(" ".join(words)), "\n".join(lines)))
            pages.append((width, height, boxes))
        return pages
    except PDFExtractionError:
        raise
    except (ET.ParseError, KeyError, ValueError, OverflowError) as exc:
        raise PDFExtractionError("PDF geometry output is invalid") from exc


def _table_like(boxes: list[_Box], width: float) -> bool:
    """Two aligned rows of three or more separated cells warrant layout text."""
    row_centers: list[list[float]] = []
    for box in boxes:
        if box.x1 - box.x0 > 0.45 * width:
            continue
        center_y = (box.y0 + box.y1) / 2
        row = next((row for row in row_centers if abs(row[0] - center_y) <= 4), None)
        if row is None:
            row = [center_y]
            row_centers.append(row)
        row.append((box.x0 + box.x1) / 2)
    aligned = 0
    for row in row_centers:
        centers = sorted(row[1:])
        distinct: list[float] = []
        for center in centers:
            if not distinct or center - distinct[-1] >= 10:
                distinct.append(center)
        # Require three genuinely separated placements across a useful width.
        if len(distinct) >= 3 and distinct[-1] - distinct[0] >= 0.30 * width:
            aligned += 1
    return aligned >= 2


def _column_split(boxes: list[_Box], width: float) -> float | None:
    pairs: list[tuple[float, float]] = []
    for index, left in enumerate(boxes):
        if left.chars < 25 or left.x1 - left.x0 > 0.72 * width:
            continue
        for other in boxes[index + 1:]:
            if other.chars < 25 or other.x1 - other.x0 > 0.72 * width:
                continue
            left_box, right_box = sorted((left, other), key=lambda box: box.x0)
            if right_box.x0 - left_box.x1 < 8:
                continue
            overlap = min(left_box.y1, right_box.y1) - max(left_box.y0, right_box.y0)
            if overlap >= 5:
                if min(left.chars, other.chars) >= 100:
                    return (left_box.x1 + right_box.x0) / 2
                pairs.append(((left_box.x1 + right_box.x0) / 2, (left.y0 + other.y0) / 2))
                if len(pairs) > _MAX_COLUMN_PAIRS:
                    return None
    for index, (split, y) in enumerate(pairs):
        if any(
            abs(split - other_split) <= 30 and abs(y - other_y) >= 5
            for other_split, other_y in pairs[index + 1:]
        ):
            return split
    return None


def _ordered_page(boxes: list[_Box], split: float) -> str | None:
    left = [box for box in boxes if box.x1 < split]
    right = [box for box in boxes if box.x0 > split]
    wide = [box for box in boxes if box not in left and box not in right]
    if not left or not right:
        return None
    substantive = [box for box in [*left, *right] if box.chars >= 20]
    column_start = min(box.y0 for box in substantive)
    column_end = max(box.y1 for box in substantive)
    prefix, suffix = [], []
    for box in wide:
        if box.y1 <= column_start + 5:
            prefix.append(box)
        elif box.y0 >= column_end - 5:
            suffix.append(box)
        else:
            return None
    for column, other in ((left, right), (right, left)):
        ordered = sorted(column, key=lambda box: box.y0)
        other_end = max(box.y1 for box in other)
        for index in range(1, len(ordered)):
            if (
                ordered[index].y0 - ordered[index - 1].y1 >= 30
                and ordered[index].y0 > other_end + 20
            ):
                trailing = ordered[index:]
                suffix.extend(trailing)
                column[:] = [box for box in column if box not in trailing]
                break
    if not left or not right:
        return None
    left_chars = sum(box.chars for box in left)
    right_chars = sum(box.chars for box in right)
    left_width = max(box.x1 for box in left) - min(box.x0 for box in left)
    right_width = max(box.x1 for box in right) - min(box.x0 for box in right)
    main_right = right_chars >= 1.3 * left_chars and right_width >= 1.5 * left_width
    columns = (right, left) if main_right else (left, right)
    groups = [prefix, *columns, suffix]
    return "\n\n".join(
        box.text for group in groups
        for box in sorted(group, key=lambda item: (item.y0, item.x0))
    ) + "\n"


def _same_visible_characters(layout: str, reading_order: str) -> bool:
    """Only whitespace may change; signs, decimals and unit glyphs must survive."""
    return Counter(char for char in layout if not char.isspace()) == Counter(
        char for char in reading_order if not char.isspace()
    )


def extract_pdf_blocks(content: bytes) -> list[PDFBlock]:
    """Extract selectable text with one stable block per nonempty physical page."""
    if len(content) > _MAX_INPUT_BYTES:
        raise PDFExtractionError("PDF source bytes exceed parser limit")
    layout_pages = _pages(_run(content, "-layout"))
    bbox_pages = _bbox_pages(_run(content, "-bbox-layout"))
    if len(bbox_pages) != len(layout_pages):
        raise PDFExtractionError("PDF page geometry and layout text disagree")
    blocks = []
    for page_number, (layout, (width, _height, boxes)) in enumerate(
        zip(layout_pages, bbox_pages, strict=True), 1
    ):
        split = (
            _column_split(boxes, width)
            if len(boxes) <= _MAX_REORDER_BOXES and not _table_like(boxes, width)
            else None
        )
        reading = _ordered_page(boxes, split) if split is not None else None
        use_reading = bool(reading) and _same_visible_characters(layout, reading)
        text = reading if use_reading else layout
        if text.strip():
            mode = "reading-order-text" if use_reading else "layout-text"
            blocks.append(PDFBlock(text, f"page={page_number}; {mode}"))
    return blocks
