"""Explicit scholarly and broad discovery HTTP routes; no scientific routing policy."""

from __future__ import annotations

import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class HTTPResult:
    status: int
    body: bytes
    headers: dict[str, str]


class HTTPDispatchBlocked(RuntimeError):
    """A local transport guard stopped this request before any HTTP dispatch."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def http_transport(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20,
) -> HTTPResult:
    """One bounded request. Credentials stay in headers and never enter receipts."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"User-Agent": "ORA-stage4-discovery/0.1", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HTTPResult(
                response.status,
                response.read(8 * 1024 * 1024 + 1),
                {k.lower(): v for k, v in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return HTTPResult(
            exc.code, exc.read(8 * 1024 * 1024 + 1), {k.lower(): v for k, v in exc.headers.items()}
        )


ROUTES = {
    "europe_pmc.search": {
        "provider": "europe_pmc",
        "index": "europe_pmc_articles",
        "operation": "search",
        "adapter": "opaque_cursor",
        "keyed": False,
        "surfaces": ["metadata", "abstract"],
        "query_description": "Search biomedical articles with caller-chosen terms or field syntax; cursor and date/journal filters are available.",
        "returned_content": "Core metadata and a returned published abstract for private research use; no full-text access is inferred.",
    },
    "crossref.search": {
        "provider": "crossref",
        "index": "crossref_works",
        "operation": "search",
        "adapter": "count_offset",
        "keyed": False,
        "surfaces": ["metadata"],
        "query_description": "Search deposited bibliographic metadata with caller-chosen terms, offset, date and type filters.",
        "returned_content": "DOI and bibliographic details, plus a returned published abstract for private research use; copyright license remains unverified.",
    },
    "crossref.doi": {
        "provider": "crossref",
        "index": "crossref_works",
        "operation": "fetch",
        "adapter": "count_offset",
        "keyed": False,
        "surfaces": ["metadata"],
        "query_description": "Fetch one exact DOI from selected_id, filters.doi or query; no topic search.",
        "returned_content": "Exact DOI metadata and any returned published abstract for private research use; no full-text license is inferred.",
    },
    "pubmed.search": {
        "provider": "ncbi",
        "index": "pubmed",
        "operation": "search",
        "adapter": "count_offset",
        "keyed": False,
        "surfaces": ["metadata"],
        "query_description": "Search PubMed with caller-chosen terms or subject syntax, offset and publication-date filters.",
        "returned_content": "ESearch IDs plus ESummary publication metadata, not article abstracts or full text.",
    },
    "pubmed.pmid": {
        "provider": "ncbi",
        "index": "pubmed",
        "operation": "fetch",
        "adapter": "count_offset",
        "keyed": False,
        "surfaces": ["metadata"],
        "query_description": "Fetch one exact numeric PMID from selected_id, filters.pmid or query; no topic search.",
        "returned_content": "ESummary publication metadata, not a published abstract or full text.",
    },
    "pubmed.abstract": {
        "provider": "ncbi",
        "index": "pubmed",
        "operation": "fetch",
        "adapter": "count_offset",
        "keyed": False,
        "surfaces": ["metadata", "abstract"],
        "query_description": "Fetch the available published abstract for one exact numeric PMID from selected_id, filters.pmid or query; no topic search.",
        "returned_content": "EFetch XML citation metadata and its published abstract when present; no article full text is inferred.",
    },
    "exa.search": {
        "provider": "exa",
        "index": "exa_web",
        "operation": "search",
        "adapter": "opaque_cursor",
        "keyed": True,
        "surfaces": ["teaser"],
        "query_description": "Broad web discovery from caller-chosen query, mode and publication dates; no offset/cursor here.",
        "returned_content": "Ranked source URLs and search teasers; no verified publication passage.",
        "modes": ["instant", "fast", "auto"],
    },
    "exa.contents": {
        "provider": "exa",
        "index": "exa_web",
        "operation": "fetch",
        "adapter": "opaque_cursor",
        "keyed": True,
        "surfaces": ["passage", "provider_summary"],
        "query_description": "Fetch one explicitly selected Exa hit ID or URL with character and cache-age controls.",
        "returned_content": "Candidate extracted text or provider-generated summary, separately labeled and requiring source-copy qualification.",
    },
}

ROUTE_FILTERS = {
    "europe_pmc.search": frozenset({"from_date", "until_date", "journal"}),
    "crossref.search": frozenset({"from_date", "until_date", "type"}),
    "crossref.doi": frozenset({"doi", "selected_id"}),
    "pubmed.search": frozenset({"from_date", "until_date"}),
    "pubmed.pmid": frozenset({"pmid", "selected_id"}),
    "pubmed.abstract": frozenset({"pmid", "selected_id"}),
    "exa.search": frozenset({"from_date", "until_date", "mode", "highlights"}),
    "exa.contents": frozenset(
        {"id", "url", "max_characters", "max_age_hours", "selection_reason"}
    ),
}

for _provider in ("tavily", "searxng"):
    ROUTES[f"{_provider}.search"] = {
        "provider": _provider, "index": f"{_provider}_web", "operation": "search",
        "adapter": "opaque_cursor", "keyed": _provider == "tavily",
        "surfaces": ["teaser"], "query_description": "Explicit complementary web discovery.",
        "returned_content": "Candidate excerpts and URLs; inspect source copies before citing.",
    }
    ROUTE_FILTERS[f"{_provider}.search"] = frozenset()


def available_routes() -> dict[str, dict]:
    """Capability descriptions; agents decide which route and query to use."""
    return {
        key: {**value, "supported_filters": sorted(ROUTE_FILTERS[key])}
        for key, value in ROUTES.items()
    }


def _url(base: str, parameters: dict[str, object]) -> str:
    return base + "?" + urllib.parse.urlencode(parameters)


def _date_filters(filters: dict, *, begin: str, end: str) -> dict[str, str]:
    output = {}
    for key, target in (("from_date", begin), ("until_date", end)):
        value = filters.get(key)
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(f"{key} must be YYYY-MM-DD")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{key} must be a real YYYY-MM-DD date") from exc
            output[target] = value
    if filters.get("from_date") and filters.get("until_date"):
        if filters["from_date"] > filters["until_date"]:
            raise ValueError("from_date must not follow until_date")
    return output


def _validated_filters(route: str, value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Route filters must be an object with text keys")
    unsupported = set(value) - ROUTE_FILTERS[route]
    if unsupported:
        raise ValueError(f"Unsupported {route} filters: {', '.join(sorted(unsupported))}")
    return value


def _text_filter(value: object, name: str, *, max_length: int = 160) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{name} must be bounded nonempty text")
    return value.strip()


def _selected_public_url(value: object) -> str:
    url = _text_filter(value, "Exa Contents URL", max_length=2048)
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Exa Contents URL must be public HTTPS without credentials")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None  # A hostname is resolved by Exa, not by this adapter.
    if address is not None and not address.is_global:
        raise ValueError("Exa Contents URL must be globally routable")
    return url


def _exact_identifier(filters: dict, query: object, field: str) -> object:
    """Reconcile generic model selection with route-specific exact identifiers."""
    values = [filters.get(field), filters.get("selected_id"), query]
    if any(value is not None and not isinstance(value, str) for value in values):
        raise ValueError(f"{field} identifier must be text")
    present = [value.strip() for value in values if isinstance(value, str) and value.strip()]
    if not present:
        return None
    if any(value != present[0] for value in present[1:]):
        raise ValueError(f"Conflicting {field} identifiers for exact fetch")
    return present[0]


def build_call(request: dict, credential: str | None = None) -> tuple[str, str, dict | None, dict]:
    """Translate only declared filters; no hidden synonym or topic expansion."""
    route = request["route"]
    if route not in ROUTES:
        raise ValueError(f"Unknown discovery route: {route}")
    spec = ROUTES[route]
    if (
        request.get("provider") != spec["provider"]
        or request.get("index") != spec["index"]
        or request.get("operation") != spec["operation"]
    ):
        raise ValueError("Request provider, index and operation must match route capability")
    if request.get("resume_esearch_ref") is not None and route != "pubmed.search":
        raise ValueError("Saved ESearch continuation is only valid for PubMed search")
    window = request.get("requested_window") or {}
    if spec["adapter"] == "count_offset" and window.get("cursor") is not None:
        raise ValueError("This route needs an offset window, not a cursor")
    if spec["adapter"] == "opaque_cursor" and window.get("offset") is not None:
        raise ValueError("This route needs a cursor window, not an offset")
    limit = window.get("limit") or (1 if spec["operation"] == "fetch" else 10)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("Page limit must be between 1 and 100")
    raw_filters = request.get("filters")
    if (
        isinstance(raw_filters, dict)
        and raw_filters.get("selected_id") is not None
        and route not in {"crossref.doi", "pubmed.pmid", "pubmed.abstract"}
    ):
        raise ValueError("selected_id is only an alias for exact DOI or PMID fetch")
    filters = _validated_filters(route, raw_filters)
    query = request.get("query")
    if spec["operation"] == "search" and (not isinstance(query, str) or not query.strip()):
        raise ValueError("Search needs a nonempty caller-chosen query")
    if spec["keyed"] and not credential:
        raise ValueError("Selected route requires an explicit credential")
    if route == "europe_pmc.search":
        terms = query
        _date_filters(filters, begin="from_date", end="until_date")
        start, end = filters.get("from_date"), filters.get("until_date")
        if start or end:
            terms += f" FIRST_PDATE:[{start or '0001-01-01'} TO {end or '9999-12-31'}]"
        if filters.get("journal") is not None:
            journal = _text_filter(filters["journal"], "journal")
            if '"' in journal:
                raise ValueError("journal cannot contain quote syntax")
            terms += f' JOURNAL:"{journal}"'
        params = {
            "query": terms,
            "format": "json",
            "resultType": "core",
            "pageSize": limit,
            "cursorMark": window.get("cursor") or "*",
        }
        return (
            "GET",
            _url("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params),
            None,
            {},
        )
    if route.startswith("crossref."):
        if route.endswith("doi"):
            doi = _exact_identifier(filters, query, "doi")
            if not isinstance(doi, str) or not doi.startswith("10.") or "/" not in doi:
                raise ValueError("A DOI is required")
            return (
                "GET",
                "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""),
                None,
                {},
            )
        params = {"query.bibliographic": query, "rows": limit, "offset": window.get("offset") or 0}
        date_filters = _date_filters(filters, begin="from-pub-date", end="until-pub-date")
        if filters.get("type") is not None:
            publication_type = _text_filter(filters["type"], "type")
            if not re.fullmatch(r"[a-z][a-z0-9-]*", publication_type):
                raise ValueError("type needs a Crossref publication type token")
            date_filters["type"] = publication_type
        if date_filters:
            params["filter"] = ",".join(f"{k}:{v}" for k, v in date_filters.items())
        return "GET", _url("https://api.crossref.org/works", params), None, {}
    if route.startswith("pubmed."):
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
        key_param = {"api_key": credential} if credential else {}
        if route in {"pubmed.pmid", "pubmed.abstract"}:
            pmid = _exact_identifier(filters, query, "pmid")
            if route == "pubmed.abstract":
                if not isinstance(pmid, str) or not re.fullmatch(r"[1-9][0-9]*", pmid):
                    raise ValueError("A positive numeric PMID is required")
            elif not isinstance(pmid, str) or not pmid.isdigit():
                raise ValueError("A numeric PMID is required")
            return (
                "GET",
                _url(
                    base + ("efetch.fcgi" if route == "pubmed.abstract" else "esummary.fcgi"),
                    {"db": "pubmed", "id": pmid,
                     "retmode": "xml" if route == "pubmed.abstract" else "json", **key_param},
                ),
                None,
                {},
            )
        terms = query
        start, end = filters.get("from_date"), filters.get("until_date")
        _date_filters(filters, begin="mindate", end="maxdate")
        params = {
            "db": "pubmed",
            "term": terms,
            "retmode": "json",
            "retmax": limit,
            "retstart": window.get("offset") or 0,
            **key_param,
        }
        if start or end:
            params.update(
                {
                    "datetype": "pdat",
                    "mindate": (start or "0001-01-01").replace("-", "/"),
                    "maxdate": (end or "9999-12-31").replace("-", "/"),
                }
            )
        return "GET", _url(base + "esearch.fcgi", params), None, {}
    if route == "tavily.search":
        return "POST", "https://api.tavily.com/search", {
            "query": query, "max_results": min(limit, 10), "search_depth": "basic",
            "include_answer": False, "include_raw_content": False, "include_usage": True,
        }, {"Authorization": f"Bearer {credential or ''}", "Content-Type": "application/json"}
    if route == "searxng.search":
        # Resolved to an operator-configured dedicated endpoint by WebSearch's
        # transport. Model input cannot choose an arbitrary service endpoint.
        return "GET", _url("https://osra-searxng.invalid/search", {"q": query, "format": "json"}), None, {}
    headers = {"x-api-key": credential or "", "Content-Type": "application/json"}
    if route == "exa.search":
        if window.get("offset") or window.get("cursor"):
            raise ValueError("Exa Search does not expose an offset/cursor in this route")
        mode = filters.get("mode", "auto")
        if mode not in ROUTES["exa.search"]["modes"]:
            raise ValueError("Exa Search mode is not supported by this discovery route")
        body = {"query": query, "type": mode, "numResults": limit}
        if filters.get("highlights") is True:
            body["contents"] = {"highlights": True}
        _date_filters(filters, begin="startPublishedDate", end="endPublishedDate")
        # Exa's timestamp bounds are exclusive. Date-only filters denote whole
        # UTC calendar days, so bracket them with the adjacent representable
        # instants instead of excluding midnight or the entire final day.
        if start := filters.get("from_date"):
            try:
                previous = date.fromisoformat(start) - timedelta(days=1)
            except OverflowError as exc:
                raise ValueError("Exa from_date cannot precede the earliest date") from exc
            body["startPublishedDate"] = previous.isoformat() + "T23:59:59.999999Z"
        if end := filters.get("until_date"):
            try:
                following = date.fromisoformat(end) + timedelta(days=1)
            except OverflowError as exc:
                raise ValueError("Exa until_date cannot follow the latest date") from exc
            body["endPublishedDate"] = following.isoformat() + "T00:00:00.000000Z"
        return "POST", "https://api.exa.ai/search", body, headers
    if bool(filters.get("id")) == bool(filters.get("url")):
        raise ValueError("Exa Contents needs exactly one selected hit ID or URL")
    identity = (
        _text_filter(filters["id"], "Exa Contents ID", max_length=2048)
        if filters.get("id")
        else _selected_public_url(filters.get("url"))
    )
    if "selection_reason" in filters:
        _text_filter(filters["selection_reason"], "selection_reason", max_length=500)
    max_characters = filters.get("max_characters", 5000)
    if type(max_characters) is not int or not 1 <= max_characters <= 8 * 1024 * 1024:
        raise ValueError("max_characters must be a positive bounded integer")
    body = {
        "ids" if filters.get("id") else "urls": [identity],
        "text": {"maxCharacters": max_characters},
    }
    cache_age = filters.get("max_age_hours")
    if cache_age is not None:
        if type(cache_age) is not int or not -1 <= cache_age <= 720:
            raise ValueError("max_age_hours must be an integer from -1 to 720")
        body["maxAgeHours"] = cache_age
    return "POST", "https://api.exa.ai/contents", body, headers


class _AbstractText(HTMLParser):
    """Remove abstract markup while retaining literal comparisons and text boundaries."""

    _BLOCKS = frozenset({
        "abstract", "sec", "title", "p", "div", "br", "li", "tr",
        "h1", "h2", "h3", "h4", "h5", "h6",
    })
    _INLINE = frozenset({
        "a", "span", "b", "i", "u", "s", "em", "strong", "sup", "sub",
        "italic", "bold", "underline", "sc", "monospace", "xref", "ext-link",
    })

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.line_offsets = [0]
        self.line_offsets.extend(match.end() for match in re.finditer("\n", source))
        self.parts: list[str] = []
        self.open_tags: list[tuple[str, bool]] = []

    def _raw_tag(self) -> str:
        line, column = self.getpos()
        start = self.line_offsets[line - 1] + column
        return self.source[start:self.source.index(">", start) + 1]

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        name = tag.removeprefix("jats:").removeprefix("html:")
        # Abstract formatting does not require boolean attributes. Keeping a
        # tag-like expression with them preserves comparisons such as <B and C>.
        markup = name in self._BLOCKS | self._INLINE and all(value is not None for _, value in attrs)
        if not markup:
            self.parts.append(self.get_starttag_text())
        elif name in self._BLOCKS:
            self.parts.append("\n")
        return markup

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        markup = self._start(tag, attrs)
        if tag.removeprefix("jats:").removeprefix("html:") != "br":
            self.open_tags.append((tag, markup))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        name = tag.removeprefix("jats:").removeprefix("html:")
        markup = name in self._BLOCKS | self._INLINE
        for index in range(len(self.open_tags) - 1, -1, -1):
            if self.open_tags[index][0] == tag:
                _, markup = self.open_tags.pop(index)
                break
        if not markup:
            self.parts.append(self._raw_tag())
        elif name in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_markup(value: str) -> str:
    parser = _AbstractText(value)
    parser.feed(value)
    parser.close()
    return re.sub(r"\n[ \t\r\n]*\n", "\n", "".join(parser.parts)).strip()


_EXPLICIT_ABSTRACT_PROHIBITION = re.compile(
    r"\b(?:no\s+(?:text\s+(?:and\s+)?data\s+mining|automated\s+analysis|"
    r"ai\s+model\s+input)|(?:text\s+(?:and\s+)?data\s+mining|"
    r"automated\s+analysis|ai\s+model\s+input)\s+(?:is\s+)?prohibited)\b",
    re.I,
)


def _explicit_abstract_prohibition(raw: dict) -> bool:
    if raw.get("rights_status") in {"restricted", "not_retained"}:
        return True
    statement = raw.get("copyrightStatement") or raw.get("copyright")
    return isinstance(statement, str) and bool(_EXPLICIT_ABSTRACT_PROHIBITION.search(statement))


def _abstract_research_rights(raw: dict, abstract: object) -> str:
    # A trusted, explicit item restriction wins over the public-API default.
    # This is an adapter safeguard, not a claim that either API emits this key.
    if _explicit_abstract_prohibition(raw):
        return "restricted"
    return "permitted" if isinstance(abstract, str) and abstract.strip() else "unknown"


_PUBMED_TRUNCATION = re.compile(
    r"\bABSTRACT TRUNCATED(?: AT (?:250|400) WORDS)?\s*[)\]]?\.?\s*$", re.I
)


def _xml_text(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    text = " ".join("".join(node.itertext()).split())
    return text or None


def parse_pubmed_abstract_xml(content: bytes, selected_id: str) -> dict:
    """Project one exact EFetch citation; never treat a sibling as the selected PMID."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("malformed_pubmed_xml") from exc
    if root.tag != "PubmedArticleSet":
        raise ValueError("malformed_pubmed_xml")
    articles = list(root)
    if not articles:
        return {"article": None}
    if len(articles) != 1 or articles[0].tag != "PubmedArticle":
        raise ValueError("unexpected_pubmed_record")
    citation = articles[0].find("MedlineCitation")
    pmid = _xml_text(citation.find("PMID")) if citation is not None else None
    if pmid != selected_id:
        raise ValueError("pubmed_identifier_mismatch")
    article = citation.find("Article") if citation is not None else None
    if article is None:
        raise ValueError("malformed_pubmed_xml")
    abstract = article.find("Abstract")
    sections = []
    if abstract is not None:
        for section in abstract.findall("AbstractText"):
            section_text = _xml_text(section)
            if not section_text:
                continue
            label = section.get("Label")
            sections.append(f"{label.strip()}: {section_text}" if label and label.strip() else section_text)
    abstract_text = "\n\n".join(sections) or None
    ids: dict[str, set[str]] = {"doi": set(), "pmcid": set()}
    for identifier in articles[0].findall("./PubmedData/ArticleIdList/ArticleId"):
        kind = identifier.get("IdType")
        value = _xml_text(identifier)
        if kind == "pubmed" and value and value != selected_id:
            raise ValueError("pubmed_identifier_mismatch")
        if kind in {"doi", "pmc"} and value:
            ids["pmcid" if kind == "pmc" else "doi"].add(value)
    resolved = {key: next(iter(values)) for key, values in ids.items() if len(values) == 1}
    conflicts = [key for key, values in ids.items() if len(values) > 1]
    publication_date = article.find("./Journal/JournalIssue/PubDate")
    electronic_date = article.find("./ArticleDate[@DateType='Electronic']")
    electronic_publication_date = (
        " ".join(
            value for name in ("Year", "Month", "Day")
            if (value := _xml_text(electronic_date.find(name)))
        ) if electronic_date is not None else None
    )
    return {
        "article": {
            "pmid": pmid,
            "title": _xml_text(article.find("ArticleTitle")),
            "journal": _xml_text(article.find("./Journal/Title")),
            "publication_date": " ".join(
                part.strip() for part in publication_date.itertext() if part.strip()
            ) if publication_date is not None else None,
            "electronic_publication_date": electronic_publication_date or None,
            "abstract": abstract_text,
            "abstract_truncated": bool(abstract_text and _PUBMED_TRUNCATION.search(abstract_text)),
            "copyrightStatement": _xml_text(abstract.find("CopyrightInformation"))
            if abstract is not None else None,
            **resolved,
        },
        "identifier_conflicts": conflicts,
    }


def source_use_notices(route: str, payload: dict) -> dict[str, dict]:
    """Preserve supplied rights clues without treating a license URL as a grant."""
    if route == "pubmed.abstract":
        raw = payload.get("article")
        if not isinstance(raw, dict):
            return {}
        notice = {
            "copyright_statement": raw.get("copyrightStatement"),
            "license_urls": [],
            "full_text_oa_reported": None,
            "explicit_prohibition_detected": _explicit_abstract_prohibition(raw),
            "abstract_truncated": raw.get("abstract_truncated") is True,
        }
        return {raw["pmid"]: notice} if raw.get("pmid") and any(
            value not in (None, [], False) for value in notice.values()
        ) else {}
    if route == "europe_pmc.search":
        raw_items = payload.get("resultList", {}).get("result", [])
    elif route == "crossref.search":
        raw_items = payload.get("message", {}).get("items", [])
    elif route == "crossref.doi":
        raw_items = [payload.get("message", {})]
    else:
        return {}
    if not isinstance(raw_items, list):
        return {}
    notices = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        identity = (
            f"{raw.get('source')}:{raw.get('id')}"
            if route == "europe_pmc.search" and raw.get("source") and raw.get("id")
            else raw.get("DOI")
        )
        if not isinstance(identity, str) or not identity:
            continue
        statement = raw.get("copyrightStatement") or raw.get("copyright")
        licenses = raw.get("license")
        urls = (
            [
                item["URL"]
                for item in licenses
                if isinstance(item, dict) and isinstance(item.get("URL"), str)
            ]
            if isinstance(licenses, list)
            else []
        )
        notice = {
            "copyright_statement": statement[:2000] if isinstance(statement, str) else None,
            "license_urls": urls[:10],
            "full_text_oa_reported": raw.get("isOpenAccess")
            if raw.get("isOpenAccess") in {"Y", "N"}
            else None,
            "explicit_prohibition_detected": _explicit_abstract_prohibition(raw),
        }
        if any(value not in (None, [], False) for value in notice.values()):
            notices[identity] = notice
    return notices


def _crossref_item(raw: dict) -> dict:
    doi = raw.get("DOI")
    title = raw.get("title") or []
    container = raw.get("container-title") or []
    dates = raw.get("published", {}).get("date-parts", [[]])[0]
    metadata = {
        "doi": doi,
        "journal": container[0] if container else None,
        "publication_date": "-".join(str(x).zfill(2) if i else str(x) for i, x in enumerate(dates))
        if dates
        else None,
        "publication_type": raw.get("type"),
    }
    metadata = {k: v for k, v in metadata.items() if v is not None}
    abstract = raw.get("abstract")
    return {
        "id": doi,
        "url": raw.get("URL") or ("https://doi.org/" + doi if doi else None),
        "title": title[0] if title else None,
        "content": _strip_markup(abstract) if isinstance(abstract, str) else None,
        "surface": "abstract" if abstract else "metadata",
        "origin": "original_source",
        "rights_status": _abstract_research_rights(raw, abstract),
        "access_status": "available",
        "scientific_metadata": metadata,
    }


def _epmc_item(raw: dict) -> dict:
    source, source_id = raw.get("source"), raw.get("id")
    pmid, pmcid, doi = raw.get("pmid"), raw.get("pmcid"), raw.get("doi")
    abstract = raw.get("abstractText")
    metadata = {
        "pmid": pmid,
        "pmcid": pmcid,
        "doi": doi,
        "journal": raw.get("journalTitle"),
        "publication_date": raw.get("firstPublicationDate"),
        "publication_type": raw.get("pubType"),
    }
    return {
        "id": f"{source}:{source_id}" if source and source_id else source_id,
        "url": ("https://europepmc.org/article/" + str(source) + "/" + str(source_id))
        if source and source_id
        else None,
        "title": raw.get("title"),
        "content": _strip_markup(abstract) if isinstance(abstract, str) else None,
        "surface": "abstract" if abstract else "metadata",
        "origin": "original_source",
        "rights_status": _abstract_research_rights(raw, abstract),
        "access_status": "available",
        "scientific_metadata": {k: v for k, v in metadata.items() if v},
    }


def _pubmed_item(raw: dict) -> dict:
    uid = raw.get("uid")
    identifiers = {
        entry.get("idtype"): entry.get("value")
        for entry in raw.get("articleids", [])
        if isinstance(entry, dict)
    }
    metadata = {
        "pmid": uid,
        "doi": identifiers.get("doi"),
        "pmcid": identifiers.get("pmc"),
        "journal": raw.get("fulljournalname"),
        "publication_date": raw.get("pubdate"),
        "publication_date_basis": "pubmed.esummary.pubdate" if raw.get("pubdate") else None,
        "electronic_publication_date": raw.get("epubdate"),
    }
    return {
        "id": uid,
        "url": "https://pubmed.ncbi.nlm.nih.gov/" + str(uid) + "/" if uid else None,
        "title": raw.get("title"),
        "surface": "metadata",
        "origin": "original_source",
        "rights_status": "unknown",
        "access_status": "available",
        "scientific_metadata": {k: v for k, v in metadata.items() if v},
    }


def _pubmed_abstract_item(raw: dict) -> dict:
    abstract = raw.get("abstract")
    metadata = {
        "pmid": raw["pmid"],
        "doi": raw.get("doi"),
        "pmcid": raw.get("pmcid"),
        "journal": raw.get("journal"),
        "publication_date": raw.get("publication_date"),
        "publication_date_basis": (
            "pubmed.efetch.Article.Journal.JournalIssue.PubDate"
            if raw.get("publication_date") else None
        ),
        "electronic_publication_date": raw.get("electronic_publication_date"),
    }
    return {
        "id": raw["pmid"],
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{raw['pmid']}/",
        "title": raw.get("title"),
        "content": abstract,
        "surface": "abstract" if abstract else "metadata",
        "origin": "original_source",
        "rights_status": _abstract_research_rights(raw, abstract),
        "access_status": "limited" if raw.get("abstract_truncated") else "available",
        "scientific_metadata": {key: value for key, value in metadata.items() if value},
    }


def _exa_item(raw: dict, *, contents: bool) -> dict:
    text = raw.get("text") if contents else None
    summary = raw.get("summary") if contents else None
    if (text is not None and not isinstance(text, str)) or (
        summary is not None and not isinstance(summary, str)
    ):
        raise ValueError("Malformed Exa text or summary")
    highlights = raw.get("highlights") if not contents else None
    excerpt = (
        "\n".join(highlights)
        if isinstance(highlights, list) and all(isinstance(x, str) for x in highlights)
        else None
    )
    selected = text if text else summary if summary else None
    surface = "passage" if text else "provider_summary" if summary else "teaser"
    restricted = (raw.get("rights_status") in {"restricted", "not_retained"}
                  or raw.get("explicit_prohibition_detected") is True)
    return {
        "id": raw.get("id"),
        "url": raw.get("url"),
        "title": raw.get("title"),
        "content": selected,
        "discovery_excerpt": excerpt if not contents and not restricted else None,
        "surface": surface,
        "origin": "provider_generated" if surface == "provider_summary" else "original_source",
        "rights_status": "restricted" if restricted else "unknown",
        "access_status": "available",
        "scientific_metadata": {"publication_date": raw["publishedDate"]}
        if raw.get("publishedDate")
        else {},
    }


def _map_items(raw: object, mapper) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("Provider item collection must be a list")
    output = []
    for item in raw:
        try:
            output.append(mapper(item) if isinstance(item, dict) else {"error": "invalid_item"})
        except (TypeError, AttributeError, IndexError, KeyError, ValueError):
            output.append({"error": "invalid_item"})
    return output


def map_response(
    route: str, payload: dict, *, offset: int = 0, cursor: str | None = None,
    selected_ids: list[str] | None = None,
) -> dict:
    """Translate provider JSON into the preexisting normalized receipt shape."""
    if route == "europe_pmc.search":
        raw = payload.get("resultList", {}).get("result", [])
        next_cursor = payload.get("nextCursorMark")
        continuation = (
            next_cursor
            if isinstance(next_cursor, str)
            and next_cursor
            and next_cursor != (cursor or "*")
            and raw
            else None
        )
        total = payload.get("hitCount")
        exhausted_without_cursor = not raw or (
            (cursor is None or cursor == "*")
            and type(total) is int
            and isinstance(raw, list)
            and total <= len(raw)
        )
        return {
            "status": "ok",
            "results": _map_items(raw, _epmc_item),
            "total": total,
            "cursor": cursor or "*",
            "next_cursor": continuation,
            "has_more": True if continuation else False if exhausted_without_cursor else None,
        }
    if route.startswith("crossref."):
        msg = payload.get("message", {})
        raw = msg.get("items", []) if route.endswith("search") else [msg]
        return {
            "status": "ok",
            "items": _map_items(raw, _crossref_item),
            "count": msg.get("total-results") if route.endswith("search") else len(raw),
            "offset": offset,
        }
    if route == "pubmed.abstract":
        raw = payload.get("article")
        return {
            "status": "ok",
            "items": [_pubmed_abstract_item(raw)] if isinstance(raw, dict) else [],
            "count": 1 if isinstance(raw, dict) else 0,
            "offset": 0,
        }
    if route.startswith("pubmed."):
        result = payload.get("result", {})
        if not isinstance(result, dict) or not isinstance(result.get("uids"), list):
            raise ValueError("Malformed PubMed summary identifiers")
        uids = result["uids"]
        if selected_ids is not None:
            if (any(not isinstance(uid, str) or uid not in selected_ids for uid in uids)
                    or len(set(uids)) != len(uids)):
                raise ValueError("PubMed summary identifiers disagree with ESearch")
            raw = [
                result.get(uid) if uid in uids and isinstance(result.get(uid), dict)
                and str(result[uid].get("uid")) == uid else None
                for uid in selected_ids
            ]
        else:
            raw = [result[x] for x in uids if isinstance(result.get(x), dict)]
        return {
            "status": "ok",
            "items": _map_items(raw, _pubmed_item),
            "count": payload.get("_search_count", len(raw)),
            "offset": offset,
            "max_visible": 10000 if route == "pubmed.search" else None,
        }
    if route in {"tavily.search", "searxng.search"}:
        return {
            "status": "ok", "results": _map_items(payload.get("results", []), lambda x: {
                "id": x.get("url"), "url": x.get("url"), "title": x.get("title"),
                "discovery_excerpt": x.get("content") if (
                    x.get("rights_status") not in {"restricted", "not_retained"}
                    and x.get("explicit_prohibition_detected") is not True
                ) else None,
                "surface": "teaser", "origin": "original_source",
                "rights_status": "restricted" if (
                    x.get("rights_status") in {"restricted", "not_retained"}
                    or x.get("explicit_prohibition_detected") is True
                ) else "unknown", "access_status": "available",
            }), "has_more": False, "usage": {"provider_request": 1, **(
                {"credits": payload["usage"]["credits"]} if isinstance(payload.get("usage"), dict)
                and isinstance(payload["usage"].get("credits"), (int, float)) else {})},
        }
    raw = payload.get("results", [])
    cost = payload.get("costDollars", {})
    usage = {"provider_request": 1}
    if isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
        usage["usd_estimated_provider_response"] = cost["total"]
    statuses = payload.get("statuses", []) if route == "exa.contents" else []
    if not isinstance(statuses, list):
        statuses = []
    failures = [
        {"error": "selected_contents_failed"}
        for x in statuses
        if isinstance(x, dict) and x.get("status") != "success"
    ]
    return {
        "status": "ok",
        "results": _map_items(raw, lambda x: _exa_item(x, contents=route == "exa.contents"))
        + failures,
        "total": len(raw) + len(failures) if route == "exa.contents" else None,
        "has_more": False if route == "exa.contents" else None,
        "usage": usage,
    }
