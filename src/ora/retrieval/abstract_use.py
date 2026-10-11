"""Narrow private-research use for published abstracts returned by public APIs.

This is an application use decision, not a copyright-license assertion. It
never applies to full text, Exa extraction or material an actual source policy
has restricted. A later, stricter source decision remains authoritative.
"""

from __future__ import annotations

from typing import Any

PUBLIC_ABSTRACT_SOURCES = {
    "europe_pmc.search": {
        "abstract_field": "abstractText",
        "basis_url": "https://europepmc.org/RestfulWebService",
    },
    "crossref.search": {
        "abstract_field": "abstract",
        "basis_url": "https://support.crossref.org/hc/en-us/articles/214320426-REST-API",
    },
    "crossref.doi": {
        "abstract_field": "abstract",
        "basis_url": "https://support.crossref.org/hc/en-us/articles/214320426-REST-API",
    },
    "pubmed.abstract": {
        "abstract_field": "PubmedArticle/MedlineCitation/Article/Abstract/AbstractText",
        "basis_url": "https://www.ncbi.nlm.nih.gov/books/NBK25499/",
    },
}
ABSTRACT_USE_POLICY_VERSION = "ora.retrieval.abstract_use.v1"


def published_abstract_use(
    *,
    route: str,
    source: dict[str, Any],
    retrieved_at: str,
    provider_response_sha256: str | None,
    source_notice: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Describe exact private use while preserving unknown copyright/license status."""
    if route not in PUBLIC_ABSTRACT_SOURCES or source.get("access_surface") != "abstract":
        return None
    status = source.get("rights_status")
    allowed = (
        ["discovery", "private_research_storage", "model_input", "factual_synthesis"]
        if status == "permitted" and source.get("content_retained")
        else ["discovery"]
    )
    return {
        "schema": ABSTRACT_USE_POLICY_VERSION,
        "decision": "private_research_use" if len(allowed) > 1 else "no_text_use",
        "rights_status_for_this_use": status,
        "basis": "published_abstract_returned_by_public_api",
        "basis_url": PUBLIC_ABSTRACT_SOURCES[route]["basis_url"],
        "source_route": route,
        "source_url": source.get("original_url"),
        "abstract_field": PUBLIC_ABSTRACT_SOURCES[route]["abstract_field"],
        "abstract_completeness": "reported_truncated" if (
            source_notice and source_notice.get("abstract_truncated")
        ) else "not_reported_truncated",
        "retrieved_at": retrieved_at,
        "abstract_sha256": source.get("content_sha256"),
        "provider_response_sha256": provider_response_sha256,
        "source_notice": source_notice,
        "allowed_uses": allowed,
        "copyright_license_status": "unverified",
        "full_text_access": "not_inferred",
        "redistribution": "not_authorized_by_this_decision",
        "source_restriction": "caller_or_source_restriction_takes_precedence",
    }


__all__ = ["ABSTRACT_USE_POLICY_VERSION", "PUBLIC_ABSTRACT_SOURCES", "published_abstract_use"]
