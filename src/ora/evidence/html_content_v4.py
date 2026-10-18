"""Structure-aware HTML reading with explicit grouped and whitespace semantics.

Earlier HTML readers remain frozen for replay. This version changes only fresh
extraction of description lists, disclosures, and inline whitespace directives.
"""

from __future__ import annotations

import re

from .html_content import (
    _STRUCTURAL,
    HTMLBlock,
    _content_roots,
    _fragment_jump,
    _Node,
    _nodes,
    _table_blocks,
    _Tree,
    _visible,
)

HTML_PARSER_VERSION = "stdlib-htmlparser-structured-v4"
_SPACE = re.compile(r"\s+")
_CONTROL_CLASS = re.compile(
    r"(?:^|[-_])(toc|tab|tabs|menu|dropdown|share|feedback|toolbar|portlet)(?:$|[-_])"
)
_V4_STRUCTURAL = _STRUCTURAL | {"details", "summary"}


def _clean(value: str) -> str:
    return _SPACE.sub(" ", value).strip()


def _raw_text(node: _Node, *, block_spacing: bool = False) -> str:
    """Join inline fragments before trimming so `Evapo- </em>for` keeps its space."""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif _visible(child):
            if child.tag == "br":
                parts.append("\n")
            else:
                structural = block_spacing and child.tag in _STRUCTURAL
                if structural:
                    parts.append(" ")
                parts.append(_raw_text(child, block_spacing=block_spacing))
                if structural:
                    parts.append(" ")
    return "".join(parts)


def _control(node: _Node) -> bool:
    """Exclude interactive navigation clusters, not prose mentioning controls."""
    if not _visible(node):
        return True
    ident = node.attrs.get("id", "")
    if re.match(r"^(?:loading|skeleton)[A-Z_-]", ident) and len(_raw_text(node)) < 100:
        return True
    classes = node.attrs.get("class", "").lower().split()
    semantic = any(_CONTROL_CLASS.search(token) for token in classes)
    if node.attrs.get("role", "").lower() in {"tablist", "toolbar", "menu"}:
        return True
    if node.tag == "ul" and not semantic:
        # In-page references in substantive list items remain content. A
        # link-dominated list is navigation.
        links = [part for part in _nodes(node) if part.tag == "a" and _visible(part)]
        items = [part for part in _nodes(node) if part.tag == "li" and _visible(part)]
        buttons = [part for part in _nodes(node) if part.tag == "button"]
        if len(items) >= 2 and len(buttons) >= 4 and len(buttons) >= len(items):
            return True
        if len(links) >= 2 and len(items) >= 2 and all(
            link.attrs.get("href", "").startswith("#") for link in links
        ) and not any(part.tag == "p" for part in _nodes(node)) and _link_ratio(node, links) >= 0.8:
            return True
    if node.tag in {"div", "aside", "section"} and not semantic:
        descendants = [part for part in _nodes(node) if part is not node and _visible(part)]
        links = [part for part in descendants if part.tag == "a"]
        items = [part for part in descendants if part.tag == "li"]
        prose = [part for part in descendants if part.tag in {"p", "pre", "table"}]
        headings = [part for part in descendants if part.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}]
        lists = [part for part in descendants if part.tag in {"ul", "ol"}]
        if (
            len(links) >= 2 and len(items) >= 2 and len(headings) <= 1
            and not prose and all(link.attrs.get("href", "").startswith("#") for link in links)
            and (
                _link_ratio(node, links) >= 0.8
                or (
                    len(headings) == 1 and len(lists) == 1
                    and _link_ratio(lists[0], links) >= 0.8
                )
            )
        ):
            return True
    if not semantic:
        return False
    descendants = [part for part in _nodes(node) if part is not node and _visible(part)]
    links = [part for part in descendants if part.tag == "a"]
    buttons = [part for part in descendants if part.tag in {"button", "input", "select"}]
    prose = [part for part in descendants if part.tag in {"p", "pre", "table"}]
    if len(links) >= 3 and not prose and _link_ratio(node, links) >= 0.8:
        return True
    if len(buttons) >= 2 and not prose:
        return True
    return False


def _link_ratio(node: _Node, links: list[_Node]) -> float:
    total = len(_clean(_raw_text(node)))
    return sum(len(_clean(_raw_text(link))) for link in links) / total if total else 0.0


def _pre_text(node: _Node) -> str:
    value = _raw_text(node).replace("\r\n", "\n").replace("\r", "\n")
    # HTML ignores exactly one LF immediately after the opening <pre> tag.
    # Other leading/trailing newlines and indentation belong to the source.
    first = node.children[0] if node.children else None
    if isinstance(first, str) and first.replace("\r\n", "\n").replace("\r", "\n").startswith("\n"):
        value = value[1:]
    return value


