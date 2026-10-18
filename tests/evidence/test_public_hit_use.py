"""Current model-use permission follows an exact public web hit's checked copy."""

import hashlib

from ora.coordination.board import ref
from ora.evidence import EvidenceLibrary, IntakeService
from ora.evidence.hit_use import checked_hit_dependencies, hit_allows_reinspection
from ora.mission.shared_materials import shared_hit_access
from ora.state import StateStore


def test_public_hit_candidate_grant_and_revocation_are_exact(tmp_path):
    store = StateStore(tmp_path / "state")
    intake = IntakeService(EvidenceLibrary(store))
    url = "https://example.org/report"
    content = b"<html><body><p>Synthetic checked web detail.</p></body></html>"

    def save_leads(tx):
        tx.put("agent_instance", "peer", {"run_id": "run"})
        first_delivery = tx.put("retrieval_delivery", "delivery-one", {})
        second_delivery = tx.put("retrieval_delivery", "delivery-two", {})
        leads = []
        for suffix, delivery in (("one", first_delivery), ("two", second_delivery)):
            hit = tx.put("retrieval_hit", f"hit-{suffix}", {
                "route": "exa.search", "mission_id": "mission", "task_id": "task",
                "agent_instance_id": "peer", "delivery_ref": ref(delivery),
                "source": {"origin": "original_source", "original_url": url,
                           "rights_status": "unknown", "content": None},
            })
            tx.link(hit, delivery, "derived_from")
            leads.append((ref(hit), ref(delivery)))
        return {"leads": leads}

    leads = store.transact("synthetic-public-leads", {}, save_leads)["leads"]
    first_hit = store.get(**leads[0][0])
    second_hit = store.get(**leads[1][0])
    assert checked_hit_dependencies(store, first_hit) == []
    assert hit_allows_reinspection(store, first_hit)
    assert hit_allows_reinspection(store, second_hit)

    fetched = {
        "status": 200, "content": content,
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "byte_length": len(content), "content_type": "text/html",
        "requested_url": url, "final_url": url, "redirect_chain": [],
        "access_basis": "public_https_unauthenticated", "authenticated": False,
        "retrieved_at": "2026-10-15T12:00:00Z",
    }
    document_ref = intake.save_public_document_fetch(
        "synthetic-public-fetch", *leads[0], mission_id="mission", run_id="run",
        actor_id="peer", task_id="task", fetched=fetched,
    )
    assert checked_hit_dependencies(store, first_hit) == []  # Fetch alone grants no model use.
    inspected = intake.inspect_public_document(
        "synthetic-public-inspect", document_ref, mission_id="mission", run_id="run",
        actor_id="peer", task_id="task",
    )
    copy_ref = inspected["copy_ref"]
    assert copy_ref is not None
    assert checked_hit_dependencies(store, first_hit) == [copy_ref]
    assert shared_hit_access(store, first_hit)

    use = store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")

    def revoke(tx):
        current = tx.get("source_use", use["id"])
        return ref(tx.put("source_use", use["id"], {
            **current["body"], "allowed_uses": [], "policy_status": "revoked",
        }, expected_version=current["version"]))

    store.transact("synthetic-public-revoke", {}, revoke)
    assert checked_hit_dependencies(store, first_hit) is None
    assert not shared_hit_access(store, first_hit)
    assert not hit_allows_reinspection(store, first_hit)
    assert checked_hit_dependencies(store, second_hit) == []
    assert shared_hit_access(store, second_hit)
    assert store.get(**document_ref)["body"]["hit_ref"] == leads[0][0]

    def observe(tx):
        return {"first": hit_allows_reinspection(tx, tx.get(**leads[0][0])),
                "second": hit_allows_reinspection(tx, tx.get(**leads[1][0]))}

    assert store.transact("synthetic-public-observe", {}, observe) == {
        "first": False, "second": True,
    }
