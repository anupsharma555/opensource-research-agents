"""Extract source-authored HTML content with its local structure intact.

This is deliberately a reader, not a browser: it does not execute scripts, infer
missing scientific meaning, or follow links. Locators describe extracted blocks
within this exact copy and are stable for the same input bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

HTML_PARSER_VERSION = "stdlib-htmlparser-structured-v2"
_MAX_TREE_DEPTH = 200
_MAX_TREE_NODES = 100_000


class HTMLExtractionError(ValueError):
    """The HTML copy exceeds this bounded reader's structural limits."""


@dataclass(frozen=True)
class HTMLBlock:
    text: str
    locator: str


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)
    parent: _Node | None = None


_VOID = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)
_AUTO_CLOSE = {
    "li": {"li"},
    "p": {"p"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "tr": {"tr", "td", "th"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "option": {"option"},
}
_CHROME_IDS = frozenset(
    "global_nav left_nav main_nav1 main_nav2 topnav site_search main_menu".split()
)
_SKIP_TAGS = frozenset("head script style template noscript svg canvas nav form button select textarea".split())
_HEADING_TAGS = frozenset("h1 h2 h3 h4 h5 h6".split())
_STRUCTURAL = frozenset(
    "article aside blockquote dd div dl dt footer h1 h2 h3 h4 h5 h6 header html li main ol p pre section table ul".split()
)
_SPACE = re.compile(r"\s+")


def _clean(value: str) -> str:
    return _SPACE.sub(" ", value).strip()


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.node_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _AUTO_CLOSE:
            closable = _AUTO_CLOSE[tag]
            for index in range(len(self.stack) - 1, 0, -1):
                if self.stack[index].tag in closable:
                    del self.stack[index:]
                    break
                if self.stack[index].tag in {"table", "ul", "ol", "body"}:
                    break
        if len(self.stack) > _MAX_TREE_DEPTH:
            raise HTMLExtractionError("HTML nesting exceeds parser limit")
        if self.node_count >= _MAX_TREE_NODES:
            raise HTMLExtractionError("HTML element count exceeds parser limit")
        self.node_count += 1
        node = _Node(tag, {key: value or "" for key, value in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def _hidden(node: _Node) -> bool:
    attrs = node.attrs
    ident = attrs.get("id", "").lower()
    classes = attrs.get("class", "").lower().split()
    role = attrs.get("role", "").lower()
    # Some legacy pages put the real article inside a container named
    # ``main_menu``. Its separate navigation children remain filtered below.
    legacy_content_wrapper = ident == "main_menu" and any(
        part.attrs.get("id", "").lower() == "main_body" for part in _nodes(node)
    )
    return (
        node.tag in _SKIP_TAGS
        or (ident in _CHROME_IDS and not legacy_content_wrapper)
        or ident.startswith("left_nav_")
        or any(part in _CHROME_IDS for part in classes)
        or "locshare-this" in classes
        or role in {"navigation", "search", "menu", "menubar", "contentinfo"}
        or "hidden" in attrs
        or attrs.get("aria-hidden", "").lower() == "true"
        or bool(re.search(r"(?:^|;)\s*display\s*:\s*none(?:;|$)", attrs.get("style", ""), re.I))
    )


def _nodes(node: _Node):
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _nodes(child)


def _visible(node: _Node) -> bool:
    current: _Node | None = node
    while current is not None:
        if _hidden(current):
            return False
        current = current.parent
    return True


def _text(node: _Node, *, omit_nested_tables: bool = False) -> str:
    if not _visible(node):
        return ""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif _visible(child) and not (omit_nested_tables and child.tag == "table"):
            if child.tag in _STRUCTURAL:
                parts.append(" ")
            parts.append(_text(child, omit_nested_tables=omit_nested_tables))
            if child.tag in _STRUCTURAL:
                parts.append(" ")
    return _clean("".join(parts))


def _content_roots(root: _Node) -> tuple[list[_Node], bool]:
    """Choose all independent content regions at the strongest available level."""
    candidates: list[_Node] = []
    for node in _nodes(root):
        ident = node.attrs.get("id", "").lower()
        if not (
            node.tag in {"main", "article"}
            or node.attrs.get("role", "").lower() == "main"
            or ident in {"main_body", "main-content", "main_content", "article-body", "content"}
        ):
            continue
        if not _text(node):
            continue
        if not _visible(node):
            continue
        candidates.append(node)
    for node in candidates:
        if node.attrs.get("id", "").lower() != "content":
            continue
        descendant_ids = {part.attrs.get("id", "").lower() for part in _nodes(node)}
        if {"page_head", "main_body"} <= descendant_ids:
            return [node], False
    tiers = (
        lambda node: (
            node.tag in {"main", "article"}
            or node.attrs.get("role", "").lower() == "main"
        ),
        lambda node: node.attrs.get("id", "").lower() in {
            "main_body", "main-content", "main_content", "article-body"
        },
        lambda node: node.attrs.get("id", "").lower() == "content",
    )
    for matches in tiers:
        selected = [node for node in candidates if matches(node)]
        if selected:
            # A containing main/article already includes its descendant regions.
            selected_ids = {id(node) for node in selected}
            independent = []
            for node in selected:
                parent = node.parent
                while parent is not None and id(parent) not in selected_ids:
                    parent = parent.parent
                if parent is None:
                    independent.append(node)
            return independent, False
    return [next((node for node in _nodes(root) if node.tag == "body"), root)], True


def _cell_text(node: _Node) -> str:
    """Keep nested list order within a cell without merging a nested table."""
    pieces: list[str] = []

    def visit(current: _Node) -> None:
        for child in current.children:
            if isinstance(child, str):
                pieces.append(child)
            elif _visible(child) and child.tag != "table":
                if child.tag == "li" and pieces:
                    pieces.append("; ")
                visit(child)
                if child.tag in {"p", "br"}:
                    pieces.append("; ")

    visit(node)
    result = _clean("".join(pieces).strip(" ;"))
    return re.sub(r"(?:\s*;\s*){2,}", "; ", result).strip(" ;")


def _fragment_jump(node: _Node) -> bool:
    """Recognize a paragraph used only to jump to the document top."""
    if node.tag != "p" or any(
        isinstance(child, str) and child.strip() for child in node.children
    ):
        return False
    elements = [child for child in node.children if isinstance(child, _Node)]
    anchors = [child for child in elements if child.tag == "a"]
    return (
        len(anchors) == 1
        and all(child.tag in {"a", "img", "br"} for child in elements)
        and anchors[0].attrs.get("href", "").lower()
        in {"#top", "#page_top", "#skip_menu"}
    )


def _rows(table: _Node) -> list[_Node]:
    return [
        node for node in _nodes(table)
        if node.tag == "tr" and _nearest_table(node) is table and _visible(node)
    ]


def _nearest_table(node: _Node) -> _Node | None:
    parent = node.parent
    while parent is not None and parent.tag != "table":
        parent = parent.parent
    return parent


def _cells(row: _Node) -> list[_Node]:
    return [
        child for child in row.children
        if isinstance(child, _Node) and child.tag in {"td", "th"} and _visible(child)
    ]


def _span(cell: _Node, name: str) -> int:
    try:
        return max(1, min(100, int(cell.attrs.get(name, "1"))))
    except ValueError:
        return 1


def _table_blocks(table: _Node) -> list[str]:
    captions = [
        _text(child, omit_nested_tables=True)
        for child in table.children
        if isinstance(child, _Node) and child.tag == "caption" and not _hidden(child)
    ]
    caption = next((value for value in captions if value), "")
    grid: list[list[tuple[_Node, str] | None]] = []
    occupied: dict[int, tuple[int, _Node, str]] = {}
    for row in _rows(table):
        line: list[tuple[_Node, str] | None] = []
        pending = dict(occupied)
        occupied = {}
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

    header_rows: list[list[tuple[_Node, str] | None]] = []
    body_rows: list[list[tuple[_Node, str] | None]] = []
    table_rows = _rows(table)
    for row, line in zip(table_rows, grid, strict=True):
        cells = _cells(row)
        header_like = cells and all(
            cell.tag == "th" or (cell.tag == "td" and not _cell_text(cell))
            for cell in cells
        )
        if not body_rows and (row.parent and row.parent.tag == "thead" or header_like):
            header_rows.append(line)
        else:
            body_rows.append(line)
    column_headers: dict[int, list[str]] = {}
    narrow_headers: dict[int, list[str]] = {}

    def remember_headers(line: list[tuple[_Node, str] | None]) -> None:
        for index, item in enumerate(line):
            if not item or not item[1]:
                continue
            column_headers.setdefault(index, [])
            if item[1] not in column_headers[index]:
                column_headers[index].append(item[1])
            if _span(item[0], "colspan") == 1 and item[1] != caption:
                narrow_headers.setdefault(index, []).append(item[1])

    for line in header_rows:
        remember_headers(line)
    named_headers = {
        cell.attrs["id"]: _cell_text(cell)
        for row in table_rows for cell in _cells(row)
        if cell.tag == "th" and cell.attrs.get("id") and _cell_text(cell)
    }
    output: list[str] = []
    for row, line in zip(table_rows[len(header_rows):], body_rows, strict=True):
        cells = _cells(row)
        if cells and all(cell.tag == "th" for cell in cells):
            scopes = {cell.attrs.get("scope", "").lower() for cell in cells}
            if not scopes & {"row", "rowgroup"}:
                first = _cell_text(cells[0])
                repeated = first and first in narrow_headers.get(0, [])
                explicit_columns = bool(scopes & {"col", "colgroup"})
                column_headers.clear()
                narrow_headers.clear()
                if (len(cells) > 1 and (repeated or explicit_columns)) or (
                    row.parent and row.parent.tag == "thead"
                ):
                    remember_headers(line)
                continue
        if row.parent and row.parent.tag == "tfoot":
            note = " | ".join(dict.fromkeys(item[1] for item in line if item and item[1]))
            if note:
                output.append(" | ".join(filter(None, (f"Table: {caption}" if caption else "Table", note))))
            continue
        labels = []
        for index, item in enumerate(line):
            if not item or item[0].tag != "th" or not item[1]:
                continue
            if item[0].attrs.get("scope", "row") == "col":
                continue
            header = narrow_headers.get(index, [])
            labels.append(f"{header[-1]}: {item[1]}" if header else item[1])
        labels = list(dict.fromkeys(labels))
        first_cell = line[0] if line else None
        first_is_label = (
            not labels and len(line) > 1 and first_cell is not None
            and bool(first_cell[1]) and bool(narrow_headers.get(0))
            and not first_cell[0].attrs.get("headers")
        )
        if first_is_label:
            labels = [f"{narrow_headers[0][-1]}: {first_cell[1]}"]
        prefix = [f"Table: {caption}" if caption else "Table"]
        if labels:
            prefix.append("Row: " + " / ".join(labels))
        seen: set[int] = set()
        for index, item in enumerate(line):
            if (
                not item or item[0].tag == "th" or id(item[0]) in seen
                or not item[1] or (first_is_label and index == 0)
            ):
                continue
            seen.add(id(item[0]))
            explicit = item[0].attrs.get("headers", "").split()
            headers = (
                list(dict.fromkeys(named_headers[ref] for ref in explicit if ref in named_headers))
                if explicit else [
                    value for value in column_headers.get(index, [])
                    if value not in labels and value != caption
                ]
            )
            label = " / ".join(headers) if headers else f"Column {index + 1}"
            output.append(" | ".join([*prefix, f"{label}: {item[1]}"]))
    if not output and (caption or header_rows):
        title = [caption] if caption else []
        title.extend(dict.fromkeys(value for values in column_headers.values() for value in values))
        if title:
            output.append("Table: " + " | ".join(title))
    return output


def extract_html_blocks(decoded: str) -> list[HTMLBlock]:
    """Return coherent source text blocks, excluding explicit page controls."""
    parser = _Tree()
    parser.feed(decoded)
    parser.close()
    roots, fallback = _content_roots(parser.root)
    blocks: list[HTMLBlock] = []
    section = "document"

    def emit(value: str, *, heading: bool = False) -> None:
        nonlocal section
        value = _clean(value)
        if not value:
            return
        if heading:
            section = value[:120]
        blocks.append(HTMLBlock(value, f"section={section}; block={len(blocks) + 1}"))

    def walk(node: _Node, list_path: tuple[str, ...] = ()) -> None:
        if not _visible(node):
            return
        if fallback and node.tag in {"header", "aside", "footer"}:
            return
        if node.tag == "table":
            for value in _table_blocks(node):
                emit(value)
            # Nested tables have their own relationships and must not inherit
            # the outer table's row or column labels.
            for nested in _nodes(node):
                if nested is not node and nested.tag == "table" and _nearest_table(nested) is node:
                    walk(nested)
            return
        if node.tag == "li":
            direct = _clean("".join(
                child if isinstance(child, str) else _text(child)
                for child in node.children
                if isinstance(child, str) or (
                    child.tag not in {"ul", "ol"} and _visible(child)
                )
            ))
            if direct:
                path = (*list_path, direct)
                emit(" > ".join(path))
            else:
                path = list_path
            for child in node.children:
                if isinstance(child, _Node) and child.tag in {"ul", "ol"}:
                    walk(child, path)
            return
        if node.tag in _HEADING_TAGS:
            emit(_text(node), heading=True)
            return
        if node.tag in {"p", "pre", "blockquote", "dt", "dd"}:
            if not _fragment_jump(node):
                emit(_text(node))
            return
        if node.tag in {"ul", "ol"}:
            for child in node.children:
                if isinstance(child, _Node):
                    walk(child, list_path)
            return
        inline: list[str] = []
        for child in node.children:
            if isinstance(child, str):
                inline.append(child)
            elif child.tag in _STRUCTURAL:
                emit("".join(inline))
                inline.clear()
                walk(child, list_path)
            elif _visible(child):
                inline.append(_text(child))
        emit("".join(inline))

    for root in roots:
        walk(root)
    return blocks
