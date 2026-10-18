"""Explicit, serial provider qualification with a durable whole-batch HTTP cap.

Without --live this command only prints the selected cases. It uses the actual
discovery adapters; no research model, alternate provider, or venue crawl runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ora.evidence.hit_use import hit_allows_reinspection
from ora.retrieval.discovery import available_routes, run_discovery_page
from ora.retrieval.validation_transport import BatchTransport, ValidationStop, _atomic_json
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
VERSION = "provider-urban-trees-v1"
QUERY = "urban tree canopy air temperature"
CASES = (
    {"id": "exa-fast", "route": "exa.search", "query": "EPA using trees and vegetation to reduce heat islands",
     "filters": {"mode": "fast", "highlights": True}, "planned_attempts": 1},
    {"id": "exa-auto", "route": "exa.search", "query": QUERY,
     "filters": {"mode": "auto", "highlights": True}, "planned_attempts": 1},
    {"id": "tavily-basic", "route": "tavily.search", "query": QUERY, "planned_attempts": 1},
    {"id": "pubmed", "route": "pubmed.search", "query": QUERY, "planned_attempts": 2},
    {"id": "crossref", "route": "crossref.search", "query": QUERY, "planned_attempts": 1},
    {"id": "europe-pmc", "route": "europe_pmc.search", "query": QUERY, "planned_attempts": 1},
    {"id": "exa-contents", "route": "exa.contents", "query": None, "planned_attempts": 1},
)
KEYS = {"exa": "EXA_API_KEY", "tavily": "TAVILY_API_KEY", "ncbi": "NCBI_API_KEY"}
TERMINAL = {"completed", "empty", "http_404", "source_unavailable", "not_exercised"}


def _timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def credentials(root: Path) -> dict:
    """Use only this checkout's selected .env; never borrow inherited project keys."""
    values = {}
    for line in (root / ".env").read_text().splitlines():
        key, separator, value = line.partition("=")
        key = key.strip().removeprefix("export ")
        if separator and key in KEYS.values():
            values[key] = value.strip().strip("\"'")
    return values


def _shape(route: str, url: str, response) -> None:
    """Do not misclassify a missing provider collection as a valid empty search."""
    if response.status >= 300:
        return
    try:
        if len(response.body) > 8 * 1024 * 1024:
            raise ValueError
        payload = json.loads(response.body)
        if not isinstance(payload, dict) or payload.get("error"):
            raise ValueError
        if route.startswith(("exa.", "tavily.")):
            valid = isinstance(payload.get("results"), list)
        elif route.startswith("crossref."):
            valid = isinstance(payload.get("message"), dict) and isinstance(payload["message"].get("items"), list)
        elif route.startswith("europe_pmc."):
            valid = isinstance(payload.get("resultList"), dict) and isinstance(payload["resultList"].get("result"), list)
        elif urlsplit(url).path.endswith("esearch.fcgi"):
            valid = isinstance(payload.get("esearchresult"), dict) and isinstance(payload["esearchresult"].get("idlist"), list)
        else:
            valid = isinstance(payload.get("result"), dict) and isinstance(payload["result"].get("uids"), list)
        if not valid:
            raise ValueError
    except (ValueError, TypeError, UnicodeDecodeError):
        raise ValidationStop("malformed_provider_response") from None


def _request(case: dict, batch_id: str, epoch: float, cases: dict, store: StateStore) -> dict | None:
    route = available_routes()[case["route"]]
    filters = dict(case.get("filters", {}))
    if case["id"] == "exa-contents":
        hits = []
        for name in ("exa-fast", "exa-auto"):
            for hit in cases.get(name, {}).get("result", {}).get("hits", []):
                record = store.get(**hit["hit_ref"])
                if (record is not None and hit_allows_reinspection(store, record)
                        and record["body"]["source"].get("rights_status") not in {"restricted", "not_retained"}
                        and hit.get("original_url", "").startswith("https://")):
                    hits.append(hit)
        if not hits:
            return None
        filters = {"url": hits[0]["original_url"], "max_characters": 2000,
                   "selection_reason": "Check extraction of one saved search candidate; no scientific claim."}
    return {
        "mission_id": "provider-validation-" + batch_id, "task_id": case["id"],
        "agent_instance_id": "provider-validation-controller",
        "request_id": case["id"], "attempt_id": "attempt-1",
        "route": case["route"], "provider": route["provider"], "index": route["index"],
        "operation": route["operation"], "query": case["query"], "filters": filters,
        "requested_at": _timestamp(epoch),
        "requested_window": {"limit": 1 if case["id"] == "exa-contents" else 3,
                             **({"cursor": "*"} if case["id"] == "europe-pmc" else {})},
    }


