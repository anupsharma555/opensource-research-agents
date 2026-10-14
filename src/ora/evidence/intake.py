"""Bounded inspection of actual source bytes before they become evidence.

Callers supply a publication identity verified elsewhere. Retrieval-hit assertions
are provenance, not proof that the returned bytes are the right article.
"""

from __future__ import annotations

import hashlib
import html
import http.client
import ipaddress
import re
import socket
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
from xml.etree import ElementTree as ET

from .hit_use import hit_allows_reinspection
from .identity import normalize_identifier
from .library import EvidenceError, EvidenceLibrary

PARSER_VERSION = "ora-intake-v1"
EXACT_SUBMISSION_VALIDATION = "exact-user-submission"
EXACT_SUBMISSION_PARSER_VERSION = "ora-exact-user-submission-v1"
MAX_COPY_BYTES = 32 * 1024 * 1024
MAX_PASSAGES = 100
_DOI = re.compile(r"10\.\d{4,9}/[^\s<>\"']+", re.I)
_LOGIN = re.compile(r"(?:sign in|log in|captcha|verify you are human|access denied)", re.I)
_FULL = re.compile(r"\b(?:methods?|materials?|results?|discussion|references)\b", re.I)
_EXPLICIT_USE_PROHIBITION = re.compile(
    r"\b(?:no\s+(?:text\s+(?:and\s+)?data\s+mining|automated\s+analysis|"
    r"ai\s+model\s+input)|(?:text\s+(?:and\s+)?data\s+mining|"
    r"automated\s+analysis|ai\s+model\s+input)\s+(?:is\s+)?prohibited)\b",
    re.I,
)


class OversizedSourceError(EvidenceError):
    """A response exceeded a bounded direct-fetch limit without silent clipping."""

    def __init__(
        self,
        url: str,
        *,
        limit_bytes: int,
        advertised_bytes: int | None,
        observed_prefix: bytes | None,
    ) -> None:
        super().__init__("Source response exceeds bounded intake limit")
        self.limit_bytes = limit_bytes
        self.advertised_bytes = advertised_bytes
        self.observed_bytes = len(observed_prefix) if observed_prefix is not None else None
        self.observed_prefix_sha256 = (
            hashlib.sha256(observed_prefix).hexdigest() if observed_prefix is not None else None
        )
        self.request_url_sha256 = hashlib.sha256(url.encode("utf-8")).hexdigest()

    def receipt(self) -> dict[str, Any]:
        """A safe diagnostic, never a hash assertion about the complete body."""
        return {
            "condition": "oversized_response",
            "limit_bytes": self.limit_bytes,
            "advertised_bytes": self.advertised_bytes,
            "observed_bytes": self.observed_bytes,
            "observed_prefix_sha256": self.observed_prefix_sha256,
            "complete_body_sha256": None,
            "request_url_sha256": self.request_url_sha256,
            "next_routes": [
                "smaller_page",
                "selected_passage_or_table",
                "permitted_artifact_route",
            ],
        }


class PublicSourceHTTPError(EvidenceError):
    """An HTTP response was received, but the public source was not fetched."""

    def __init__(
        self,
        status_code: int,
        *,
        requested_url: str,
        final_url: str,
        redirect_chain: list[dict[str, Any]],
    ) -> None:
        super().__init__(f"Source fetch failed: HTTP {status_code}")
        self.status_code = status_code
        self.requested_url = requested_url
        self.final_url = final_url
        self.redirect_chain = list(redirect_chain)
        self.retrieved_at = utc_now()


@dataclass(frozen=True)
class Block:
    text: str
    locator: str


@dataclass(frozen=True)
class _PreparedModelCopy:
    copy_ref: dict[str, Any]
    copy: dict[str, Any]
    blocks_by_locator: dict[str, list[str]]
    source_use_ref: dict[str, Any]
    submitted_text: str | None


def _exact_submitted_text(store: Any, copy: dict, content: bytes) -> str | None:
    """Recognize the retained raw-text coordinates of an original pasted input."""
    body = copy["body"]
    if (body.get("mime_type") != "text/plain"
            or body.get("inspection_state") != "checked"
            or body.get("validation_record") != EXACT_SUBMISSION_VALIDATION
            or hashlib.sha256(content).hexdigest() != body.get("content_sha256")):
        return None
    encounter_ref = body.get("encounter")
    if not isinstance(encounter_ref, dict):
        return None
    encounter = store.get(**encounter_ref)
    if (encounter is None or encounter["kind"] != "source_encounter"
            or encounter["body"].get("route") != "user_supplied"
            or encounter["body"].get("status") != "accepted"
            or encounter["body"].get("publication") != body.get("publication")):
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_exact_submitted_passage(passage: dict, copy: dict, text: str | None) -> bool:
    """Match the original whole submission without rewriting its saved citation."""
    body = passage["body"]
    return (
        text is not None
        and passage["id"] in copy["body"].get("passage_ids", [])
        and body.get("copy_ref") == {key: copy[key] for key in ("kind", "id", "version")}
        and body.get("inspection_state") == "checked"
        and body.get("span_id") == "supplied-1"
        and body.get("locator") == f"submitted text; chars=0:{len(text)}"
        and type(body.get("start")) is int and body["start"] == 0
        and type(body.get("end")) is int and body["end"] == len(text)
        and body.get("text") == text
    )


class _HTMLBlocks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.stack: list[str] = []
        self.parts: list[str] = []
        self.section = "document"
        self.ordinal = 0
        self.in_block = False
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "nav", "footer"}:
            self.skip += 1
        self.stack.append(tag)
        if tag in {"p", "h1", "h2", "h3", "h4", "li", "tr", "caption", "abstract"}:
            self._flush()
            self.in_block = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "h1", "h2", "h3", "h4", "li", "tr", "caption", "abstract"}:
            self._flush(heading=tag.startswith("h"))
        if tag in {"script", "style", "nav", "footer"}:
            self.skip = max(0, self.skip - 1)
        if self.stack:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if not self.skip and self.in_block:
            self.parts.append(data)

    def _flush(self, *, heading: bool = False) -> None:
        text = " ".join(" ".join(self.parts).split())
        if text:
            self.ordinal += 1
            if heading:
                self.section = text[:120]
            self.blocks.append(Block(text, f"section={self.section}; block={self.ordinal}"))
        self.parts = []
        self.in_block = False


def _xml_blocks(content: bytes) -> list[Block]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise EvidenceError("XML parser rejected source bytes") from exc
    blocks: list[Block] = []
    section = "document"
    ordinal = 0
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag == "article-id" and node.attrib.get("pub-id-type") == "doi" and node.text:
            blocks.append(Block(f"DOI: {node.text.strip()}", "article-meta; article-id=doi"))
        if tag in {"title", "article-title"} and node is not root:
            candidate = " ".join(" ".join(node.itertext()).split())
            if candidate:
                section = candidate[:120]
        if tag in {"p", "abstract", "td", "th", "caption", "table-wrap-foot", "fn"}:
            # Container abstract text is only used when it has no paragraph children.
            if tag == "abstract" and any(
                child.tag.rsplit("}", 1)[-1] == "p" for child in node.iter()
            ):
                continue
            value = " ".join(" ".join(node.itertext()).split())
            if value:
                ordinal += 1
                blocks.append(Block(value, f"section={section}; {tag}={ordinal}"))
    return blocks


def _partial_xml_blocks(content: bytes) -> list[Block]:
    """Recover only XML elements that closed before a declared truncation."""
    parser = ET.XMLPullParser(events=("end",))
    try:
        parser.feed(content)
    except ET.ParseError:
        pass
    section = "document"
    ordinal = 0
    blocks: list[Block] = []
    try:
        events = parser.read_events()
        for _, node in events:
            tag = node.tag.rsplit("}", 1)[-1].lower()
            if tag in {"title", "article-title"}:
                value = " ".join(" ".join(node.itertext()).split())
                if value:
                    section = value[:120]
            if tag == "article-id" and node.attrib.get("pub-id-type") == "doi" and node.text:
                blocks.append(Block(f"DOI: {node.text.strip()}", "article-meta; article-id=doi"))
            if tag in {"p", "td", "th", "caption", "table-wrap-foot", "fn"}:
                value = " ".join(" ".join(node.itertext()).split())
                if value:
                    ordinal += 1
                    blocks.append(Block(value, f"section={section}; {tag}={ordinal}"))
    except ET.ParseError:
        pass
    return blocks


