"""Measure small local lexical recall and lookup time on simulated source text.

Run with ``.venv/bin/python tests/evidence/measure_lookup.py``. This is a
diagnostic fixture, not a production latency guarantee or scientific benchmark.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from ora.evidence import EvidenceLibrary
from ora.state import StateStore

DOCUMENTS = (
    ("Depression response was reported at six weeks.", "depression response", True),
    ("Anxiety retention was reported at twelve weeks.", "anxiety retention", True),
    ("Trial registration names the primary outcome.", "trial registration", True),
    ("The report describes side effects after treatment.", "adverse reactions", False),
)


def measure() -> dict:
    with tempfile.TemporaryDirectory(prefix="ora-evidence-measure-") as directory:
        library = EvidenceLibrary(StateStore(Path(directory)))
        expected = []
        for index, (body, query, should_match) in enumerate(DOCUMENTS):
            publication = library.register_publication(
                f"publication-{index}", identifiers={"doi": f"10.1234/simulated-lookup-{index}"},
                title=f"Simulated lookup report {index}", verification="Synthetic measurement fixture",
            )["publication_ref"]
            encounter = library.record_encounter(
                f"encounter-{index}", publication_ref=publication, retrieval_hit_ref=None,
                status="accepted", access_surface="abstract", retrieved_at="2026-01-02T00:00:00Z",
                route="local-measurement-fixture",
            )["encounter_ref"]
            saved = library.add_copy(
                f"copy-{index}", publication, encounter, content=body.encode(), content_sha256=None,
                mime_type="text/plain", source_version="simulated-v1", access_surface="abstract",
                retrieved_at="2026-01-02T00:00:00Z", rights_basis="Synthetic project-owned fixture",
                retain_bytes=True, reference=f"simulated://lookup/{index}",
                passages=[{"span_id": "main", "locator": "Abstract, sentence 1", "text": body,
                           "start": 0, "end": len(body)}],
                inspection_state="checked", validation_record="synthetic-offset-check-v1",
            )
            expected.append((saved["passage_refs"][0]["id"], query, should_match))
        results = []
        for passage_id, query, should_match in expected:
            search = library.search_passages(query, limit=1)
            found = search["results"][0]["passage_ref"]["id"] if search["results"] else None
            started = time.perf_counter()
            exact = library.get_passage(passage_id)
            exact_ms = round((time.perf_counter() - started) * 1000, 3)
            results.append({
                "query": query, "expected_passage_id": passage_id, "top1_hit": found == passage_id,
                "expected_lexical_match": should_match, "search_ms": search["elapsed_ms"],
                "exact_lookup_ms": exact_ms, "examined": search["examined"],
                "exact_found": exact is not None,
            })
        return {
            "method": "token_overlap_v1", "simulated_source_count": len(DOCUMENTS),
            "known_source_recall_at_1": sum(item["top1_hit"] for item in results) / len(results),
            "results": results,
            "queue_time": "not measured by the synchronous library",
            "proof_boundary": "Local four-source measurement only; synonyms and clinical relevance need separate evaluation",
        }


if __name__ == "__main__":
    print(json.dumps(measure(), indent=2))