def run_case(store, case, budget, values, state_dir, *, clock, sleep):
    budget.set_case(case["id"])
    row = budget.data["cases"][case["id"]]
    if row.get("status") in TERMINAL:
        return
    if row.get("state_dir", str(state_dir)) != str(state_dir):
        budget.stop("case_state_directory_changed")
    row.update(route=case["route"], state_dir=str(state_dir))
    provider = available_routes()[case["route"]]["provider"]
    credential = values.get(KEYS.get(provider, ""))
    if provider in KEYS and not credential:
        budget.stop("selected_credential_missing")
    row["authentication"] = "keyed_request_not_yet_observed" if credential else "public_no_key"
    request = row.get("request") or _request(case, budget.batch_id, clock(), budget.data["cases"], store)
    if request is None:
        row.update(status="not_exercised", reason="no_eligible_saved_exa_hit")
        budget.save()
        return
    row.setdefault("retry_count", 0)
    while True:
        row.update(request=request, status="running")
        budget.save()

        def transport(method, url, **kwargs):
            response = budget(method, url, **kwargs)
            _shape(case["route"], url, response)
            return response

        result = run_discovery_page(store, request, credential=credential, transport=transport)
        row["result"] = result
        row["ended_at"] = clock()
        statuses = [leg["status"] for leg in result.get("http_legs", []) if "status" in leg]
        if credential and statuses and all(200 <= code < 300 for code in statuses):
            row["authentication"] = "keyed_http_success"
        budget.save()
        if any(code in {401, 403} for code in statuses):
            row["authentication"] = "rejected"
            budget.stop("authentication_rejected")
        if result.get("provider_payload_condition") in {"malformed", "oversized", "transport_error"} or result.get("outcome") == "uncertain_dispatch":
            budget.stop("invalid_or_uncertain_adapter_result")
        if result.get("outcome") == "quota_wait" or any(code in {429, 503} for code in statuses):
            retry_at = result.get("retry_at")
            if not retry_at or row["retry_count"] >= 1:
                row.update(status="pending", reason="rate_wait_requires_later_review")
                budget.save()
                return
            wake = datetime.fromisoformat(retry_at.replace("Z", "+00:00")).timestamp()
            wait_ref = result.get("wait_ref")
            saved_wait = store.get(**wait_ref) if isinstance(wait_ref, dict) else None
            if saved_wait and saved_wait["body"].get("route") == case["route"] and (
                saved_wait["body"].get("retry_at") == retry_at
            ):
                exact_wake = saved_wait["body"].get("until_epoch")
                if type(exact_wake) in (int, float) and math.isfinite(exact_wake):
                    wake = exact_wake
            if wake >= min(row["deadline"], budget.data["deadline"]):
                row.update(status="pending", reason="rate_wait_beyond_observation", wake_at=wake)
                budget.save()
                return
            # Wait through the saved reset, never consume another model turn.
            while clock() < wake:
                sleep(min(1.0, wake - clock()))
            if statuses:
                row["retry_count"] += 1
                request = {**request, "attempt_id": "attempt-2", "requested_at": _timestamp(clock())}
                if result.get("esearch_ref"):
                    request["resume_esearch_ref"] = result["esearch_ref"]
            budget.save()
            continue
        if any(code >= 400 and code != 404 for code in statuses):
            budget.stop("unexpected_provider_http_error")
        if not result.get("page_ref") or not result.get("delivery_ref"):
            budget.stop("missing_saved_adapter_receipt")
        content_failed = case["id"] == "exa-contents" and any(
            item.get("status") != "success" for item in result.get("provider_meta", {}).get("statuses", [])
        )
        if result.get("item_errors") and not content_failed:
            budget.stop("malformed_provider_items")
        row["status"] = ("http_404" if 404 in statuses else "source_unavailable" if content_failed
                         else "completed" if result.get("hits") else "empty")
        if row["status"] == "http_404":
            row["reason"] = "provider_returned_404_not_evidence_of_scientific_absence"
        if content_failed:
            row["reason"] = "selected_exa_contents_failed"
        row["elapsed_ms"] = round((clock() - row["started_at"]) * 1000, 3)
        budget.save()
        return


def summarize(data: dict, selected: list[dict]) -> dict:
    attempts = data["attempts"]
    rows = []
    for case in selected:
        row = data["cases"].get(case["id"], {})
        result = row.get("result", {})
        calls = [a for a in attempts if a["case_id"] == case["id"]]
        rows.append({"case": case["id"], "route": case["route"],
                     "status": row.get("status", "not_run"), "reason": row.get("reason"),
                     "authentication": row.get("authentication", "not_checked"),
                     "attempts": len(calls), "http_statuses": [a.get("status") for a in calls],
                     "returned_results": len(result["hits"]) if "hits" in result else None,
                     "elapsed_ms": row.get("elapsed_ms"),
                     "provider_usage": [a["provider_usage"] for a in calls if "provider_usage" in a] or None,
                     "page_ref": result.get("page_ref"), "delivery_ref": result.get("delivery_ref"),
                     "provider_details": result.get("provider_meta"),
                     "hits": [{key: hit.get(key) for key in
                               ("hit_id", "hit_ref", "title", "original_url", "scientific_metadata", "access_surface", "rights_status")}
                              for hit in result.get("hits", [])],
                     "source_text_checked": False})
    return {"schema": "ora.provider_validation.summary.v1", "case_version": VERSION,
            "batch_id": data["batch_id"], "execution_mode": data.get("execution_mode"),
            "max_http_attempts": data["max_attempts"], "http_attempts": len(attempts),
            "captured_responses": sum(a.get("state") == "observed" for a in attempts),
            "model_calls": 0, "model_tokens": 0, "stop_reason": data.get("stop_reason"),
            "started_at": _timestamp(data["started_at"]), "ended_at": _timestamp(data.get("observed_until", data["started_at"])),
            "cases": rows, "proof_limit": "Provider adapter checks only; no agent choice, checked passage delivery, or scientific answer."}


