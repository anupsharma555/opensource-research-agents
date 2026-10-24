"""A shared public download never shares a peer's source-use grant."""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ora.coordination.board import ref
from ora.evidence import EvidenceLibrary, IntakeService
from ora.evidence.shared_fetch import SharedPublicFetch, extraction_key
from ora.state import StateStore

URL = "https://example.org/article?id=1"
CONTENT = b"<html><body><p>Synthetic shared public result.</p></body></html>"


def _leads(store, url=URL):
    def save(tx):
        leads = []
        for suffix in ("one", "two"):
            tx.put("agent_instance", f"peer-{suffix}", {"run_id": "run"})
            delivery = tx.put("retrieval_delivery", f"delivery-{suffix}", {})
            hit = tx.put("retrieval_hit", f"hit-{suffix}", {
                "route": "exa.search", "mission_id": "mission", "task_id": f"task-{suffix}",
                "agent_instance_id": f"peer-{suffix}", "delivery_ref": ref(delivery),
                "source": {"origin": "original_source", "original_url": url,
                           "rights_status": "unknown", "content": None},
            })
            tx.link(hit, delivery, "derived_from")
            leads.append((ref(hit), ref(delivery)))
        return {"leads": leads}

    return store.transact("synthetic-shared-leads", {}, save)["leads"]


def _receipt(url: str, content: bytes = CONTENT):
    return {"status": 200, "content": content,
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "byte_length": len(content), "content_type": "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": "2026-10-22T12:00:00Z"}