def _pdf_blocks(content: bytes) -> tuple[list[Block], str]:
    try:
        completed = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", "-", "-"],
            input=content,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise EvidenceError("PDF extraction unavailable: pdftotext is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise EvidenceError("PDF extraction timed out") from exc
    if completed.returncode != 0:
        raise EvidenceError("PDF parser rejected source bytes")
    decoded = completed.stdout.decode("utf-8", errors="replace")
    pages = decoded.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    blocks = [
        Block(page, f"page={index}; layout-text")
        for index, page in enumerate(pages, 1)
        if page.strip()
    ]
    return blocks, "poppler-pdftotext-layout"


def extract_blocks(
    content: bytes, content_type: str, *, allow_partial_xml: bool = False
) -> tuple[list[Block], str]:
    """Return uncorrected source text and locators; no scientific interpretation."""
    mime = content_type.split(";", 1)[0].lower().strip()
    if mime == "application/pdf":
        return _pdf_blocks(content)
    if mime in {"application/xml", "text/xml", "application/jats+xml"}:
        try:
            return _xml_blocks(content), "stdlib-elementtree"
        except EvidenceError:
            if not allow_partial_xml:
                raise
            recovered = _partial_xml_blocks(content)
            if recovered:
                return recovered, "stdlib-elementtree-partial"
            parser = _HTMLBlocks()
            parser.feed(content.decode("utf-8", errors="replace"))
            parser._flush()
            return parser.blocks, "stdlib-htmlparser-partial-xml"
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EvidenceError("Text bytes are not UTF-8") from exc
    if mime == "text/html":
        parser = _HTMLBlocks()
        parser.feed(decoded)
        parser._flush()
        return parser.blocks, "stdlib-htmlparser"
    if mime in {"text/plain", "application/abstract+text"}:
        return [Block(decoded, "text; chars=0:" + str(len(decoded)))], "utf8-text"
    raise EvidenceError("Unsupported content type")


def _document_structure(content: bytes, content_type: str, blocks: list[Block]) -> str | None:
    """Recognize source structure; headings inside an abstract are not article body."""
    mime = content_type.split(";", 1)[0].lower().strip()
    if mime in {"application/xml", "text/xml", "application/jats+xml"}:
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return None
        if root.tag.rsplit("}", 1)[-1].lower() != "article":
            return None
        body = next((node for node in root if node.tag.rsplit("}", 1)[-1].lower() == "body"), None)
        if body is not None:
            sections = sum(node.tag.rsplit("}", 1)[-1].lower() == "sec" for node in body.iter())
            body_text_length = sum(len(" ".join(node.itertext())) for node in body)
            if sections >= 2 and body_text_length >= 1000:
                return "jats_article_body"
            if any(node.tag.rsplit("}", 1)[-1].lower() in {"sec", "p"} for node in body.iter()):
                return "jats_partial_body"
        return (
            "jats_abstract_only"
            if any(node.tag.rsplit("}", 1)[-1].lower() == "abstract" for node in root.iter())
            else None
        )
    if mime != "text/html":
        return None
    abstract_at = next(
        (
            index
            for index, block in enumerate(blocks)
            if block.text.strip().casefold() == "abstract"
            and "section=abstract;" in block.locator.casefold()
        ),
        None,
    )
    if abstract_at is None:
        return None
    introduction_at = next(
        (
            index
            for index, block in enumerate(blocks[abstract_at + 1 :], abstract_at + 1)
            if block.text.strip().casefold() == "introduction"
            and "section=introduction;" in block.locator.casefold()
        ),
        None,
    )
    if introduction_at is not None:
        if sum(len(block.text) for block in blocks[introduction_at + 1 :]) >= 300:
            return "html_article_body"
        return "html_partial_body"
    if any(len(block.text) >= 40 for block in blocks[abstract_at + 1 :]):
        return "html_abstract_section"
    return None


def detected_content_type(content: bytes, reported_type: str) -> str:
    """Prefer unmistakable source signatures over a server's MIME label."""
    beginning = content[:256].removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    if beginning.startswith(b"%pdf-"):
        return "application/pdf"
    if beginning.startswith(b"<?xml") or re.match(rb"<(?:[a-z0-9_.-]+:)?article\b", beginning):
        return "application/xml"
    if beginning.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return "text/html"
    return reported_type.split(";", 1)[0].lower().strip()


def _public_fetch_use_policy(
    receipt: dict[str, Any],
    *,
    content: bytes,
    source_url: str,
    content_type: str,
    retrieved_at: str,
    classification: str,
    identity_basis: str | None,
    expected_doi: str | None,
    blocks: list[Block],
    caller_rights_status: str,
) -> dict[str, Any]:
    """Decide private use of these exact fetched bytes, never a copyright license."""

    def public_https(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = urlparse(value)
            host = parsed.hostname
            port = parsed.port
            if host:
                try:
                    if not ipaddress.ip_address(host).is_global:
                        return False
                except ValueError:
                    pass  # Domain DNS was checked and pinned by fetch_public_source.
            return bool(
                parsed.scheme == "https"
                and host
                and port in {None, 443}
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            return False

    requested = receipt.get("requested_url")
    final = receipt.get("final_url")
    chain = receipt.get("redirect_chain")
    chain_valid = isinstance(chain, list)
    if chain_valid:
        previous = requested
        for leg in chain:
            if (
                not isinstance(leg, dict)
                or leg.get("from_url") != previous
                or not public_https(leg.get("to_url"))
                or leg.get("status") not in {301, 302, 303, 307, 308}
            ):
                chain_valid = False
                break
            previous = leg["to_url"]
        chain_valid = chain_valid and previous == final
    digest = hashlib.sha256(content).hexdigest()
    matching = bool(
        receipt.get("status") == 200
        and receipt.get("access_basis") == "public_https_unauthenticated"
        and receipt.get("authenticated") is False
        and receipt.get("content") == content
        and receipt.get("content_sha256") == digest
        and receipt.get("byte_length") == len(content)
        and receipt.get("content_type") == content_type
        and receipt.get("retrieved_at") == retrieved_at
        and final == source_url
        and public_https(requested)
        and public_https(final)
        and chain_valid
    )
    visible_dois = _actual_dois("\n".join(block.text for block in blocks)[:8000])
    expected = normalize_identifier("doi", expected_doi) if expected_doi else None
    identity_checked = bool(
        (expected and expected in visible_dois)
        or (isinstance(identity_basis, str) and identity_basis.startswith("verified_source:"))
    )
    source_prohibition = any(
        _EXPLICIT_USE_PROHIBITION.search(block.text)
        and (
            any(
                label in block.locator.casefold()
                for label in ("copyright", "license", "terms", "rights")
            )
            or block.text.strip().casefold().startswith("no text and data mining")
        )
        for block in blocks
    )
    allowed_surface = classification in {"abstract", "passage", "full_text", "navigation_document", "web_document"}
    grant = bool(
        matching
        and identity_checked
        and allowed_surface
        and not source_prohibition
        and caller_rights_status == "unknown"
    )
    return {
        "schema": "ora.evidence.direct_private_use.v1",
        "decision": "private_research_use" if grant else "not_granted",
        "basis": "user_authorized_private_research_on_checked_public_copy",
        "public_fetch_verified": matching,
        "identity_check": "visible_doi_match"
        if expected and expected in visible_dois
        else "verified_source_basis"
        if identity_checked
        else "unverified",
        "identity_basis": identity_basis,
        "classification": classification,
        "source_prohibition_detected": source_prohibition,
        "requested_url": requested if isinstance(requested, str) else None,
        "final_url": final if isinstance(final, str) else None,
        "redirect_chain": chain if chain_valid else [],
        "source_sha256": digest,
        "byte_length": len(content),
        "retrieved_at": retrieved_at,
        "access_basis": receipt.get("access_basis"),
        "authenticated": receipt.get("authenticated"),
        "allowed_uses": ["discovery", "storage", "model_input", "factual_synthesis"]
        if grant
        else ["discovery"],
        "copyright_license_status": "unverified",
        "export_allowed": False,
        "redistribution": "not_authorized_by_this_decision",
        "source_restriction": "explicit_caller_or_source_prohibition_takes_precedence",
    }


def fetch_public_source(
    url: str,
    *,
    allowed_hosts: set[str] | None = None,
    timeout: float = 15,
    max_bytes: int = MAX_COPY_BYTES,
) -> dict[str, Any]:
    """Fetch a public HTTPS source with DNS-pinned TLS and bounded bytes.

    A caller may supply a narrower host set. Fetch success grants no retention
    or evidence permission. Each redirect must pass the same public-net check.
    """
    allowed = {host.lower() for host in allowed_hosts} if allowed_hosts is not None else None

    def public_addresses(host: str) -> list[str]:
        try:
            answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise EvidenceError("Source host could not be resolved") from exc
        if not answers:
            raise EvidenceError("Source host did not resolve to a public address")
        addresses = [item[4][0] for item in answers]
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise EvidenceError("Source host did not resolve solely to public addresses")
        return addresses

    def validate(target: str) -> str:
        parsed = urlparse(target)
        host = (parsed.hostname or "").lower()
        try:
            port = parsed.port
        except ValueError as exc:
            raise EvidenceError("Source URL has an invalid port") from exc
        if (
            parsed.scheme != "https"
            or not host
            or (allowed is not None and host not in allowed)
            or parsed.username
            or parsed.password
            or port not in {None, 443}
        ):
            raise EvidenceError("Source URL must be permitted public HTTPS on port 443")
        public_addresses(host)
        return host

    class PublicHTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            if self._tunnel_host:
                raise EvidenceError("Source fetch does not use an HTTP proxy tunnel")
            last_error: OSError | None = None
            for address in public_addresses(self.host):
                try:
                    self.sock = socket.create_connection(
                        (address, self.port), self.timeout, self.source_address
                    )
                    break
                except OSError as exc:
                    last_error = exc
            else:
                raise EvidenceError("Public source connection failed") from last_error
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)

    class PublicHTTPSHandler(HTTPSHandler):
        def https_open(self, request: Request) -> Any:
            return self.do_open(PublicHTTPSConnection, request, context=self._context)

    redirects: list[dict[str, Any]] = []

    class GuardedRedirect(HTTPRedirectHandler):
        def redirect_request(
            self, request: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
        ) -> Request | None:
            validate(newurl)
            redirects.append({"from_url": request.full_url, "to_url": newurl, "status": code})
            return super().redirect_request(request, fp, code, msg, headers, newurl)

    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_COPY_BYTES:
        raise EvidenceError(
            "Direct-fetch byte limit must be positive and at most the intake maximum"
        )
    validate(url)
    request = Request(
        url,
        headers={
            "User-Agent": "ORA-Stage4-intake/1.0",
            "Accept": "application/xml,text/xml,text/html,application/pdf,text/plain",
        },
    )
    try:
        with build_opener(ProxyHandler({}), GuardedRedirect(), PublicHTTPSHandler()).open(
            request, timeout=timeout
        ) as response:
            validate(response.url)
            length = response.headers.get("Content-Length")
            advertised = int(length) if length and length.isdecimal() else None
            if advertised is not None and advertised > max_bytes:
                raise OversizedSourceError(
                    response.url,
                    limit_bytes=max_bytes,
                    advertised_bytes=advertised,
                    observed_prefix=None,
                )
            content = response.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise OversizedSourceError(
                    response.url,
                    limit_bytes=max_bytes,
                    advertised_bytes=advertised,
                    observed_prefix=content,
                )
            return {
                "status": response.status,
                "content": content,
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "byte_length": len(content),
                "content_type": response.headers.get("Content-Type", "application/octet-stream"),
                "requested_url": url,
                "final_url": response.url,
                "redirect_chain": redirects,
                "access_basis": "public_https_unauthenticated",
                "authenticated": False,
                "retrieved_at": utc_now(),
            }
    except HTTPError as exc:
        raise PublicSourceHTTPError(
            exc.code,
            requested_url=url,
            final_url=exc.url,
            redirect_chain=redirects,
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise EvidenceError(f"Source fetch failed: {type(exc).__name__}") from exc


def _actual_dois(text: str) -> set[str]:
    found = set()
    for raw in _DOI.findall(text):
        try:
            found.add(normalize_identifier("doi", raw.rstrip(".,;)]")))
        except ValueError:
            pass
    return found


def _classify(
    blocks: list[Block],
    *,
    declared_surface: str | None,
    expected_doi: str | None,
    identity_basis: str | None,
    truncated: bool,
    content_type: str,
    document_structure: str | None = None,
) -> tuple[str, str]:
    text = "\n".join(item.text for item in blocks)
    if not text.strip():
        return "empty", "No extractable text"
    if _LOGIN.search(text[:3000]) and (
        len(text) < 5000 or document_structure not in {"jats_article_body", "html_article_body"}
    ):
        return "access_page", "Login, CAPTCHA or access notice in returned content"
    observed: set[str] = set()
    if expected_doi:
        expected = normalize_identifier("doi", expected_doi)
        identity_region = (
            blocks[0].text[:8000]
            if content_type.split(";", 1)[0] == "application/pdf"
            else text[:2000]
        )
        observed = _actual_dois(identity_region)
        if observed and expected not in observed:
            return "wrong_article", "Visible source DOI differs from expected publication DOI"
    if declared_surface == "teaser":
        return "teaser", "Discovery surface is a short teaser"
    if declared_surface == "provider_summary":
        return "provider_summary", "Provider-generated text is not source evidence"
    if not identity_basis and (
        not expected_doi or normalize_identifier("doi", expected_doi) not in observed
    ):
        return (
            "identity_unverified",
            "No visible matching DOI or independent article-identity basis",
        )
    if truncated:
        return (
            "partial",
            "Caller reports incomplete bytes or text; exact selected spans may still be usable",
        )
    if document_structure in {"jats_article_body", "html_article_body"}:
        return "full_text", "Checked article-body structure beyond the abstract"
    if document_structure in {"jats_partial_body", "html_partial_body"}:
        return "passage", "Article body is present; full-text completeness not established"
    if document_structure == "html_abstract_section" and declared_surface == "full_text":
        return "passage", "Declared article text; full-text body not established"
    if document_structure in {"jats_abstract_only", "html_abstract_section"}:
        return "abstract", "Delimited published abstract without verified article body"
    if declared_surface == "abstract" or content_type == "application/abstract+text":
        return "abstract", "Published abstract asserted by route; exact content inspected"
    has_section = _FULL.search(text) or any(_FULL.search(block.locator) for block in blocks)
    if content_type == "application/pdf" and has_section:
        return "passage", "Checked PDF section excerpt; full-text completeness not established"
    if declared_surface == "full_text" and len(text) >= 3000 and has_section:
        return "full_text", "Declared full text with article sections"
    if declared_surface == "full_text" and has_section:
        return "passage", "Selected article text; full-text completeness not established"
    return "unverified", "Insufficient evidence to classify as complete abstract or full text"


def _selected_passages(
    blocks: list[Block], selected_texts: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    output: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    if len(selected_texts) > MAX_PASSAGES:
        omissions.append(
            {
                "index_range": [MAX_PASSAGES + 1, len(selected_texts)],
                "reason": "selection_batch_limit; submit another bounded operation",
            }
        )
    for index, selected in enumerate(selected_texts[:MAX_PASSAGES], 1):
        if not isinstance(selected, str) or not selected or len(selected) > 5000:
            omissions.append({"index": index, "reason": "selection_empty_or_too_long"})
            continue
        hits = [(block, block.text.find(selected)) for block in blocks if selected in block.text]
        if len(hits) != 1:
            omissions.append(
                {
                    "index": index,
                    "reason": "selection_absent_or_ambiguous",
                    "occurrences": len(hits),
                }
            )
            continue
        block, start = hits[0]
        output.append(
            {
                "span_id": f"selected-{index}",
                "locator": f"{block.locator}; chars={start}:{start + len(selected)}",
                "text": selected,
                "validation_record": f"{PARSER_VERSION}:exact-substring-in-extracted-block",
            }
        )
    return output, omissions


def _require_model_use_record(
    use: dict[str, Any] | None, copy_ref: dict[str, Any]
) -> dict[str, Any]:
    """Validate a current exact use grant without judging scientific disputes."""
    if use is None or use["body"].get("copy_ref") != copy_ref:
        raise EvidenceError("Source-use record is missing")
    policy = use["body"]
    if (
        policy.get("rights_status") in {"restricted", "not_retained"}
        or policy.get("policy_status") in {"revoked", "requires_source_policy_resolution"}
        or not isinstance(policy.get("allowed_uses"), list)
        or "model_input" not in policy["allowed_uses"]
        or policy.get("allow_model_input") is False
        or policy.get("allowed_for_model") is False
        or policy.get("model_use") in {"restricted", "prohibited"}
    ):
        raise EvidenceError("Source policy does not grant model input use")
    return {"kind": use["kind"], "id": use["id"], "version": use["version"]}


class IntakeService:
    """Inspect one fetched response and persist only evidence allowed by its policy."""

    def __init__(self, library: EvidenceLibrary):
        self.library = library
        self.store = library.store

    def inspect_response(
        self,
        operation_id: str,
        publication_ref: dict[str, Any],
        *,
        content: bytes,
        content_type: str,
        source_url: str,
        retrieved_at: str,
        source_version: str,
        rights_status: str,
        rights_basis: str,
        declared_surface: str | None,
        expected_doi: str | None = None,
        selected_texts: list[str] | None = None,
        truncated: bool = False,
        retrieval_hit_ref: dict[str, Any] | None = None,
        encounter_ref: dict[str, Any] | None = None,
        table_check: dict[str, Any] | None = None,
        identity_basis: str | None = None,
        missing_fields: list[str] | None = None,
        separate_source: bool = False,
        public_fetch_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Classify actual bytes; save exact copy/passages only with explicit permission.

        A PDF table selection requires a rendered-original check supplied by the
        reviewer. Its page, row, header, units and footnote assertions are saved.
        This API never treats an HTTP status as content verification.
        """
        if not operation_id or not source_url or not source_version or not rights_basis:
            raise EvidenceError("Intake needs operation, source, version and policy basis")
        if rights_status not in {"permitted", "restricted", "unknown"}:
            raise EvidenceError("Unknown rights status")
        if not isinstance(content, bytes) or len(content) > MAX_COPY_BYTES:
            raise EvidenceError("Source bytes absent or exceed bounded intake limit")
        if not retrieved_at:
            raise EvidenceError("Retrieval time is required")
        if table_check:
            required = {
                "page",
                "row",
                "header",
                "units",
                "footnote",
                "rendered_original_sha256",
                "reviewer_basis",
            }
            if not required.issubset(table_check) or not table_check["reviewer_basis"]:
                raise EvidenceError(
                    "PDF table check needs page, row, context and rendered-original basis"
                )
        digest = hashlib.sha256(content).hexdigest()
        hit = self.store.get(**retrieval_hit_ref) if retrieval_hit_ref else None
        if retrieval_hit_ref and hit is None:
            raise EvidenceError("Exact retrieval hit is missing")
        source = hit["body"]["source"] if hit else None
        hit_policy = hit["body"].get("source_use_policy") if hit else None
        policy_applies = bool(
            not separate_source
            and source
            and hit_policy
            and source["original_url"] == source_url
            and source["content_sha256"] == digest
            and hit_policy.get("abstract_sha256") == digest
            and "model_input" in hit_policy.get("allowed_uses", [])
        )
        if (
            source
            and source["original_url"] == source_url
            and source["rights_status"] in {"restricted", "not_retained"}
        ):
            rights_status = "restricted"
            rights_basis += "; exact retrieval hit has an explicit source restriction"
        elif rights_status == "unknown" and policy_applies and public_fetch_receipt is None:
            rights_status = "permitted"
            rights_basis += "; " + hit_policy["basis"] + " (private research use only)"
        actual_type = detected_content_type(content, content_type)
        parser = "unavailable"
        structure: str | None = None
        blocks: list[Block] = []
        passages: list[dict[str, Any]] = []
        omissions: list[dict[str, Any]] = []
        try:
            blocks, parser = extract_blocks(content, actual_type, allow_partial_xml=truncated)
            structure = _document_structure(content, actual_type, blocks)
            surface, reason = _classify(
                blocks,
                declared_surface=declared_surface,
                expected_doi=expected_doi,
                identity_basis=identity_basis,
                truncated=truncated,
                content_type=actual_type,
                document_structure=structure,
            )
            if surface in {"abstract", "passage", "partial", "full_text"}:
                passages, omissions = _selected_passages(blocks, selected_texts or [])
        except EvidenceError as exc:
            surface, reason, passages = "parser_failure", str(exc), []
        direct_policy = (
            _public_fetch_use_policy(
                public_fetch_receipt,
                content=content,
                source_url=source_url,
                content_type=content_type,
                retrieved_at=retrieved_at,
                classification=surface,
                identity_basis=identity_basis,
                expected_doi=expected_doi,
                blocks=blocks,
                caller_rights_status=rights_status,
            )
            if isinstance(public_fetch_receipt, dict)
            else None
        )
        if (
            rights_status == "unknown"
            and direct_policy
            and direct_policy["decision"] == "private_research_use"
        ):
            rights_status = "permitted"
            rights_basis += "; " + direct_policy["basis"]
        quality_body = {
            "schema": "ora.evidence.intake_quality.v1",
            "source_url": source_url,
            "source_sha256": digest,
            "byte_length": len(content),
            "returned_content_type": content_type,
            "detected_content_type": actual_type,
            "mime_mismatch": actual_type != content_type.split(";", 1)[0].lower().strip(),
            "retrieved_at": retrieved_at,
            "source_version": source_version,
            "parser": parser,
            "document_structure": structure,
            "parser_version": PARSER_VERSION,
            "classification": surface,
            "classification_reason": reason,
            "rights_status": rights_status,
            "rights_basis": rights_basis,
            "truncated": truncated,
            "identity_basis": identity_basis,
            "inspected_blocks": len(blocks),
            "inspected_text_length": sum(len(b.text) for b in blocks),
            "selected_locators": [p["locator"] for p in passages],
            "table_check": table_check,
            "selection_omissions": omissions,
            "retrieval_hit_ref": retrieval_hit_ref,
            "retrieval_source_use_policy": hit_policy,
            "retrieval_policy_applies_to_bytes": policy_applies,
            "direct_source_use_policy": direct_policy,
            "separate_source": separate_source,
        }

        def save_quality(tx: Any) -> dict[str, Any]:
            saved = tx.put("intake_quality", f"intake-quality-{uuid.uuid4().hex}", quality_body)
            if retrieval_hit_ref:
                hit = tx.get(**retrieval_hit_ref)
                if hit is None:
                    raise EvidenceError("Exact retrieval hit is missing")
                tx.link(saved, hit, "derived_from")
            return {"kind": saved["kind"], "id": saved["id"], "version": saved["version"]}

        quality_ref = self.store.transact(f"{operation_id}-quality", quality_body, save_quality)
        result: dict[str, Any] = {
            "classification": surface,
            "reason": reason,
            "quality_ref": quality_ref,
            "copy_ref": None,
            "passage_refs": [],
            "selection_omissions": omissions,
            "information_need_refs": [],
        }
        if surface not in {"abstract", "passage", "partial", "full_text"}:
            return result
        copy_surface = ("passage" if passages else "metadata") if surface == "partial" else surface
        same_hit_copy = bool(
            not (direct_policy and direct_policy["decision"] == "private_research_use")
            and not truncated
            and source
            and source["origin"] == "original_source"
            and source["original_url"] == source_url
            and source["access_surface"] == copy_surface
        )
        if same_hit_copy and encounter_ref is None:
            encounter_ref = self.library.record_retrieval_hit(
                f"{operation_id}-encounter",
                publication_ref,
                retrieval_hit_ref,
            )["encounter_ref"]
        if encounter_ref is None:
            encounter_ref = self.library.record_encounter(
                f"{operation_id}-encounter",
                publication_ref=publication_ref,
                retrieval_hit_ref=None,
                status="partial" if surface == "partial" else "accepted",
                access_surface=copy_surface,
                retrieved_at=retrieved_at,
                route="direct_inspected_source",
            )["encounter_ref"]
        retain = rights_status == "permitted"
        # A same-URL hit restriction survives a new fetch; an independently
        # permitted alternate URL can establish its own direct-copy policy.
        if (
            source
            and source["rights_status"] in {"restricted", "not_retained"}
            and (not separate_source or source["original_url"] == source_url)
        ):
            retain = False
        elif (
            source
            and source["rights_status"] == "unknown"
            and not (direct_policy and direct_policy["decision"] == "private_research_use")
            and (not separate_source or source["original_url"] == source_url)
        ):
            retain = False
        saved = self.library.add_copy(
            f"{operation_id}-copy",
            publication_ref,
            encounter_ref,
            content=content if retain else None,
            content_sha256=digest,
            mime_type=actual_type if retain else "application/x-source-locator",
            source_version=source_version,
            access_surface=copy_surface if retain else "metadata",
            retrieved_at=retrieved_at,
            rights_basis=rights_basis,
            retain_bytes=retain,
            reference=source_url,
            passages=passages if retain else [],
            inspection_state="checked" if retain else "provider_asserted",
            validation_record=f"{PARSER_VERSION}:{parser}:source-sha256={digest}"
            if retain
            else None,
            mapping_version=f"{PARSER_VERSION}:{parser}:partial_xml={truncated}",
        )
        copy_ref = saved["copy_ref"]
        use_body = {
            "schema": "ora.evidence.source_use.v1",
            "copy_ref": copy_ref,
            "source_sha256": digest,
            "rights_status": rights_status,
            "rights_basis": rights_basis,
            "allowed_uses": direct_policy["allowed_uses"]
            if retain and direct_policy and direct_policy["decision"] == "private_research_use"
            else ["discovery", "storage", "model_input"]
            if retain
            else ["discovery"],
            "export_allowed": False,
            "policy_status": "granted" if retain else "requires_source_policy_resolution",
            "quality_ref": quality_ref,
            "source_url": source_url,
            "retrieval_hit_ref": retrieval_hit_ref,
            "separate_source": separate_source,
            "retrieval_source_use_policy": hit_policy,
            "retrieval_policy_applies_to_bytes": policy_applies,
            "direct_source_use_policy": direct_policy,
        }

        def save_use(tx: Any) -> dict[str, Any]:
            copy = tx.get(**copy_ref)
            quality = tx.get(**quality_ref)
            # Another peer can change the earlier copy's use after the service
            # precheck. Recheck in the transaction that would grant these same
            # retained bytes; an independent acquisition has its own policy.
            if (retain and not separate_source and retrieval_hit_ref
                    and source and source.get("content_sha256") == digest
                    and not hit_allows_reinspection(
                        tx, tx.get(**retrieval_hit_ref), exclude_copy_ref=copy_ref,
                        exclude_quality_ref=quality_ref,
                    )):
                raise EvidenceError("Current use of this same retrieval hit changed before intake grant")
            saved_use = tx.put("source_use", f"source-use-{uuid.uuid4().hex}", use_body)
            tx.link(saved_use, copy, "depends_on")
            tx.link(saved_use, quality, "depends_on")
            tx.bind(
                "source_use.copy",
                f"{copy_ref['id']}:{copy_ref['version']}",
                "source_use",
                saved_use["id"],
            )
            return {
                "kind": saved_use["kind"],
                "id": saved_use["id"],
                "version": saved_use["version"],
            }

        result.update(
            copy_ref=copy_ref,
            passage_refs=saved["passage_refs"],
            source_use_ref=self.store.transact(f"{operation_id}-use", use_body, save_use),
        )
        if surface == "partial" and missing_fields:
            for index, field in enumerate(missing_fields, 1):
                if not isinstance(field, str) or not field.strip():
                    continue
                need = self.open_information_need(
                    f"{operation_id}-need-{index}",
                    publication_ref,
                    field=field,
                    question=f"Find the missing {field} in a permitted same-study source",
                    missing_from=copy_ref,
                    same_study_refs=[],
                    independent_context_refs=[],
                )
                result["information_need_refs"].append(need)
        return result

    def inspect_navigation_document(
        self,
        operation_id: str,
        navigation_ref: dict[str, Any],
        *,
        mission_id: str,
        run_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        """Qualify retained HTML as a document without inventing a publication.

        The navigation is the original source identity. Its bytes remain a
        discovery lead until their exact fetch and use conditions support a
        checked, task-scoped copy for the existing source reader.
        """
        from ora.retrieval.catalog_tools import page_venue_navigation

        if not operation_id or not isinstance(navigation_ref, dict):
            raise EvidenceError("Navigation inspection needs an exact saved reference")
        page_venue_navigation(
            self.store, mission_id=mission_id, run_id=run_id, actor_id=actor_id,
            navigation_ref=navigation_ref, link_offset=0, link_limit=1,
        )
        navigation = self.store.get(**navigation_ref)
        body = navigation["body"]
        if (
            body.get("mission_id") != mission_id
            or body.get("run_id") != run_id
            or body.get("actor_id") != actor_id
        ):
            raise EvidenceError("Navigation belongs to another research peer or run")
        blob = body["blob"]
        content = self.store.read_blob(blob["sha256"])
        digest = hashlib.sha256(content).hexdigest()
        content_type = body["content_type"]
        actual_type = detected_content_type(content, content_type)
        if actual_type != "text/html":
            raise EvidenceError("Navigation document inspection currently supports HTML only")
        blocks, parser = extract_blocks(content, actual_type)
        visible_text = "\n".join(item.text for item in blocks)
        classification = (
            "empty" if not visible_text.strip() else
            "access_page" if _LOGIN.search(visible_text[:3000]) else
            "navigation_document"
        )
        snapshot = body.get("catalog_entry_snapshot") or {}
        access_note = snapshot.get("access_note", "")
        # The normal block reader omits footer and navigation chrome. Check
        # those retained bytes for explicit use prohibitions before granting
        # the document to the model.
        full_page_text = html.unescape(re.sub(r"<[^>]*>", " ", content.decode("utf-8", errors="ignore")))
        catalog_restriction = bool(
            isinstance(access_note, str)
            and (
                re.search(r"(?:generative[- ]AI|AI model) input requires written permission", access_note, re.I)
                or _EXPLICIT_USE_PROHIBITION.search(access_note)
            )
        )
        page_restriction = bool(_EXPLICIT_USE_PROHIBITION.search(full_page_text))
        identity_basis = (
            f"verified_source:navigation:{navigation['id']}:{navigation['version']}:"
            f"sha256={digest}"
        )
        fetch_receipt = {
            "status": body.get("status_code"),
            "access_basis": body.get("access_basis"),
            "authenticated": body.get("authenticated"),
            "content": content,
            "content_sha256": body.get("content_sha256"),
            "byte_length": body.get("byte_length"),
            "content_type": content_type,
            "retrieved_at": body.get("checked_at"),
            "requested_url": body.get("requested_url"),
            "final_url": body.get("final_url"),
            "redirect_chain": body.get("redirect_chain"),
        }
        policy = _public_fetch_use_policy(
            fetch_receipt, content=content, source_url=body["final_url"],
            content_type=content_type, retrieved_at=body["checked_at"],
            classification=classification, identity_basis=identity_basis,
            expected_doi=None, blocks=blocks,
            caller_rights_status="restricted" if catalog_restriction or page_restriction else "unknown",
        )
        permitted = policy["decision"] == "private_research_use"
        quality_body = {
            "schema": "ora.evidence.intake_quality.v1",
            "navigation_ref": navigation_ref,
            "catalog_ref": body["request"]["catalog_ref"],
            "catalog_version": body["catalog_version"],
            "source_url": body["final_url"],
            "requested_url": body["requested_url"],
            "redirect_chain": body["redirect_chain"],
            "source_sha256": digest,
            "byte_length": len(content),
            "returned_content_type": content_type,
            "detected_content_type": actual_type,
            "retrieved_at": body["checked_at"],
            "source_version": f"navigation:{navigation['id']}:{navigation['version']}",
            "parser": parser,
            "parser_version": PARSER_VERSION,
            "classification": classification,
            "classification_reason": (
                "Saved HTML document text; publication or study identity not inferred"
                if classification == "navigation_document" else
                "Navigation supplied no usable HTML document text"
            ),
            "rights_status": "permitted" if permitted else "restricted" if catalog_restriction or page_restriction else "unknown",
            "rights_basis": policy["basis"],
            "identity_basis": identity_basis,
            "inspected_blocks": len(blocks),
            "inspected_text_length": len(visible_text),
            "truncated": False,
            "selected_locators": [],
            "direct_source_use_policy": policy,
            "catalog_restriction": catalog_restriction,
            "page_restriction": page_restriction,
        }

        def save_quality(tx: Any) -> dict[str, Any]:
            exact = tx.get(**navigation_ref)
            if exact != navigation:
                raise EvidenceError("Navigation changed before quality commit")
            saved = tx.put("intake_quality", f"intake-quality-{uuid.uuid4().hex}", quality_body)
            tx.link(saved, exact, "derived_from")
            return {key: saved[key] for key in ("kind", "id", "version")}

        quality_ref = self.store.transact(
            f"{operation_id}-quality", quality_body, save_quality, mission_id=mission_id,
        )
        result: dict[str, Any] = {
            "classification": classification,
            "quality_ref": quality_ref,
            "navigation_ref": navigation_ref,
            "copy_ref": None,
            "passage_refs": [],
            "source_use_ref": None,
            "model_input_status": "permitted" if permitted else "not_granted",
        }
        prior_copy = self.store.resolve(
            "source_copy.navigation", f"{navigation_ref['id']}:{navigation_ref['version']}"
        )
        copy_ref = None
        if prior_copy:
            if (
                prior_copy["body"].get("navigation_ref") != navigation_ref
                or prior_copy["body"].get("content_sha256") != digest
                or prior_copy["body"].get("reference") != body["final_url"]
            ):
                raise EvidenceError("Existing navigation copy disagrees with its saved origin")
            prior_ref = {key: prior_copy[key] for key in ("kind", "id", "version")}
            current_use = self.store.resolve(
                "source_use.copy", f"{prior_copy['id']}:{prior_copy['version']}"
            )
            if current_use:
                try:
                    if not permitted:
                        raise EvidenceError("Current navigation policy does not grant model input")
                    if prior_copy["body"].get("model_use") in {"restricted", "prohibited"}:
                        raise EvidenceError("Current copy does not grant model input")
                    use_ref = _require_model_use_record(current_use, prior_ref)
                except EvidenceError:
                    result["model_input_status"] = "not_granted"
                else:
                    result["copy_ref"] = prior_ref
                    result["source_use_ref"] = use_ref
                return result
            if prior_copy["body"].get("intake_operation_id") != operation_id:
                result["model_input_status"] = "not_granted"
                return result
            copy_ref = prior_ref
        if not permitted:
            return result
        if copy_ref is None:
            encounter_ref = self.library.record_encounter(
                f"{operation_id}-encounter", publication_ref=None, retrieval_hit_ref=None,
                navigation_ref=navigation_ref, status="accepted", access_surface="passage",
                retrieved_at=body["checked_at"], route="catalog_navigation",
            )["encounter_ref"]
            saved = self.library.add_copy(
                f"{operation_id}-copy", None, encounter_ref,
                navigation_ref=navigation_ref, intake_operation_id=operation_id,
                content=content, content_sha256=digest,
                mime_type=content_type,
                source_version=quality_body["source_version"], access_surface="passage",
                retrieved_at=body["checked_at"], rights_basis=policy["basis"],
                retain_bytes=True, reference=body["final_url"], passages=[],
                inspection_state="checked",
                validation_record=f"{PARSER_VERSION}:{parser}:source-sha256={digest}",
                mapping_version=f"{PARSER_VERSION}:{parser}",
            )
            copy_ref = saved["copy_ref"]
        use_body = {
            "schema": "ora.evidence.source_use.v1",
            "copy_ref": copy_ref,
            "navigation_ref": navigation_ref,
            "source_sha256": digest,
            "rights_status": "permitted",
            "rights_basis": policy["basis"],
            "allowed_uses": policy["allowed_uses"],
            "export_allowed": False,
            "policy_status": "granted",
            "quality_ref": quality_ref,
            "source_url": body["final_url"],
            "direct_source_use_policy": policy,
        }

        def save_use(tx: Any) -> dict[str, Any]:
            exact_copy = tx.get(**copy_ref)
            exact_nav = tx.get(**navigation_ref)
            if (
                exact_copy is None or exact_nav != navigation
                or exact_copy["body"].get("navigation_ref") != navigation_ref
                or exact_copy["body"].get("intake_operation_id") != operation_id
                or tx.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
            ):
                raise EvidenceError("Navigation copy changed before source-use commit")
            saved_use = tx.put("source_use", f"source-use-{uuid.uuid4().hex}", use_body)
            tx.link(saved_use, exact_copy, "depends_on")
            tx.link(saved_use, exact_nav, "depends_on")
            tx.link(saved_use, tx.get(**quality_ref), "depends_on")
            tx.bind(
                "source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}",
                "source_use", saved_use["id"],
            )
            return {key: saved_use[key] for key in ("kind", "id", "version")}

        result.update(
            copy_ref=copy_ref,
            source_use_ref=self.store.transact(
                f"{operation_id}-use", use_body, save_use, mission_id=mission_id,
            ),
        )
        return result

    def save_public_document_fetch(
        self, operation_id: str, hit_ref: dict[str, Any], response_ref: dict[str, Any],
        *, mission_id: str, run_id: str, actor_id: str, task_id: str,
        fetched: dict[str, Any],
    ) -> dict[str, Any]:
        """Retain an exact public fetch behind a same-task browser URL lead."""
        if not operation_id or hit_ref.get("kind") != "retrieval_hit" or response_ref.get("kind") != "model_response_receipt":
            raise EvidenceError("Public document fetch needs exact hit and response references")
        hit = self.store.get(**hit_ref)
        response = self.store.get(**response_ref)
        call_ref = response["body"].get("call_ref") if response else None
        call = self.store.get(**call_ref) if isinstance(call_ref, dict) else None
        attempt = self.store.get("agent_attempt", response["body"].get("attempt_id")) if response else None
        if (
            hit is None or response is None or call is None or attempt is None
            or attempt["body"].get("run_id") != run_id
            or attempt["body"].get("call_ref") != call_ref
            or hit["body"].get("route") != "groq.browser_search.url_leads"
            or hit["body"].get("source", {}).get("origin") != "original_source"
            or hit["body"].get("source", {}).get("access_surface") != "metadata"
            or hit["body"].get("source", {}).get("content") is not None
            or any(hit["body"].get(key) != value for key, value in (
                ("mission_id", mission_id), ("task_id", task_id),
                ("agent_instance_id", actor_id)))
            or any(call["body"].get(key) != value for key, value in (
                ("mission_id", mission_id), ("task_id", task_id),
                ("instance_id", actor_id)))
            or not any(link["relation"] == "derived_from" and link["target"] == response_ref
                       for link in self.store.links(hit))
        ):
            raise EvidenceError("Public document lead is outside the original peer task")
        content = fetched.get("content")
        requested_url = hit["body"]["source"].get("original_url")
        if (
            fetched.get("status") != 200
            or not isinstance(content, bytes) or not content or len(content) > MAX_COPY_BYTES
            or fetched.get("requested_url") != requested_url
            or fetched.get("content_sha256") != hashlib.sha256(content).hexdigest()
            or fetched.get("byte_length") != len(content)
            or not isinstance(fetched.get("final_url"), str)
            or not isinstance(fetched.get("retrieved_at"), str)
            or not isinstance(fetched.get("content_type"), str)
        ):
            raise EvidenceError("Public document fetch receipt disagrees with exact source bytes")
        blob = self.store.put_blob(content)
        body = {
            "schema": "ora.evidence.public_document_fetch.v1",
            "operation_id": operation_id,
            "mission_id": mission_id, "run_id": run_id, "actor_id": actor_id,
            "task_id": task_id, "hit_ref": hit_ref, "response_ref": response_ref,
            "status": 200, "requested_url": requested_url,
            "final_url": fetched["final_url"],
            "redirect_chain": fetched.get("redirect_chain"),
            "checked_at": fetched["retrieved_at"],
            "content_type": fetched["content_type"],
            "detected_content_type": detected_content_type(content, fetched["content_type"]),
            "content_sha256": fetched["content_sha256"],
            "byte_length": len(content),
            "access_basis": fetched.get("access_basis"),
            "authenticated": fetched.get("authenticated"),
            "hit_rights_status": hit["body"]["source"].get("rights_status"),
            "blob": blob,
        }
        record_id = f"public-document-fetch:{operation_id}"

        def save(tx: Any) -> dict[str, Any]:
            if tx.get(**hit_ref) != hit or tx.get(**response_ref) != response:
                raise EvidenceError("Public document lead changed before fetch record")
            record = tx.put("public_document_fetch", record_id, body)
            tx.link(record, hit, "fetched_from_browser_hit")
            tx.link(record, response, "derived_from_browser_response")
            return {key: record[key] for key in ("kind", "id", "version")}

        return self.store.transact(operation_id, body, save, mission_id=mission_id)

    def inspect_public_document(
        self, operation_id: str, document_ref: dict[str, Any], *,
        mission_id: str, run_id: str, actor_id: str, task_id: str,
    ) -> dict[str, Any]:
        """Qualify saved browser-fetched HTML without asserting publication identity."""
        document = self.store.get(**document_ref)
        if document is None or document_ref.get("kind") != "public_document_fetch":
            raise EvidenceError("Exact saved public document fetch is missing")
        body = document["body"]
        if any(body.get(key) != value for key, value in (
            ("mission_id", mission_id), ("run_id", run_id),
            ("actor_id", actor_id), ("task_id", task_id),
        )):
            raise EvidenceError("Public document belongs to another research peer or task")
        hit = self.store.get(**body["hit_ref"])
        if (
            hit is None or hit["body"].get("source", {}).get("original_url") != body["requested_url"]
            or not any(link["relation"] == "derived_from" and link["target"] == body["response_ref"]
                       for link in self.store.links(hit))
        ):
            raise EvidenceError("Public document lost its exact browser lead")
        content = self.store.read_blob(body["blob"]["sha256"])
        digest = hashlib.sha256(content).hexdigest()
        if digest != body["content_sha256"] or len(content) != body["byte_length"]:
            raise EvidenceError("Public document bytes disagree with saved fetch")
        actual_type = detected_content_type(content, body["content_type"])
        if actual_type != body.get("detected_content_type"):
            raise EvidenceError("Public document type disagrees with saved bytes")
        if actual_type != "text/html":
            raise EvidenceError("Public document inspection currently supports HTML only")
        blocks, parser = extract_blocks(content, actual_type)
        visible_text = "\n".join(item.text for item in blocks)
        classification = (
            "empty" if not visible_text.strip() else
            "access_page" if _LOGIN.search(visible_text[:3000]) else
            "web_document"
        )
        full_page_text = html.unescape(re.sub(r"<[^>]*>", " ", content.decode("utf-8", errors="ignore")))
        page_restriction = bool(_EXPLICIT_USE_PROHIBITION.search(full_page_text))
        hit_restriction = body.get("hit_rights_status") in {"restricted", "not_retained"}
        identity_basis = (
            f"verified_source:public_document:{document['id']}:{document['version']}:"
            f"sha256={digest}"
        )
        receipt = {
            "status": body["status"], "access_basis": body.get("access_basis"),
            "authenticated": body.get("authenticated"), "content": content,
            "content_sha256": digest, "byte_length": len(content),
            "content_type": body["content_type"], "retrieved_at": body["checked_at"],
            "requested_url": body["requested_url"], "final_url": body["final_url"],
            "redirect_chain": body["redirect_chain"],
        }
        policy = _public_fetch_use_policy(
            receipt, content=content, source_url=body["final_url"],
            content_type=body["content_type"], retrieved_at=body["checked_at"],
            classification=classification, identity_basis=identity_basis,
            expected_doi=None, blocks=blocks,
            caller_rights_status="restricted" if page_restriction or hit_restriction else "unknown",
        )
        permitted = policy["decision"] == "private_research_use"
        quality_body = {
            "schema": "ora.evidence.intake_quality.v1", "document_ref": document_ref,
            "retrieval_hit_ref": body["hit_ref"], "response_ref": body["response_ref"],
            "source_url": body["final_url"], "requested_url": body["requested_url"],
            "redirect_chain": body["redirect_chain"], "source_sha256": digest,
            "byte_length": len(content), "returned_content_type": body["content_type"],
            "detected_content_type": actual_type, "retrieved_at": body["checked_at"],
            "source_version": f"public_document:{document['id']}:{document['version']}",
            "parser": parser, "parser_version": PARSER_VERSION,
            "classification": classification,
            "classification_reason": (
                "Saved HTML document text; publication or study identity not inferred"
                if classification == "web_document" else
                "Public fetch supplied no usable HTML document text"
            ),
            "rights_status": "permitted" if permitted else "restricted" if page_restriction or hit_restriction else "unknown",
            "rights_basis": policy["basis"], "identity_basis": identity_basis,
            "inspected_blocks": len(blocks), "inspected_text_length": len(visible_text),
            "truncated": False, "selected_locators": [],
            "direct_source_use_policy": policy, "page_restriction": page_restriction,
            "hit_restriction": hit_restriction,
        }

        def save_quality(tx: Any) -> dict[str, Any]:
            if tx.get(**document_ref) != document:
                raise EvidenceError("Public document changed before quality commit")
            saved = tx.put("intake_quality", f"intake-quality-{uuid.uuid4().hex}", quality_body)
            tx.link(saved, document, "derived_from")
            return {key: saved[key] for key in ("kind", "id", "version")}

        quality_ref = self.store.transact(
            f"{operation_id}-quality", quality_body, save_quality, mission_id=mission_id,
        )
        result = {
            "classification": classification, "quality_ref": quality_ref,
            "document_ref": document_ref, "copy_ref": None, "passage_refs": [],
            "source_use_ref": None, "model_input_status": "permitted" if permitted else "not_granted",
        }
        prior_copy = self.store.resolve(
            "source_copy.public_document", f"{document_ref['id']}:{document_ref['version']}"
        )
        copy_ref = None
        if prior_copy:
            if (
                prior_copy["body"].get("document_ref") != document_ref
                or prior_copy["body"].get("content_sha256") != digest
                or prior_copy["body"].get("reference") != body["final_url"]
            ):
                raise EvidenceError("Existing public-document copy disagrees with its saved origin")
            prior_ref = {key: prior_copy[key] for key in ("kind", "id", "version")}
            current_use = self.store.resolve(
                "source_use.copy", f"{prior_copy['id']}:{prior_copy['version']}"
            )
            if current_use:
                try:
                    if not permitted:
                        raise EvidenceError("Current public-document policy does not grant model input")
                    use_ref = _require_model_use_record(current_use, prior_ref)
                except EvidenceError:
                    result["model_input_status"] = "not_granted"
                else:
                    result["copy_ref"] = prior_ref
                    result["source_use_ref"] = use_ref
                return result
            if prior_copy["body"].get("intake_operation_id") != operation_id:
                result["model_input_status"] = "not_granted"
                return result
            copy_ref = prior_ref
        if not permitted:
            return result
        if copy_ref is None:
            encounter_ref = self.library.record_encounter(
                f"{operation_id}-encounter", publication_ref=None, retrieval_hit_ref=None,
                document_ref=document_ref, status="accepted", access_surface="passage",
                retrieved_at=body["checked_at"], route="public_document_fetch",
            )["encounter_ref"]
            saved = self.library.add_copy(
                f"{operation_id}-copy", None, encounter_ref,
                document_ref=document_ref, intake_operation_id=operation_id,
                content=content, content_sha256=digest, mime_type=actual_type,
                source_version=quality_body["source_version"], access_surface="passage",
                retrieved_at=body["checked_at"], rights_basis=policy["basis"],
                retain_bytes=True, reference=body["final_url"], passages=[],
                inspection_state="checked",
                validation_record=f"{PARSER_VERSION}:{parser}:source-sha256={digest}",
                mapping_version=f"{PARSER_VERSION}:{parser}",
            )
            copy_ref = saved["copy_ref"]
        use_body = {
            "schema": "ora.evidence.source_use.v1", "copy_ref": copy_ref,
            "document_ref": document_ref, "source_sha256": digest,
            "rights_status": "permitted", "rights_basis": policy["basis"],
            "allowed_uses": policy["allowed_uses"], "export_allowed": False,
            "policy_status": "granted", "quality_ref": quality_ref,
            "source_url": body["final_url"], "direct_source_use_policy": policy,
        }

        def save_use(tx: Any) -> dict[str, Any]:
            exact_copy = tx.get(**copy_ref)
            if (
                exact_copy is None or tx.get(**document_ref) != document
                or exact_copy["body"].get("document_ref") != document_ref
                or exact_copy["body"].get("intake_operation_id") != operation_id
                or tx.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
            ):
                raise EvidenceError("Public-document copy changed before source-use commit")
            saved_use = tx.put("source_use", f"source-use-{uuid.uuid4().hex}", use_body)
            tx.link(saved_use, exact_copy, "depends_on")
            tx.link(saved_use, document, "depends_on")
            tx.link(saved_use, tx.get(**quality_ref), "depends_on")
            tx.bind(
                "source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}",
                "source_use", saved_use["id"],
            )
            return {key: saved_use[key] for key in ("kind", "id", "version")}

        result.update(
            copy_ref=copy_ref,
            source_use_ref=self.store.transact(
                f"{operation_id}-use", use_body, save_use, mission_id=mission_id,
            ),
        )
        return result

    def open_information_need(
        self,
        operation_id: str,
        publication_ref: dict[str, Any],
        *,
        field: str,
        question: str,
        missing_from: dict[str, Any],
        same_study_refs: list[dict[str, Any]],
        independent_context_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Record two pursuit paths without borrowing details across studies."""
        if not field.strip() or not question.strip() or not missing_from:
            raise EvidenceError(
                "Named information need requires a field, question and exact source"
            )
        primary_families = {
            x["count_key"] for x in self.library.study_families_for_publication(publication_ref)
        }
        for candidate in same_study_refs:
            families = {
                x["count_key"] for x in self.library.study_families_for_publication(candidate)
            }
            if not primary_families or not primary_families.intersection(families):
                raise EvidenceError(
                    "Same-study companion lacks a verified shared study-family link"
                )
        for candidate in independent_context_refs:
            if candidate["id"] == publication_ref["id"] or candidate in same_study_refs:
                raise EvidenceError(
                    "Independent context cannot be the target or same-study companion"
                )
            families = {
                x["count_key"] for x in self.library.study_families_for_publication(candidate)
            }
            if primary_families.intersection(families):
                raise EvidenceError("Shared study family is not independent context")
        body = {
            "schema": "ora.evidence.information_need.v1",
            "publication_ref": publication_ref,
            "field": field,
            "question": question,
            "missing_from": missing_from,
            "same_study_refs": same_study_refs,
            "independent_context_refs": independent_context_refs,
            "status": "open",
            "attribution_rule": "independent_context_does_not_fill_target_field",
        }

        def save(tx: Any) -> dict[str, Any]:
            source = tx.get(**missing_from)
            if source is None or source["kind"] not in {"source_copy", "intake_quality"}:
                raise EvidenceError("Missing-field source must be an exact copy or quality record")
            record = tx.put("information_need", f"information-need-{uuid.uuid4().hex}", body)
            tx.link(record, source, "depends_on")
            for item in same_study_refs + independent_context_refs:
                other = tx.get(**item)
                if other is None:
                    raise EvidenceError("Information-need candidate record is missing")
                tx.link(record, other, "candidate_source")
            return {"kind": record["kind"], "id": record["id"], "version": record["version"]}

        return self.store.transact(operation_id, body, save)

    def _current_model_use(self, copy_ref: dict[str, Any]) -> dict[str, Any]:
        use = self.store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
        return _require_model_use_record(use, copy_ref)

    def _prepare_model_copy(self, copy_ref: dict[str, Any]) -> _PreparedModelCopy:
        copy = self.store.get(**copy_ref)
        if copy is None or copy["body"].get("inspection_state") != "checked":
            raise EvidenceError("Exact checked source copy is missing")
        use_ref = self._current_model_use(copy_ref)
        blob = copy["body"].get("blob")
        if not blob:
            raise EvidenceError("Copy does not retain permitted source bytes")
        content = self.store.read_blob(blob["sha256"])
        if hashlib.sha256(content).hexdigest() != copy["body"]["content_sha256"]:
            raise EvidenceError("Source-copy checksum differs from retained bytes")
        blocks, _ = extract_blocks(
            content,
            copy["body"]["mime_type"],
            allow_partial_xml=(copy["body"].get("mapping_version") or "").endswith(
                "partial_xml=True"
            ),
        )
        indexed: dict[str, list[str]] = {}
        for block in blocks:
            indexed.setdefault(block.locator, []).append(block.text)
        return _PreparedModelCopy(
            copy_ref, copy, indexed, use_ref, _exact_submitted_text(self.store, copy, content)
        )

    def source_for_model(
        self, passage_ref: dict[str, Any], *, _prepared: _PreparedModelCopy | None = None
    ) -> dict[str, Any]:
        """Resolve one exact retained passage and its current model-use policy.

        A packet may pass a call-local prepared copy to avoid repeat extraction.
        Its caller rechecks current policy after assembling the entire packet.
        """
        passage = self.store.get(**passage_ref)
        if passage is None or passage["kind"] != "source_passage":
            raise EvidenceError("Exact retained passage is missing")
        if passage["body"].get("inspection_state") != "checked":
            raise EvidenceError("Selected passage was not checked against its source copy")
        copy_ref = passage["body"]["copy_ref"]
        if _prepared is not None and copy_ref != _prepared.copy_ref:
            raise EvidenceError("Selected passage belongs to a different source copy")
        prepared = _prepared or self._prepare_model_copy(copy_ref)
        locator = passage["body"]["locator"]
        prefix, marker, bounds = locator.rpartition("; chars=")
        if not marker or not re.fullmatch(r"\d+:\d+", bounds):
            raise EvidenceError("Passage locator lacks exact extracted-text offsets")
        start, end = (int(value) for value in bounds.split(":"))
        matches = prepared.blocks_by_locator.get(prefix, [])
        submitted = _is_exact_submitted_passage(passage, prepared.copy, prepared.submitted_text)
        if not submitted and (len(matches) != 1 or matches[0][start:end] != passage["body"]["text"]):
            raise EvidenceError("Passage does not replay at its source locator")
        if _prepared is None and self._current_model_use(copy_ref) != prepared.source_use_ref:
            raise EvidenceError("Source-use policy changed during passage resolution")
        return {
            "passage_ref": passage_ref,
            "copy_ref": copy_ref,
            "text": passage["body"]["text"],
            "locator": passage["body"]["locator"],
            "source_use_ref": prepared.source_use_ref,
        }


def checked_model_text(
    library: EvidenceLibrary, copy_ref: dict[str, Any], passage_refs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build an exact, policy-checked text packet from selected source passages.

    Review or correction flags are returned for scientific judgment. They do
    not erase intact passages. A changed use grant or mismatched source does.
    """
    if (
        not isinstance(copy_ref, dict)
        or set(copy_ref) != {"kind", "id", "version"}
        or copy_ref["kind"] != "source_copy"
        or not isinstance(copy_ref["id"], str)
        or type(copy_ref["version"]) is not int
        or copy_ref["version"] < 1
    ):
        raise EvidenceError("Exact source-copy reference is required")
    if not isinstance(passage_refs, list) or not passage_refs:
        raise EvidenceError("At least one exact selected passage is required")
    if len(passage_refs) > MAX_PASSAGES:
        raise EvidenceError("Selected passage packet exceeds bounded batch size")
    store = library.store
    service = IntakeService(library)
    prepared = service._prepare_model_copy(copy_ref)
    copy = prepared.copy
    entries = []
    seen: set[tuple[str, int]] = set()
    policy_ref = None
    for pointer in passage_refs:
        if (
            not isinstance(pointer, dict)
            or set(pointer) != {"kind", "id", "version"}
            or pointer["kind"] != "source_passage"
            or not isinstance(pointer["id"], str)
            or type(pointer["version"]) is not int
            or pointer["version"] < 1
        ):
            raise EvidenceError("Exact source-passage reference is required")
        key = (pointer["id"], pointer["version"])
        if key in seen:
            raise EvidenceError("Selected passage is duplicated")
        seen.add(key)
        resolved = service.source_for_model(pointer, _prepared=prepared)
        if policy_ref is not None and resolved["source_use_ref"] != policy_ref:
            raise EvidenceError("Source-use policy changed during packet construction")
        policy_ref = resolved["source_use_ref"]
        passage = store.get(**pointer)
        entries.append(
            {
                "passage_ref": pointer,
                "locator": resolved["locator"],
                "text": resolved["text"],
                "text_sha256": hashlib.sha256(resolved["text"].encode("utf-8")).hexdigest(),
                "needs_review": store.review_status(passage),
            }
        )
    if service._current_model_use(copy_ref) != policy_ref:
        raise EvidenceError("Source-use policy changed during packet construction")
    current_policy = store.get(**policy_ref)
    quality_ref = current_policy["body"].get("quality_ref")
    quality = store.get(**quality_ref) if isinstance(quality_ref, dict) else None
    if quality is None or quality["kind"] != "intake_quality":
        raise EvidenceError("Exact source-quality record is missing")
    original_hash = copy["body"].get("content_sha256")
    if (
        not original_hash
        or current_policy["body"].get("source_sha256") != original_hash
        or quality["body"].get("source_sha256") != original_hash
    ):
        raise EvidenceError("Source copy, quality and use hashes disagree")
    text = "\n\n".join(
        f"[{entry['passage_ref']['id']} | {entry['locator']}]\n{entry['text']}" for entry in entries
    )
    needs = []
    after_id = ""
    while True:
        page = store.records("information_need", limit=1000, after_id=after_id)
        if not page:
            break
        needs.extend(
            {
                "need_ref": {key: record[key] for key in ("kind", "id", "version")},
                "field": record["body"]["field"],
                "status": record["body"]["status"],
            }
            for record in page
            if record["body"].get("missing_from") == copy_ref
        )
        after_id = page[-1]["id"]
    return {
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "copy_ref": copy_ref,
        "content_sha256": original_hash,
        "passage_refs": passage_refs,
        "source_use_refs": [policy_ref],
        "passages": entries,
        "quality_ref": quality_ref,
        "classification": quality["body"].get("classification"),
        "truncated": quality["body"].get("truncated"),
        "selection_omissions": quality["body"].get("selection_omissions", []),
        "information_needs": needs,
        "needs_review": {
            "copy": store.review_status(copy),
            "source_use": store.review_status(current_policy),
            "quality": store.review_status(quality),
        },
    }


def default_exact_submitted_passage(
    library: EvidenceLibrary, copy_ref: dict[str, Any]
) -> dict[str, Any] | None:
    """Find a citable whole-submission span without changing older raw-copy input.

    A historical submission without a separate use record keeps its existing
    raw-text route. A present use record must pass the normal checked-passage
    boundary, including its current grant, before the span becomes a default.
    """
    if (
        not isinstance(copy_ref, dict)
        or set(copy_ref) != {"kind", "id", "version"}
        or copy_ref.get("kind") != "source_copy"
    ):
        return None
    store = library.store
    copy = store.get(**copy_ref)
    if copy is None:
        return None
    passage_id = f"{copy_ref['id']}:supplied-1"
    body = copy["body"]
    if body.get("passage_ids") != [passage_id]:
        return None
    if store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}") is None:
        return None
    blob = body.get("blob")
    if not isinstance(blob, dict):
        return None
    submitted = _exact_submitted_text(store, copy, store.read_blob(blob["sha256"]))
    passage = store.get("source_passage", passage_id)
    if passage is None or not _is_exact_submitted_passage(passage, copy, submitted):
        return None
    pointer = {key: passage[key] for key in ("kind", "id", "version")}
    checked_model_text(library, copy_ref, [pointer])
    return pointer


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