def run_batch(root: Path, batch_id: str, state_dir: Path, selected: list[dict], *,
              max_attempts=8, values=None, opener=None, clock=time.time, sleep=time.sleep,
              execution_mode="live") -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", batch_id):
        raise ValueError("Use a short lowercase batch ID")
    private = (root / ".local" / "search-validation").resolve()
    state_dir = state_dir.resolve()
    if not state_dir.is_relative_to(private) or state_dir == private:
        raise ValueError("State directory must be beneath .local/search-validation")
    ledger = private / "batches" / (batch_id + ".json")
    selection = {"version": VERSION, "cases": selected}
    with BatchTransport(ledger, batch_id=batch_id, max_attempts=max_attempts, clock=clock, opener=opener) as budget:
        if budget.data.get("execution_mode", execution_mode) != execution_mode:
            budget.stop("execution_mode_changed")
        if budget.data.get("selection", selection) != selection:
            budget.stop("batch_selection_changed")
        budget.data.update(selection=selection, execution_mode=execution_mode)
        if budget.data.get("state_dir", str(state_dir)) != str(state_dir):
            budget.stop("batch_state_directory_changed")
        budget.data["state_dir"] = str(state_dir)
        values = credentials(root) if values is None else values
        selected_keys = {KEYS[provider] for case in selected
                         if (provider := available_routes()[case["route"]]["provider"]) in KEYS}
        key_bindings = {key: hashlib.sha256(values.get(key, "").encode()).hexdigest() for key in selected_keys}
        if budget.data.get("credential_bindings", key_bindings) != key_bindings:
            budget.stop("batch_credentials_changed")
        budget.data["credential_bindings"] = key_bindings
        source_hashes = {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [ROOT / "scripts/validate_search.py", *sorted((ROOT / "src/ora").rglob("*.py"))]
        }
        if budget.data.get("source_sha256", source_hashes) != source_hashes:
            budget.stop("batch_source_changed")
        budget.data["source_sha256"] = source_hashes
        budget.save()
        store = StateStore(state_dir)
        try:
            for case in selected:
                if clock() >= budget.data["deadline"]:
                    budget.stop("batch_deadline")
                try:
                    run_case(store, case, budget, values, state_dir, clock=clock, sleep=sleep)
                except ValidationStop as exc:
                    row = budget.data["cases"].setdefault(case["id"], {})
                    row.update(status="stopped", reason=exc.reason, ended_at=clock())
                    if not budget.data.get("stop_reason"):
                        budget.data["stop_reason"] = exc.reason
                    budget.save()
                    break
                if budget.data["cases"][case["id"]].get("status") == "pending":
                    break
        except Exception as exc:
            budget.data["stop_reason"] = getattr(exc, "reason", "runner_error_" + type(exc).__name__)
            budget.save()
        budget.data["observed_until"] = clock()
        budget.save()
        summary = summarize(budget.data, selected)
        _atomic_json(private / "reports" / (batch_id + ".json"), summary)
        return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--batch-id")
    parser.add_argument("--case", action="append", choices=[case["id"] for case in CASES])
    parser.add_argument("--provider", choices=["exa", "tavily", "pubmed", "crossref", "europe_pmc"])
    parser.add_argument("--max-http-attempts", type=int, default=8)
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args(argv)
    selected = [case for case in CASES if (not args.case or case["id"] in args.case)
                and (not args.provider or available_routes()[case["route"]]["provider"] ==
                     ("ncbi" if args.provider == "pubmed" else args.provider))]
    if not selected or not 1 <= args.max_http_attempts <= 8:
        parser.error("Select at least one case and an HTTP ceiling from one to eight")
    if not args.live:
        print(json.dumps({"live": False, "model_calls": 0, "max_http_attempts": args.max_http_attempts,
                          "planned_http_attempts": sum(case["planned_attempts"] for case in selected),
                          "cases": selected}, indent=2))
        return 0
    if not args.batch_id:
        parser.error("Explicit live execution requires a persistent --batch-id")
    state = args.state_dir or ROOT / ".local/search-validation" / args.batch_id / "state"
    try:
        result = run_batch(ROOT, args.batch_id, state, selected, max_attempts=args.max_http_attempts)
    except ValidationStop as exc:
        print(json.dumps({"status": "stopped", "reason": exc.reason}))
        return 1
    print(json.dumps({"batch_id": result["batch_id"], "http_attempts": result["http_attempts"],
                      "model_calls": 0, "stop_reason": result["stop_reason"],
                      "cases": [{key: row[key] for key in ("case", "status", "attempts", "returned_results")}
                                for row in result["cases"]]}, indent=2))
    return 1 if result["stop_reason"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