def _inline_white_space(node: _Node) -> bool | None:
    """Read only this element's inline declaration, never computed stylesheet CSS."""
    selected: tuple[bool, bool] | None = None
    for declaration in node.attrs.get("style", "").split(";"):
        property_name, separator, value = declaration.partition(":")
        if separator and property_name.strip().lower() == "white-space":
            important = bool(re.search(r"!important\s*$", value, flags=re.I))
            value = re.sub(r"\s*!important\s*$", "", value, flags=re.I).strip().lower()
            if value in {"pre", "pre-wrap", "break-spaces"}:
                preserve = True
            elif value in {"normal", "nowrap", "pre-line", "initial"}:
                preserve = False
            else:
                # Invalid or stylesheet-dependent values cannot override a
                # valid inline declaration in this bounded reader.
                continue
            if selected is None or important or not selected[0]:
                selected = (important, preserve)
    return selected[1] if selected is not None else None


def _dl_children(node: _Node):
    """Flatten only div wrappers allowed around description-list groups."""
    for child in node.children:
        if isinstance(child, _Node) and _visible(child):
            if child.tag == "div":
                yield from _dl_children(child)
            else:
                yield child


def extract_html_blocks(decoded: str) -> list[HTMLBlock]:
    """Return v4 blocks with stable, copy-local locators."""
    parser = _Tree()
    parser.feed(decoded)
    parser.close()
    roots, fallback = _content_roots(parser.root)
    blocks: list[HTMLBlock] = []
    section = "document"

    def emit(value: str, *, heading: bool = False, preformatted: bool = False) -> None:
        nonlocal section
        value = (
            value.replace("\r\n", "\n").replace("\r", "\n")
            if preformatted else _clean(value)
        )
        if not value.strip():
            return
        if heading:
            section = value[:120]
        blocks.append(HTMLBlock(value, f"section={section}; block={len(blocks) + 1}"))

    def walk(
        node: _Node, list_path: tuple[str, ...] = (), inherited_preformatted: bool = False,
    ) -> None:
        if _control(node):
            return
        declared_white_space = _inline_white_space(node)
        preformatted = (
            inherited_preformatted if declared_white_space is None else declared_white_space
        )
        if fallback and node.tag in {"header", "aside", "footer"}:
            return
        if node.tag == "table":
            for value in _table_blocks(node):
                emit(value)
            for nested in _nodes(node):
                if nested is not node and nested.tag == "table" and _nearest_table(nested) is node:
                    walk(nested, inherited_preformatted=preformatted)
            return
        if node.tag == "dl":
            terms: list[str] = []
            definitions: list[str] = []

            def flush_group() -> None:
                if terms and definitions:
                    emit(
                        "Term: " + " / ".join(terms)
                        + " | Definition: " + " | ".join(definitions),
                        preformatted=preformatted,
                    )
                elif terms:
                    emit(" / ".join(terms), preformatted=preformatted)
                elif definitions:
                    emit(" | ".join(definitions), preformatted=preformatted)
                terms.clear()
                definitions.clear()

            for child in _dl_children(node):
                if child.tag == "dt":
                    if definitions:
                        flush_group()
                    value = _raw_text(child, block_spacing=True)
                    if value.strip():
                        terms.append(value if preformatted else _clean(value))
                elif child.tag == "dd":
                    value = _raw_text(child, block_spacing=True)
                    if value.strip():
                        definitions.append(value if preformatted else _clean(value))
                else:
                    flush_group()
                    walk(child, inherited_preformatted=preformatted)
            flush_group()
            return
        if node.tag == "details":
            # Source extraction includes authored disclosure body even when
            # initially collapsed; the summary remains a separate label.
            for child in node.children:
                if isinstance(child, str):
                    emit(child, preformatted=preformatted)
                elif child.tag == "summary":
                    emit(_raw_text(child), preformatted=preformatted)
                else:
                    walk(child, inherited_preformatted=preformatted)
            return
        if node.tag == "li":
            direct = "".join(
                child if isinstance(child, str) else _raw_text(child)
                for child in node.children
                if isinstance(child, str) or (
                    child.tag not in {"ul", "ol"} and _visible(child)
                )
            )
            if not preformatted:
                direct = _clean(direct)
            path = (*list_path, direct) if direct else list_path
            if direct:
                emit(" > ".join(path), preformatted=preformatted)
            for child in node.children:
                if isinstance(child, _Node) and child.tag in {"ul", "ol"}:
                    walk(child, path, preformatted)
            return
        if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            emit(_raw_text(node), heading=True, preformatted=preformatted)
            return
        if node.tag == "pre":
            emit(_pre_text(node), preformatted=True)
            return
        if node.tag == "summary":
            emit(_raw_text(node), preformatted=preformatted)
            return
        if node.tag in {"p", "blockquote", "dt", "dd"}:
            if not _fragment_jump(node):
                emit(_raw_text(node), preformatted=preformatted)
            return
        if node.tag in {"ul", "ol"}:
            for child in node.children:
                if isinstance(child, _Node):
                    walk(child, list_path, preformatted)
            return
        inline: list[str] = []
        for child in node.children:
            if isinstance(child, str):
                inline.append(child)
            elif child.tag in _V4_STRUCTURAL:
                emit("".join(inline), preformatted=preformatted)
                inline.clear()
                walk(child, list_path, preformatted)
            elif _visible(child):
                inline.append(_raw_text(child))
        emit("".join(inline), preformatted=preformatted)

    for root in roots:
        walk(root)
    return blocks


def _nearest_table(node: _Node) -> _Node | None:
    parent = node.parent
    while parent is not None and parent.tag != "table":
        parent = parent.parent
    return parent
