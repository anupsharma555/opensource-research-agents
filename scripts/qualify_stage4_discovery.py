"""Explicit private live qualification for Stage 4 discovery and venue navigation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from ora.retrieval.catalog import record_navigation, seed_catalog
from ora.retrieval.discovery import available_routes, run_discovery_page
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".local" / "stage4" / "discovery"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exa_key() -> str | None:
    """Read the already configured key privately; never print or persist it."""
    if os.environ.get("EXA_API_KEY"):
        return os.environ["EXA_API_KEY"]
    env = ROOT / ".env"
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        if line.startswith("EXA_API_KEY="):
            return line.partition("=")[2].strip().strip("\"'") or None
    return None


def _request(
    run_id: str,
    name: str,
    route: str,
    query: str | None,
    *,
    filters: dict | None = None,
    window: dict | None = None,
) -> dict:
    spec = available_routes()[route]
    return {
        "mission_id": "stage4-qualification-" + run_id,
        "task_id": "stage4-discovery",
        "request_id": name,
        "attempt_id": "attempt-1",
        "agent_instance_id": "qualification-operator",
        "route": route,
        "provider": spec["provider"],
        "index": spec["index"],
        "operation": spec["operation"],
        "query": query,
        "filters": filters or {},
        "requested_at": _now(),
        "requested_window": window or {"offset": 0, "limit": 3},
    }


def _probe_seed(entry: dict) -> dict:
    url = entry["links"][0]["url"]
    started = time.perf_counter()
    final_url, status, method = url, None, "HEAD"
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(
            url, method=method, headers={"User-Agent": "ORA-stage4-venue-check/0.1"}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status, final_url = response.status, response.url
                if method == "GET":
                    response.read(1024)
        except urllib.error.HTTPError as exc:
            status, final_url = exc.code, exc.url
            if status in {403, 405} and method == "HEAD":
                continue
        except (urllib.error.URLError, TimeoutError):
            status = None
        break
    return {
        "venue_id": entry["id"],
        "url": url,
        "final_url": final_url,
        "status_code": status,
        "method": method,
        "outcome": "navigable"
        if status is not None and 200 <= status < 400
        else "blocked"
        if status in {401, 403, 429}
        else "unavailable",
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "checked_at": _now(),
    }


def _checks(store: StateStore) -> list[dict]:
    with ThreadPoolExecutor(max_workers=5) as pool:
        observations = list(pool.map(_probe_seed, seed_catalog()["entries"]))
    for item in observations:
        saved = record_navigation(
            store,
            venue_id=item["venue_id"],
            url=item["url"],
            outcome=item["outcome"],
            checked_at=item["checked_at"],
            status_code=item["status_code"],
            alternate_url=item["final_url"]
            if item["final_url"] != item["url"] and item["final_url"].startswith("https://")
            else None,
        )
        item["navigation_ref"] = saved["navigation_ref"]
    return observations


def _case(
    store: StateStore,
    run_id: str,
    name: str,
    difficulty: str,
    route: str,
    query: str | None,
    *,
    filters: dict | None = None,
    window: dict | None = None,
    credential: str | None = None,
) -> dict:
    req = _request(run_id, name, route, query, filters=filters, window=window)
    started = time.perf_counter()
    try:
        result = run_discovery_page(store, req, credential=credential)
        return {
            "name": name,
            "difficulty": difficulty,
            "request": req,
            "result": result,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "basis": "live_retrieval_deterministic_processing",
        }
    except Exception as exc:  # Qualification continues independent routes.
        return {
            "name": name,
            "difficulty": difficulty,
            "request": req,
            "error_type": type(exc).__name__,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "basis": "live_attempt_unverified",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Explicitly call configured public and Exa APIs"
    )
    parser.add_argument(
        "--check-seeds", action="store_true", help="Also navigate all 25 official venue seeds"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not args.live:
        parser.error("Live qualification requires --live; offline behavior is in tests/retrieval")
    output = args.output.resolve()
    if output != DEFAULT_OUTPUT.resolve() and not str(output).startswith(
        str((ROOT / ".local").resolve()) + os.sep
    ):
        parser.error("Qualification receipts must stay in the private .local directory")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    store = StateStore(output / "state")
    key = _exa_key()
    cases = []

    def run(name, difficulty, route, query, **kwargs):
        result = _case(store, run_id, name, difficulty, route, query, **kwargs)
        cases.append(result)
        outcome = result.get("result", {}).get("outcome", result.get("error_type", "unknown"))
        print(f"{difficulty}: {name}: {outcome}", flush=True)
        return result

    # Versioned qualification probes; these do not prescribe agent search decisions.
    run(
        "v1_known_doi",
        "very_simple",
        "crossref.doi",
        None,
        filters={"doi": "10.1136/bmj.h5627"},
        window={"offset": 0, "limit": 1},
    )
    run(
        "v1_missing_doi",
        "very_simple",
        "crossref.doi",
        None,
        filters={"doi": "10.9999/ora-missing-2026"},
        window={"offset": 0, "limit": 1},
    )
    run(
        "v1_focused_europe_pmc",
        "simple",
        "europe_pmc.search",
        "computerised cognitive behavioural therapy depression",
        filters={"from_date": "2015-01-01", "until_date": "2025-12-31"},
        window={"cursor": "*", "limit": 3},
    )
    run(
        "v1_synonym_europe_pmc",
        "simple",
        "europe_pmc.search",
        "internet cognitive behavioral therapy depression",
        filters={"from_date": "2015-01-01", "until_date": "2025-12-31"},
        window={"cursor": "*", "limit": 3},
    )
    pubmed = run(
        "v1_pubmed_page_one",
        "intermediate",
        "pubmed.search",
        "digital psychotherapy depression randomized trial",
        window={"offset": 0, "limit": 2},
    )
    if pubmed.get("result", {}).get("window", {}).get("next_window"):
        run(
            "v1_pubmed_page_two",
            "intermediate",
            "pubmed.search",
            "digital psychotherapy depression randomized trial",
            window={"offset": 2, "limit": 2},
        )
    run(
        "v1_crossref_complement",
        "intermediate",
        "crossref.search",
        "digital psychotherapy depression randomized trial",
        filters={"from_date": "2015-01-01"},
        window={"offset": 0, "limit": 3},
    )
    if key:
        exa = run(
            "v1_exa_precise",
            "advanced",
            "exa.search",
            "REEACT computerised cognitive behavioural therapy depression BMJ trial",
            credential=key,
            window={"limit": 3},
        )
        hits = exa.get("result", {}).get("hits", [])
        if hits and hits[0].get("provider_item_id"):
            run(
                "v1_exa_selected_contents",
                "advanced",
                "exa.contents",
                None,
                filters={
                    "id": hits[0]["provider_item_id"],
                    "max_characters": 5000,
                    "max_age_hours": 720,
                    "selection_reason": "Known REEACT control hit; Search returned no inline text",
                },
                credential=key,
                window={"limit": 1},
            )
        run(
            "v1_exa_alternate",
            "complex",
            "exa.search",
            "psychotherapy digital health equity implementation depression anxiety trials",
            credential=key,
            window={"limit": 4},
        )
    else:
        cases.append(
            {
                "name": "exa_routes",
                "difficulty": "advanced_complex",
                "basis": "not_run",
                "reason": "EXA_API_KEY not configured",
            }
        )
    run(
        "v1_compound_europe_pmc",
        "complex",
        "europe_pmc.search",
        "(digital mental health OR internet psychotherapy) AND (depression OR anxiety) AND trial",
        filters={"from_date": "2018-01-01"},
        window={"cursor": "*", "limit": 5},
    )
    run(
        "v1_compound_crossref",
        "complex",
        "crossref.search",
        "digital mental health implementation equity psychotherapy",
        filters={"from_date": "2018-01-01"},
        window={"offset": 0, "limit": 5},
    )
    navigation = _checks(store) if args.check_seeds else []
    receipt = {
        "schema": "ora.stage4.discovery_qualification.v1",
        "run_id": run_id,
        "created_at": _now(),
        "basis": "live_provider_calls_without_model_execution",
        "case_version": 1,
        "catalog_seed_count": len(seed_catalog()["entries"]),
        "navigation": navigation,
        "cases": cases,
        "source_hashes": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "src/ora/retrieval/providers.py",
                "src/ora/retrieval/discovery.py",
                "src/ora/retrieval/catalog.py",
                "src/ora/retrieval/venue_seeds.json",
            )
        },
    }
    path = output / (run_id + ".json")
    path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    path.chmod(0o600)
    print(f"private_receipt={path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