def test_two_peers_reuse_one_download_and_extraction_but_get_distinct_grants(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    leads = _leads(store)
    counts = {"fetch": 0, "extract": 0}
    from ora.evidence import shared_fetch as module

    original_extract = module.extract_blocks

    def fetch(url):
        counts["fetch"] += 1
        return _receipt(url)

    def extract(*args, **kwargs):
        counts["extract"] += 1
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(module, "extract_blocks", extract)
    shared = SharedPublicFetch(store, fetcher=fetch)
    first = shared.fetch(mission_id="mission", operation_id="fetch-one", url=URL)
    second = shared.fetch(mission_id="mission", operation_id="fetch-two", url=URL)
    assert first["status"] == second["status"] == "completed"
    assert counts == {"fetch": 1, "extract": 1}
    assert first["shared_fetch_ref"] == second["shared_fetch_ref"]
    intake = IntakeService(EvidenceLibrary(store))
    with pytest.raises(ValueError, match="public access basis"):
        shared.adopt(
            intake, mission_id="another-mission", run_id="run", actor_id="peer-one",
            task_id="task-one", operation_id="wrong-mission", hit_ref=leads[0][0],
            response_ref=leads[0][1], shared_fetch_ref=first["shared_fetch_ref"],
            shared_extraction_ref=first["shared_extraction_ref"],
        )
    copies = []
    for suffix, lead in zip(("one", "two"), leads):
        document = shared.adopt(
            intake, mission_id="mission", run_id="run", actor_id=f"peer-{suffix}",
            task_id=f"task-{suffix}", operation_id=f"adopt-{suffix}",
            hit_ref=lead[0], response_ref=lead[1],
            shared_fetch_ref=first["shared_fetch_ref"],
            shared_extraction_ref=first["shared_extraction_ref"],
        )
        inspected = intake.inspect_public_document(
            f"inspect-{suffix}", document, mission_id="mission", run_id="run",
            actor_id=f"peer-{suffix}", task_id=f"task-{suffix}",
        )
        assert inspected["model_input_status"] == "permitted"
        copies.append(inspected["copy_ref"])
    assert copies[0] != copies[1]
    assert counts == {"fetch": 1, "extract": 1}
    first_use = store.resolve("source_use.copy", f"{copies[0]['id']}:{copies[0]['version']}")

    def revoke(tx):
        old = tx.get("source_use", first_use["id"])
        return ref(tx.put("source_use", old["id"], {
            **old["body"], "policy_status": "revoked", "allowed_uses": [],
        }, expected_version=old["version"]))

    store.transact("revoke-first-peer", {}, revoke)
    from ora.evidence.hit_use import hit_allows_reinspection
    assert not hit_allows_reinspection(store, store.get(**leads[0][0]))
    assert hit_allows_reinspection(store, store.get(**leads[1][0]))


def test_inflight_peer_is_nonblocking_and_restart_is_uncertain(tmp_path):
    store = StateStore(tmp_path / "state")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fetch(url):
        calls.append(url)
        entered.set()
        assert release.wait(5)
        return _receipt(url)

    shared = SharedPublicFetch(store, fetcher=fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(shared.fetch, mission_id="mission",
                                   operation_id="fetch-one", url=URL)
        assert entered.wait(5)
        pending = shared.fetch(mission_id="mission", operation_id="fetch-two", url=URL)
        assert pending == {"status": "pending", "reason": "shared_fetch_in_flight"}
        release.set()
        first = first_future.result(timeout=5)
    assert first["status"] == "completed"
    assert shared.fetch(mission_id="mission", operation_id="fetch-two", url=URL)["status"] == "completed"
    assert calls == [URL]

    another = "https://example.org/other?id=1"
    def interrupted(_url):
        raise TimeoutError("uncertain after dispatch")

    uncertain = SharedPublicFetch(store, fetcher=interrupted)
    assert uncertain.fetch(mission_id="mission", operation_id="lost", url=another)["status"] == "uncertain"
    restarted = SharedPublicFetch(store, fetcher=fetch)
    assert restarted.fetch(mission_id="mission", operation_id="new-peer", url=another) == {
        "status": "uncertain", "reason": "unresolved_previous_dispatch",
    }
    assert calls == [URL]


def test_query_and_representation_are_distinct_and_refresh_needs_new_operation(tmp_path):
    store = StateStore(tmp_path / "state")
    calls = []

    def fetch(url):
        calls.append(url)
        return _receipt(url)

    shared = SharedPublicFetch(store, fetcher=fetch)
    one = shared.fetch(mission_id="mission", operation_id="one", url=URL)
    assert shared.fetch(mission_id="mission", operation_id="one", url=URL) == {
        **one, "reused_fetch": True, "reused_extraction": True,
    }
    shared.fetch(mission_id="mission", operation_id="query-two",
                 url="https://example.org/article?id=2")
    shared.fetch(mission_id="mission", operation_id="rendered", url=URL,
                 representation="rendered_html")
    refreshed = shared.fetch(mission_id="mission", operation_id="refresh", url=URL,
                             refresh=True)
    assert refreshed["shared_fetch_ref"] != one["shared_fetch_ref"]
    assert len(calls) == 4
    assert shared.fetch(mission_id="mission", operation_id="one", url=URL)["shared_fetch_ref"] == one["shared_fetch_ref"]


def test_checked_redirect_final_url_can_reuse_but_unrelated_url_cannot_adopt(tmp_path):
    store = StateStore(tmp_path / "state")
    final_url = "https://example.org/final?version=1"
    calls = []

    def fetch(url):
        calls.append(url)
        receipt = _receipt(url)
        receipt["final_url"] = final_url
        receipt["redirect_chain"] = [{"from_url": url, "to_url": final_url, "status": 302}]
        return receipt

    shared = SharedPublicFetch(store, fetcher=fetch)
    result = shared.fetch(mission_id="mission", operation_id="redirect", url=URL)
    reused = shared.fetch(mission_id="mission", operation_id="direct-final", url=final_url)
    assert result["shared_fetch_ref"] == reused["shared_fetch_ref"]
    assert calls == [URL]
    refreshed = shared.fetch(mission_id="mission", operation_id="refresh-redirect",
                             url=URL, refresh=True)
    assert refreshed["shared_fetch_ref"] != result["shared_fetch_ref"]
    assert shared.fetch(mission_id="mission", operation_id="direct-final-after-refresh",
                        url=final_url)["shared_fetch_ref"] == refreshed["shared_fetch_ref"]
    assert calls == [URL, URL]
    leads = _leads(store, final_url)
    adopted = shared.adopt(
        IntakeService(EvidenceLibrary(store)), mission_id="mission", run_id="run",
        actor_id="peer-one", task_id="task-one", operation_id="adopt-final",
        hit_ref=leads[0][0], response_ref=leads[0][1],
        shared_fetch_ref=refreshed["shared_fetch_ref"],
        shared_extraction_ref=refreshed["shared_extraction_ref"],
    )
    assert store.get(**adopted)["body"]["requested_url"] == final_url

    def wrong(tx):
        old = tx.get(**leads[1][0])
        changed = tx.put("retrieval_hit", old["id"], {
            **old["body"], "source": {**old["body"]["source"],
                                      "original_url": "https://example.org/unrelated"},
        }, expected_version=old["version"])
        tx.link(changed, tx.get(**leads[1][1]), "derived_from")
        return ref(changed)

    wrong_ref = store.transact("wrong-lead", {}, wrong)
    with pytest.raises(ValueError, match="Shared fetch URL differs"):
        shared.adopt(
            IntakeService(EvidenceLibrary(store)), mission_id="mission", run_id="run",
            actor_id="peer-two", task_id="task-two", operation_id="adopt-wrong",
            hit_ref=wrong_ref, response_ref=leads[1][1],
            shared_fetch_ref=result["shared_fetch_ref"],
            shared_extraction_ref=result["shared_extraction_ref"],
        )


def test_redirect_refresh_never_overwrites_independent_final_url(tmp_path):
    store = StateStore(tmp_path / "state")
    final_url = "https://example.org/independent-final"
    calls = []

    def fetch(url):
        calls.append(url)
        receipt = _receipt(url)
        if url == URL:
            receipt["final_url"] = final_url
            receipt["redirect_chain"] = [{"from_url": URL, "to_url": final_url,
                                          "status": 302}]
        return receipt

    shared = SharedPublicFetch(store, fetcher=fetch)
    redirected = shared.fetch(mission_id="mission", operation_id="redirect-first", url=URL)
    assert shared.fetch(mission_id="mission", operation_id="alias-before-refresh",
                        url=final_url)["shared_fetch_ref"] == redirected["shared_fetch_ref"]
    independent = shared.fetch(mission_id="mission", operation_id="independent-refresh",
                               url=final_url, refresh=True)
    shared.fetch(mission_id="mission", operation_id="redirect-refresh", url=URL,
                 refresh=True)
    current_final = shared.fetch(mission_id="mission", operation_id="independent-reader",
                                 url=final_url)
    assert current_final["shared_fetch_ref"] == independent["shared_fetch_ref"]
    assert calls == [URL, final_url, URL]


def test_expired_local_extraction_claim_recovers_without_refetch(tmp_path, monkeypatch):
    now = [100.0]
    store = StateStore(tmp_path / "state", clock=lambda: now[0])
    key = extraction_key(hashlib.sha256(CONTENT).hexdigest(), "text/html", "ordinary_html")

    def abandoned(tx):
        return ref(tx.put("shared_extraction", f"shared-extraction:{key}", {
            "schema": "ora.evidence.shared_extraction.v1", "key": key,
            "phase": "dispatched", "owner_nonce": "crashed-local-parser",
            "lease_expires_at": 101.0,
        }))

    store.transact("abandoned-local-extraction", {}, abandoned)
    calls = {"fetch": 0, "extract": 0}
    from ora.evidence import shared_fetch as module

    original_extract = module.extract_blocks

    def fetch(url):
        calls["fetch"] += 1
        return _receipt(url)

    def extract(*args, **kwargs):
        calls["extract"] += 1
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(module, "extract_blocks", extract)
    shared = SharedPublicFetch(store, fetcher=fetch)
    assert shared.fetch(mission_id="mission", operation_id="before-lease", url=URL)["status"] == "pending"
    assert calls == {"fetch": 1, "extract": 0}
    now[0] = 102.0
    recovered = shared.fetch(mission_id="mission", operation_id="after-lease", url=URL)
    assert recovered["status"] == "completed"
    assert calls == {"fetch": 1, "extract": 1}
    assert store.get("shared_extraction", f"shared-extraction:{key}")["version"] == 3
    assert shared.fetch(mission_id="mission", operation_id="replay", url=URL)["status"] == "completed"
    assert calls == {"fetch": 1, "extract": 1}


def test_shared_bytes_do_not_override_restricted_peer_hit(tmp_path):
    store = StateStore(tmp_path / "state")
    leads = _leads(store)
    shared = SharedPublicFetch(store, fetcher=lambda url: _receipt(url))
    fetched = shared.fetch(mission_id="mission", operation_id="shared", url=URL)

    def restrict(tx):
        old = tx.get(**leads[1][0])
        updated = tx.put("retrieval_hit", old["id"], {
            **old["body"], "source": {**old["body"]["source"],
                                      "rights_status": "restricted"},
        }, expected_version=old["version"])
        tx.link(updated, tx.get(**leads[1][1]), "derived_from")
        return ref(updated)

    restricted_hit = store.transact("restrict-second-hit", {}, restrict)
    intake = IntakeService(EvidenceLibrary(store))
    document = shared.adopt(
        intake, mission_id="mission", run_id="run", actor_id="peer-two",
        task_id="task-two", operation_id="adopt-restricted",
        hit_ref=restricted_hit, response_ref=leads[1][1],
        shared_fetch_ref=fetched["shared_fetch_ref"],
        shared_extraction_ref=fetched["shared_extraction_ref"],
    )
    inspected = intake.inspect_public_document(
        "inspect-restricted", document, mission_id="mission", run_id="run",
        actor_id="peer-two", task_id="task-two",
    )
    assert inspected["model_input_status"] == "not_granted"
    assert inspected["copy_ref"] is None
