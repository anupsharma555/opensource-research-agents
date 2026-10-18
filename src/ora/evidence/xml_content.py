"""Bounded structural extraction for complete JATS and ordinary XML copies.

This module does not validate a JATS dialect, render MathML, or infer scientific
claims. It keeps section scope and source-authored table associations visible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .html_content import _cell_text, _cells, _Node, _rows, _span, _text

XML_PARSER_VERSION = "stdlib-elementtree-structured-v2"
_SPACE = re.compile(r"\s+")
_BLOCK_INLINE = frozenset({"p", "list", "list-item", "def-list", "def-item"})
_MAX_DEPTH = 200
_MAX_ELEMENTS = 100_000


@dataclass(frozen=True)
class XMLBlock:
    text: str
    locator: str


class XMLExtractionError(ValueError):
    """Complete XML is invalid or exceeds the structural reader's bounds."""


class XMLStructureLimitError(XMLExtractionError):
    """A complete tree exceeded a hard parser bound and cannot be recovered."""


def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1].lower()


def _clean(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def _raw_text(node: ET.Element) -> str:
    parts = [node.text or ""]
    for child in node:
        tag = _tag(child)
        child_text = _raw_text(child)
        if tag == "sup":
            contents = _clean(child_text)
            parts.append("^" + (contents if len(contents) == 1 else "{" + contents + "}"))
        elif tag == "sub":
            contents = _clean(child_text)
            parts.append("_" + (contents if len(contents) == 1 else "{" + contents + "}"))
        elif tag in {"break", "br"}:
            parts.append(" ")
        else:
            if tag in _BLOCK_INLINE:
                parts.append(" ")
            parts.append(child_text)
            if tag in _BLOCK_INLINE:
                parts.append(" ")
        parts.append(child.tail or "")
    return "".join(parts)


def _html_node(element: ET.Element, parent: _Node | None = None) -> _Node:
    """Represent a JATS XHTML-like table for the frozen cell/header formatter."""
    node = _Node("br" if _tag(element) == "break" else _tag(element), dict(element.attrib), parent=parent)
    if element.text:
        node.children.append(element.text)
    for child in element:
        tag = _tag(child)
        if tag in {"sup", "sub"}:
            contents = _clean(_raw_text(child))
            marker = "^" if tag == "sup" else "_"
            node.children.append(marker + (
                contents if len(contents) == 1 else "{" + contents + "}"
            ))
        else:
            node.children.append(_html_node(child, node))
        if child.tail:
            node.children.append(child.tail)
    return node


def _href(node: ET.Element) -> str | None:
    return next(
        (value for key, value in node.attrib.items() if key.rsplit("}", 1)[-1] == "href"),
        None,
    )


def _structured_table_blocks(table: _Node) -> list[str]:
    """Keep each XML cell with every covered header, including merged columns."""
    caption = next(
        (
            _text(child) for child in table.children
            if isinstance(child, _Node) and child.tag == "caption" and _text(child)
        ),
        "",
    )
    rows = _rows(table)
    grid: list[list[tuple[_Node, str] | None]] = []
    occupied: dict[int, tuple[int, _Node, str]] = {}
    for row in rows:
        line: list[tuple[_Node, str] | None] = []
        pending, occupied = occupied, {}
        column = 0
        for cell in _cells(row):
            while column in pending:
                remaining, old, value = pending.pop(column)
                while len(line) <= column:
                    line.append(None)
                line[column] = (old, value)
                if remaining > 1:
                    occupied[column] = (remaining - 1, old, value)
                column += 1
            value = _cell_text(cell)
            width, height = _span(cell, "colspan"), _span(cell, "rowspan")
            for offset in range(width):
                position = column + offset
                while len(line) <= position:
                    line.append(None)
                line[position] = (cell, value)
                if height > 1:
                    occupied[position] = (height - 1, cell, value)
            column += width
        for position, (remaining, old, value) in pending.items():
            while len(line) <= position:
                line.append(None)
            line[position] = (old, value)
            if remaining > 1:
                occupied[position] = (remaining - 1, old, value)
        grid.append(line)

    first_body = 0
    for row in rows:
        cells = _cells(row)
        if cells and (
            (row.parent and row.parent.tag == "thead")
            or all(cell.tag == "th" or (cell.tag == "td" and not _cell_text(cell)) for cell in cells)
        ):
            first_body += 1
        else:
            break
    headers: dict[int, list[str]] = {}
    narrow_headers: dict[int, list[str]] = {}
    for line in grid[:first_body]:
        for index, item in enumerate(line):
            if item and item[1] and item[1] != caption:
                headers.setdefault(index, [])
                if item[1] not in headers[index]:
                    headers[index].append(item[1])
                if _span(item[0], "colspan") == 1:
                    narrow_headers.setdefault(index, []).append(item[1])
    named_headers = {
        cell.attrs["id"]: _cell_text(cell)
        for row in rows for cell in _cells(row)
        if cell.tag == "th" and cell.attrs.get("id") and _cell_text(cell)
    }
    output: list[str] = []
    for row, line in zip(rows[first_body:], grid[first_body:], strict=True):
        if row.parent and row.parent.tag == "tfoot":
            note = " | ".join(dict.fromkeys(item[1] for item in line if item and item[1]))
            if note:
                output.append(" | ".join((f"Table: {caption}" if caption else "Table", note)))
            continue
        row_labels = []
        for index, item in enumerate(line):
            if not item or item[0].tag != "th" or not item[1]:
                continue
            if item[0].attrs.get("scope", "row") in {"col", "colgroup"}:
                continue
            header = narrow_headers.get(index, [])
            row_labels.append(f"{header[-1]}: {item[1]}" if header else item[1])
        row_labels = list(dict.fromkeys(row_labels))
        first = line[0] if line else None
        first_is_label = (
            not row_labels and first is not None and len(line) > 1
            and first[0].tag == "td" and bool(first[1])
            and bool(narrow_headers.get(0)) and not first[0].attrs.get("headers")
            and _span(first[0], "colspan") == 1
        )
        if first_is_label:
            row_labels = [f"{narrow_headers[0][-1]}: {first[1]}"]
        prefix = [f"Table: {caption}" if caption else "Table"]
        if row_labels:
            prefix.append("Row: " + " / ".join(row_labels))
        seen: set[int] = set()
        for index, item in enumerate(line):
            if not item or item[0].tag == "th" or not item[1] or id(item[0]) in seen:
                continue
            if first_is_label and item[0] is first[0]:
                continue
            seen.add(id(item[0]))
            explicit = item[0].attrs.get("headers", "").split()
            if explicit:
                names = [named_headers[ref] for ref in explicit if ref in named_headers]
            else:
                covered = [
                    column for column, covered_item in enumerate(line)
                    if covered_item and covered_item[0] is item[0]
                ]
                names = [
                    name for column in covered for name in headers.get(column, [])
                    if name != caption and name not in row_labels
                ]
            label = " / ".join(dict.fromkeys(names)) or f"Column {index + 1}"
            output.append(" | ".join([*prefix, f"{label}: {item[1]}"]))
    if not output and caption:
        output.append("Table: " + caption)
    return output


def extract_xml_blocks(content: bytes) -> list[XMLBlock]:
    """Extract a complete XML copy; malformed copies use the legacy partial path."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise XMLExtractionError("XML parser rejected source bytes") from exc
    pending = [(root, 1)]
    element_count = 0
    while pending:
        current, depth = pending.pop()
        element_count += 1
        if depth > _MAX_DEPTH or element_count > _MAX_ELEMENTS:
            raise XMLStructureLimitError("XML structure exceeds parser limit")
        pending.extend((child, depth + 1) for child in current)
    blocks: list[XMLBlock] = []
    ordinal = 0
    table_ordinal = 0

    def emit(value: str, scope: tuple[str, ...], kind: str = "p") -> None:
        nonlocal ordinal
        value = _clean(value)
        if not value:
            return
        ordinal += 1
        section = " / ".join(scope) if scope else "document"
        suffix = f"{kind}; block={ordinal}" if "=" in kind or ";" in kind else f"{kind}={ordinal}"
        blocks.append(XMLBlock(value, f"section={section}; {suffix}"))

    def walk(node: ET.Element, scope: tuple[str, ...]) -> None:
        nonlocal table_ordinal
        tag = _tag(node)
        if tag == "abstract":
            descendants = (*scope, "Abstract")
            if not any(
                _tag(child) in {"p", "sec"} for child in node.iter() if child is not node
            ):
                emit(_raw_text(node), descendants, "abstract")
                return
            for child in node:
                walk(child, descendants)
            return
        if tag == "sec":
            title = next((child for child in node if _tag(child) == "title"), None)
            label = _clean(_raw_text(title)) if title is not None else "Untitled section"
            descendants = (*scope, label)
            for child in node:
                if child is not title:
                    walk(child, descendants)
            return
        if tag == "table-wrap":
            table_ordinal += 1
            caption = next((child for child in node if _tag(child) == "caption"), None)
            label = next((child for child in node if _tag(child) == "label"), None)
            caption_parts = [
                _clean(_raw_text(part)) for part in (label, caption) if part is not None
            ]
            caption_text = ". ".join(part for part in caption_parts if part)
            tables = [
                descendant for child in node
                for descendant in (list(child) if _tag(child) == "alternatives" else [child])
                if _tag(descendant) == "table"
            ]
            for table in tables:
                table_node = _html_node(table)
                if caption_text:
                    caption_node = _Node("caption", parent=table_node)
                    caption_node.children.append(caption_text)
                    table_node.children.insert(0, caption_node)
                for index, value in enumerate(_structured_table_blocks(table_node), 1):
                    emit(value, scope, f"table={table_ordinal}; cell={index}")
            if not tables and caption_text:
                emit("Table: " + caption_text, scope, f"table={table_ordinal}")
            for child in node:
                if _tag(child) == "alternatives":
                    for alternative in child:
                        if _tag(alternative) == "graphic" and (href := _href(alternative)):
                            emit(
                                "Table graphic reference: " + href,
                                scope, f"table={table_ordinal}; graphic",
                            )
            for child in node:
                if _tag(child) == "table-wrap-foot":
                    note = _clean(_raw_text(child))
                    if note:
                        prefix = f"Table: {caption_text} | " if caption_text else "Table | "
                        emit(prefix + "Note: " + note, scope, f"table={table_ordinal}; note")
            return
        if tag == "fig":
            label = next((child for child in node if _tag(child) == "label"), None)
            caption = next((child for child in node if _tag(child) == "caption"), None)
            caption_text = ". ".join(
                part for part in (
                    _clean(_raw_text(value)) for value in (label, caption) if value is not None
                ) if part
            )
            if caption_text:
                emit("Figure: " + caption_text, scope, "figure")
            for child in node:
                if _tag(child) == "graphic" and (href := _href(child)):
                    emit("Figure graphic reference: " + href, scope, "figure-graphic")
            return
        if tag in {"p", "fn", "preformat"}:
            emit(_raw_text(node), scope, tag)
            return
        if tag in {"mixed-citation", "element-citation"}:
            emit(_raw_text(node), scope, "reference")
            return
        if tag == "table":
            table_ordinal += 1
            for index, value in enumerate(_structured_table_blocks(_html_node(node)), 1):
                emit(value, scope, f"table={table_ordinal}; cell={index}")
            return
        for child in node:
            walk(child, scope)

    if _tag(root) == "article":
        front = next((child for child in root if _tag(child) == "front"), None)
        metadata_root = front if front is not None else root
        for node in metadata_root.iter():
            if _tag(node) == "article-id" and node.attrib.get("pub-id-type") == "doi":
                emit("DOI: " + _raw_text(node), ("Article metadata",), "article-id=doi")
                break
        title = next((node for node in metadata_root.iter() if _tag(node) == "article-title"), None)
        if title is not None:
            emit(_raw_text(title), ("Article metadata",), "article-title")
        if front is not None:
            for node in front.iter():
                if _tag(node) == "abstract":
                    walk(node, ())
        for child in root:
            if _tag(child) == "body":
                for item in child:
                    walk(item, ())
            elif _tag(child) == "back":
                for item in child:
                    walk(item, ("Back matter",))
            elif _tag(child) not in {"front", "article-id", "article-title"}:
                walk(child, ())
    else:
        walk(root, ())
    return blocks
