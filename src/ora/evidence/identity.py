"""Conservative identifiers for publications, registries, and datasets.

Normalization removes resolver syntax and harmless case differences. It never
uses a title to assert that two works or two participant groups are the same.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse


class IdentityError(ValueError):
    """An asserted verified identifier is malformed or unsupported."""


KINDS = frozenset({"doi", "pmid", "pmcid", "arxiv", "registry", "dataset_doi"})
_DOI = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)
_ARXIV_NEW = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?", re.IGNORECASE)
_ARXIV_OLD = re.compile(r"[a-z][a-z.\-]+/\d{7}(?:v\d+)?", re.IGNORECASE)


def normalize_identifier(kind: str, value: str) -> str:
    """Return a conservative canonical value, or reject uncertain syntax."""
    if kind not in KINDS or not isinstance(value, str) or not value.strip():
        raise IdentityError(f"Unsupported or empty identifier: {kind}")
    raw = value.strip()
    parsed = urlparse(raw)
    if raw.lower().startswith(("http://", "https://")):
        host = (parsed.hostname or "").lower()
        if kind in {"doi", "dataset_doi"} and host in {"doi.org", "dx.doi.org"}:
            raw = unquote(parsed.path.lstrip("/"))
        elif kind == "pmid" and host in {"pubmed.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}:
            raw = parsed.path.strip("/").removeprefix("pubmed/")
        elif kind == "pmcid" and host in {"pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}:
            raw = parsed.path.strip("/").removeprefix("articles/")
        elif kind == "arxiv" and host in {"arxiv.org", "www.arxiv.org"}:
            raw = parsed.path.strip("/").removeprefix("abs/")
        elif kind == "registry" and host in {"isrctn.com", "www.isrctn.com", "clinicaltrials.gov"}:
            raw = parsed.path.strip("/").removeprefix("study/")
        else:
            raise IdentityError(f"Unrecognized {kind} resolver host")
    elif "://" in raw:
        raise IdentityError(f"Unrecognized {kind} resolver scheme")

    if kind in {"doi", "dataset_doi"}:
        raw = re.sub(r"^doi:\s*", "", raw, flags=re.IGNORECASE).lower()
        if not _DOI.fullmatch(raw):
            raise IdentityError("DOI must include a 10.<registrant>/<suffix> value")
        return raw
    if kind == "pmid":
        raw = re.sub(r"^pmid:\s*", "", raw, flags=re.IGNORECASE)
        if not re.fullmatch(r"[1-9]\d*", raw):
            raise IdentityError("PMID must be positive decimal digits")
        return raw
    if kind == "pmcid":
        raw = re.sub(r"^pmcid:\s*", "", raw, flags=re.IGNORECASE).upper()
        if not re.fullmatch(r"PMC[1-9]\d*", raw):
            raise IdentityError("PMCID must be PMC followed by positive decimal digits")
        return raw
    if kind == "arxiv":
        raw = re.sub(r"^arxiv:\s*", "", raw, flags=re.IGNORECASE)
        if not (_ARXIV_NEW.fullmatch(raw) or _ARXIV_OLD.fullmatch(raw)):
            raise IdentityError("Unrecognized arXiv identifier")
        return re.sub(r"v\d+$", "", raw, flags=re.IGNORECASE).lower()
    raw = raw.upper()
    if not (
        re.fullmatch(r"ISRCTN\d{8}", raw)
        or re.fullmatch(r"NCT\d{8}", raw)
        or re.fullmatch(r"ACTRN\d{14}", raw)
    ):
        raise IdentityError("Registry ID must be an ISRCTN, NCT, or ACTRN identifier")
    return raw


def normalize_identifiers(identifiers: dict[str, str]) -> dict[str, str]:
    if not isinstance(identifiers, dict):
        raise IdentityError("Identifiers must be a mapping")
    return {kind: normalize_identifier(kind, value) for kind, value in identifiers.items()}
