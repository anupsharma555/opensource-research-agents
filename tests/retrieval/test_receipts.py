"""The shared store retains retrieval attempts without promoting hits to studies."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.contracts.models import RetrievalEnvelope
from ora.retrieval import record_delivery
from ora.state import StateConflict, StateStore


def request(attempt: str = "try-1") -> dict:
    return {
        "mission_id": "synthetic-mission",
        "task_id": "synthetic-task",
        "request_id": "synthetic-search",
        "attempt_id": attempt,
        "agent_instance_id": "synthetic-collector",
        "route": "synthetic-route",
        "provider": "simulated-count",
        "index": "synthetic-index",
        "operation": "search",
        "query": "fictional intervention",
        "filters": {"year_from": 2020},
        "requested_at": "2026-10-08T10:00:00Z",
        "requested_window": {"offset": 0, "limit": 2},
    }


def item(*, content="Synthetic published abstract") -> dict:
    return {
        "id": "synthetic-id-1",
        "url": "https://example.test/study/1",
        "title": "Fictional study",
        "content": content,
        "surface": "abstract",
        "origin": "original_source",
        "rights_status": "permitted",
        "access_status": "available",
    }


def page(*, items=None, status="ok") -> dict:
    return {
        "status": status,
        "count": 1,
        "offset": 0,
        "items": [item()] if items is None else items,
        "usage": {"provider_request": 1, "provider_credit": 2},
    }


def save(store: StateStore, raw: object, *, delivery="delivery-1", attempt="try-1") -> dict:
    return record_delivery(
        store, request(attempt), raw, delivery_id=delivery, adapter="count_offset",
        retrieved_at="2026-10-08T10:01:00Z",
        documented_usage=[{"unit": "estimated_usd", "amount": 0.002}],
    )


def test_attempt_page_hit_and_sanitized_blob_have_exact_links(tmp_path):
    store = StateStore(tmp_path)
    result = save(store, page())
    assert result["outcome"] == "success"
    assert len(result["accepted_hit_refs"]) == 1
    assert result["reused_hit_refs"] == []
    hit_ref = result["accepted_hit_refs"][0]
    hit = store.get(**hit_ref)
    assert hit["body"]["request_id"] == "synthetic-search"
    assert hit["body"]["source"]["access_surface"] == "abstract"
    assert hit["body"]["source"]["rights_status"] == "permitted"
    assert hit["body"]["delivery_ref"] == result["delivery_ref"]
    delivery = store.get(**result["delivery_ref"])
    wire = delivery["body"]["envelope"]
    assert wire["schemaVersion"] == "1.0"
    assert wire["request"]["deliveryId"] == "delivery-1"
    assert wire["request"]["querySha256"] == hashlib.sha256(
        b"fictional intervention"
    ).hexdigest()
    assert wire["returnedWindow"]["acceptedCount"] == 1
    assert RetrievalEnvelope.model_validate(wire).model_dump(mode="json", by_alias=True) == wire
    assert delivery["body"]["envelope"]["usage"] == [
        {"unit": "provider_request", "amount": 1, "basis": "observed"},
        {"unit": "provider_credit", "amount": 2, "basis": "observed"},
        {"unit": "estimated_usd", "amount": 0.002, "basis": "documented"},
    ]
    blob = delivery["body"]["blob"]
    assert hashlib.sha256(store.read_blob(blob["sha256"])).hexdigest() == blob["sha256"]
    assert result["raw_payload_ref"]["original_retained"] is False
    assert result["raw_payload_ref"]["sanitized_sha256"] == blob["sha256"]
    assert any(link["relation"] == "derived_from" for link in store.links(hit))
    assert len(store.events("synthetic-mission")) == 1


def test_duplicate_delivery_replays_once_but_new_retry_keeps_own_usage(tmp_path):
    store = StateStore(tmp_path)
    first = save(store, page())
    assert save(store, page()) == first
    assert len(store.records("retrieval_attempt")) == 1
    assert len(store.records("retrieval_delivery")) == 1
    assert len(store.records("retrieval_hit")) == 1
    assert len(store.events("synthetic-mission")) == 1
    with pytest.raises(StateConflict, match="different inputs"):
        save(store, page(items=[item(content="Changed abstract")]))

    retried = save(store, page(), delivery="delivery-2", attempt="try-2")
    assert retried["accepted_hit_refs"] == []
    assert retried["reused_hit_refs"] == first["accepted_hit_refs"]
    assert len(store.records("retrieval_attempt")) == 2
    assert len(store.records("retrieval_delivery")) == 2
    assert len(store.records("retrieval_hit")) == 1
    assert len(store.events("synthetic-mission")) == 2
    assert store.get(**first["attempt_ref"])["body"]["usage_by_delivery"][0]["usage"][0]["basis"] == "observed"


def test_distinct_redelivery_of_one_attempt_is_visible_without_second_charge(tmp_path):
    store = StateStore(tmp_path)
    first = save(store, page())
    redelivery = save(store, page(), delivery="delivery-redelivered")
    assert redelivery["accepted_hit_refs"] == []
    assert redelivery["reused_hit_refs"] == first["accepted_hit_refs"]
    assert len(store.records("retrieval_delivery")) == 2
    assert len(store.records("retrieval_hit")) == 1
    attempt = store.get(**redelivery["attempt_ref"])
    assert attempt["version"] == 2
    assert [entry["counted_for_attempt"] for entry in attempt["body"]["usage_by_delivery"]] == [True, False]


def test_later_mapped_scientific_or_rights_upgrade_is_a_new_hit_version(tmp_path):
    store = StateStore(tmp_path)
    first = save(store, page())
    upgraded_item = {**item(), "authors": [{"name": "Ada Example"}],
                     "tables": [{"rows": [["group-a", 20, 0.25]]}]}
    upgraded = save(store, page(items=[upgraded_item]), delivery="metadata-upgrade")
    assert len(upgraded["accepted_hit_refs"]) == 1
    assert upgraded["accepted_hit_refs"] != first["accepted_hit_refs"]
    newer = store.get(**upgraded["accepted_hit_refs"][0])["body"]["source"]
    assert newer["scientific_metadata"]["authors"][0]["name"] == "Ada Example"
    assert newer["structured_content"]["tables"][0]["rows"] == [["group-a", 20, 0.25]]
    repeated = save(store, page(items=[upgraded_item]), delivery="metadata-upgrade-repeat")
    assert repeated["accepted_hit_refs"] == []
    assert repeated["reused_hit_refs"] == upgraded["accepted_hit_refs"]

    restricted = save(
        store, page(items=[{**upgraded_item, "rights_status": "restricted"}]),
        delivery="rights-change",
    )
    assert len(restricted["accepted_hit_refs"]) == 1
    restricted_source = store.get(**restricted["accepted_hit_refs"][0])["body"]["source"]
    assert restricted_source["content"] is None
    assert restricted_source["structured_content"] == {}

    rights_store = StateStore(tmp_path / "rights-first")
    blocked = save(
        rights_store, page(items=[{**upgraded_item, "rights_status": "restricted"}]),
    )
    permitted = save(
        rights_store, page(items=[upgraded_item]), delivery="later-permitted",
    )
    assert permitted["accepted_hit_refs"] != blocked["accepted_hit_refs"]
    assert rights_store.get(**permitted["accepted_hit_refs"][0])["body"]["source"]["content_retained"]


def test_structured_only_permitted_item_survives_without_prose_content(tmp_path):
    store = StateStore(tmp_path)
    table = {"columns": ["arm", "n", "rate"], "rows": [["a", 12, 0.25], ["b", 11, None]]}
    saved = save(store, page(items=[{
        **item(content=None), "surface": "passage", "tables": [table],
    }]))
    hit = store.get(**saved["accepted_hit_refs"][0])["body"]["source"]
    assert hit["content"] is None
    assert hit["structured_content"]["tables"] == [table]
    assert store.get(**saved["delivery_ref"])["body"]["envelope"]["items"][0][
        "structuredContent"
    ]["tables"] == [table]


def test_late_failed_retry_cannot_replace_accepted_hit_or_other_attempt(tmp_path):
    store = StateStore(tmp_path)
    first = save(store, page())
    failed = save(store, {"status": "error", "error": "synthetic route failure"},
                  delivery="delivery-late", attempt="try-late")
    assert failed["outcome"] == "provider_failure"
    assert failed["accepted_hit_refs"] == []
    assert store.get(**first["accepted_hit_refs"][0]) is not None
    assert store.get(**first["attempt_ref"])["body"]["outcome"] == "success"
    assert store.get(**failed["attempt_ref"])["body"]["outcome"] == "provider_failure"
    assert len(store.records("retrieval_hit")) == 1


@pytest.mark.parametrize("status,stop_reason", [
    ("error", "error"),
    ("quota_wait", "quota_wait"),
])
def test_failed_attempt_retains_valid_observed_usage_and_bad_unit_diagnostic(
    tmp_path, status, stop_reason,
):
    store = StateStore(tmp_path)
    result = save(store, {
        "status": status,
        "usage": {"provider_credit": 2, "input_tokens": 30, "invalid_unit": -1},
    })
    assert result["outcome"] == "provider_failure"
    assert result["accepted_hit_refs"] == []
    delivery = store.get(**result["delivery_ref"])
    envelope = delivery["body"]["envelope"]
    assert envelope["returnedWindow"]["providerStopReason"] == stop_reason
    assert envelope["returnedWindow"]["windowErrors"] == ["invalid_observed_usage"]
    assert envelope["usage"] == [
        {"unit": "provider_credit", "amount": 2, "basis": "observed"},
        {"unit": "input_tokens", "amount": 30, "basis": "observed"},
        {"unit": "estimated_usd", "amount": 0.002, "basis": "documented"},
    ]
    attempt = store.get(**result["attempt_ref"])
    assert attempt["body"]["outcome"] == "provider_failure"
    assert attempt["body"]["usage_by_delivery"][0]["usage"] == envelope["usage"]
    assert store.records("retrieval_hit") == []


def test_mixed_page_preserves_valid_sibling_and_redacts_unsafe_fields(tmp_path):
    store = StateStore(tmp_path)
    secret = "synthetic-secret-value"
    unsafe = page(items=[
        {**item(content=f"Reported result. Bearer {secret}"),
         "url": f"https://user:pass@example.test/study/1?api_key={secret}&topic=trial"},
        {"id": "broken", "error": f"Bearer {secret}"},
    ])
    unsafe["Authorization"] = f"Bearer {secret}"  # Unknown raw field must be dropped.
    requested = request()
    requested["query"] = f"fictional topic token={secret}"
    requested["filters"] = {"Authorization": f"Bearer {secret}"}
    raw = json.dumps(unsafe).encode()
    result = record_delivery(
        store, requested, raw, delivery_id="mixed", adapter="count_offset",
        retrieved_at="2026-10-08T10:01:00Z",
    )
    assert result["outcome"] == "partial"
    assert len(result["accepted_hit_refs"]) == len(result["item_errors"]) == 1
    assert result["raw_payload_ref"]["original_sha256"] == hashlib.sha256(raw).hexdigest()
    delivery = store.get(**result["delivery_ref"])
    assert secret not in str(delivery)
    assert delivery["body"]["envelope"]["omittedFields"] == [
        {"field": "[sensitive-field]", "reason": "sensitive"},
    ]
    assert result["raw_payload_ref"]["retained_projection"] == "mapped_scientific_fields"
    assert result["raw_payload_ref"]["original_retained"] is False
    assert delivery["body"]["envelope"]["request"]["querySha256"] == hashlib.sha256(
        requested["query"].encode()
    ).hexdigest()
    assert secret not in str(store.get(**result["accepted_hit_refs"][0]))
    assert secret not in store.read_blob(result["raw_payload_ref"]["blob"]["sha256"]).decode()
    assert "Authorization" not in store.read_blob(result["raw_payload_ref"]["blob"]["sha256"]).decode()
    assert result["raw_payload_ref"]["redacted_paths"]


def test_empty_item_failure_malformed_and_quota_each_have_a_receipt(tmp_path):
    store = StateStore(tmp_path)
    cases = [
        ({"status": "ok", "count": 0, "offset": 0, "items": []}, "empty"),
        ({"status": "ok", "count": 1, "offset": 0, "items": [{"error": "bad"}]}, "item_failure"),
        (b"{broken-json", "malformed_payload"),
        ({"status": "quota_wait"}, "provider_failure"),
    ]
    for index, (raw, expected) in enumerate(cases):
        result = save(store, raw, delivery=f"delivery-{index}", attempt=f"try-{index}")
        assert result["outcome"] == expected
        assert store.get(**result["delivery_ref"])["body"]["envelope"]["outcome"] == expected
    assert len(store.records("retrieval_delivery")) == 4
    assert store.records("retrieval_hit") == []


def test_persisted_first_twenty_keep_known_unseen_next_page_and_all_hits(tmp_path):
    store = StateStore(tmp_path)
    requested = request()
    requested["requested_window"] = {"offset": 0, "limit": 20}
    page_items = [{**item(), "id": f"synthetic-{index}", "rank": index}
                  for index in range(1, 21)]
    result = record_delivery(
        store, requested,
        {"status": "ok", "count": 21, "offset": 0, "max_visible": 100,
         "items": page_items},
        delivery_id="first-twenty", adapter="count_offset",
        retrieved_at="2026-10-08T10:01:00Z",
        continuation={"action": "continue", "reason": "One result remains available"},
    )
    assert len(result["accepted_hit_refs"]) == 20
    assert result["window"]["known_unseen_count"] == 1
    assert result["window"]["next_window"] == {"offset": 20, "limit": 20}
    wire = store.get(**result["delivery_ref"])["body"]["envelope"]
    assert wire["returnedWindow"]["moreAvailable"] == "yes"
    assert wire["returnedWindow"]["continuation"]["action"] == "continue"
    assert wire["returnedWindow"]["knownUnseenCount"] == 1
    assert wire["returnedWindow"]["nextWindow"] == {"offset": 20, "limit": 20, "cursor": None}
    assert len(store.records("retrieval_hit")) == 20
    assert RetrievalEnvelope.model_validate(wire).model_dump(mode="json", by_alias=True) == wire


def test_persisted_cursor_result_with_unknown_total_does_not_claim_exhaustion(tmp_path):
    store = StateStore(tmp_path)
    requested = request()
    requested["provider"] = "simulated-cursor"
    requested["requested_window"] = {"cursor": "page-a", "limit": 1}
    result = record_delivery(
        store, requested,
        {"status": "ok", "results": [item()], "cursor": "page-a",
         "next_cursor": "page-b", "has_more": True},
        delivery_id="cursor-unknown-total", adapter="opaque_cursor",
        retrieved_at="2026-10-08T10:01:00Z",
    )
    wire = store.get(**result["delivery_ref"])["body"]["envelope"]
    assert len(result["accepted_hit_refs"]) == 1
    assert wire["returnedWindow"]["providerTotal"] is None
    assert wire["returnedWindow"]["knownUnseenCount"] is None
    assert wire["returnedWindow"]["moreAvailable"] == "yes"
    assert wire["returnedWindow"]["nextWindow"] == {
        "cursor": "page-b", "offset": None, "limit": None,
    }
    assert RetrievalEnvelope.model_validate(wire).model_dump(mode="json", by_alias=True) == wire
