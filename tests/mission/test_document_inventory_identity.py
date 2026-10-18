"""A source-free inventory keeps verified web identity and current use separate."""

from __future__ import annotations

import hashlib

from ora.coordination.board import ref
from ora.mission.source_context import save_source_inventory, source_inventory_page
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_source_context import control

URL = "https://example.org/docs/browser-search"


def _case(tmp_path, *, url=URL, invalid_document=False, wrong_reference=False,
          wrong_document_mission=False, navigation=False, copy_id="copy"):
    store = StateStore(tmp_path)
    html = b"<html><title>Synthetic guide</title><main>Saved text.</main></html>"
    digest = hashlib.sha256(html).hexdigest()
    blob = store.put_blob(html)

    def save(tx):
        if navigation:
            origin = tx.put("venue_navigation", "navigation", {
                "mission_id": "m", "title": "Synthetic program",
                "final_url": url, "content_sha256": digest,
            })
            origin_ref = ref(origin)
            route = "catalog_navigation"
            origin_key = "navigation_ref"
        elif invalid_document:
            origin_ref = {"kind": "public_document_fetch", "id": "missing-document",
                          "version": 1}
            route = "public_document_fetch"
            origin_key = "document_ref"
        else:
            origin = tx.put("public_document_fetch", "document", {
                "mission_id": "other-m" if wrong_document_mission else "m",
                "run_id": "run", "actor_id": "peer",
                "task_id": "task", "status": 200, "final_url": url,
                "content_sha256": digest, "detected_content_type": "text/html",
            })
            origin_ref = ref(origin)
            route = "public_document_fetch"
            origin_key = "document_ref"
        encounter = tx.put("source_encounter", "encounter", {
            "mission_id": "m", "route": route, origin_key: origin_ref,
        })
        copy = tx.put("source_copy", copy_id, {
            "mission_id": "m", "publication": None,
            "navigation_ref": origin_ref if navigation else None,
            "document_ref": None if navigation else origin_ref,
            "encounter": ref(encounter), "reference": "https://example.org/wrong"
            if wrong_reference else url,
            "content_sha256": digest, "mime_type": "text/html",
            "inspection_state": "checked", "blob": blob, "passage_ids": [],
        })
        quality = tx.put("intake_quality", "quality", {
            "mission_id": "m", "source_sha256": digest,
            "classification": "web_document", "truncated": False,
        })
        use = tx.put("source_use", "use", {
            "mission_id": "m", "copy_ref": ref(copy), origin_key: origin_ref,
            "source_url": url, "source_sha256": digest,
            "quality_ref": ref(quality), "allowed_uses": ["model_input"],
            "policy_status": "granted",
        })
        tx.bind("source_use.copy", f"{copy_id}:1", "source_use", use["id"])
        return {"copy_ref": ref(copy), "use_ref": ref(use)}

    saved = store.transact("document-inventory-fixture", {}, save, mission_id="m")
    control_ref = control(store, [saved["copy_ref"]])
    inventory_ref = save_source_inventory(store, "document-inventory", control_ref=control_ref)
    return store, inventory_ref, saved


def _item(store, inventory_ref, *, reply_char_limit=1800):
    page = source_inventory_page(
        store, inventory_ref=inventory_ref, actor_id="peer", task_id="task",
        reply_char_limit=reply_char_limit,
    )
    assert page["next_cursor"] is None
    assert len(canonical(page)) <= reply_char_limit
    return page["items"][0], page


def test_permitted_web_document_keeps_exact_url_in_normal_and_source_free_inventory(tmp_path):
    store, inventory_ref, saved = _case(tmp_path)
    item, page = _item(store, inventory_ref)
    assert item["ref"] == saved["copy_ref"]
    assert item["title"] == "Saved web document"
    assert item["source_url"] == URL
    assert item["ordinary_document_ref"] == store.get(**saved["copy_ref"])["body"]["document_ref"]
    assert item["model_input_status"] == "permitted"
    assert item["fetch"] == {
        "tool": "read_source_window", "source_id": saved["copy_ref"]["id"],
        "source_version": saved["copy_ref"]["version"],
    }
    assert item["saved_reading"]["source_text_in_this_call"] is False
    source_free = {"source_context_mode": "source_free_recovery", "inventory": page}
    assert URL in canonical(source_free)
    assert "Saved text." not in canonical(source_free)
    tight, _page = _item(store, inventory_ref, reply_char_limit=500)
    assert tight["source_url"] == URL
    assert tight["model_input_status"] == "permitted"
    assert tight["fetch"]["tool"] == "read_source_window"
    assert "ordinary_document_ref" not in tight
    assert tight["ordinary_document_ref_omitted_for_page_budget"] is True


def test_existing_navigation_title_does_not_gain_document_url(tmp_path):
    store, inventory_ref, _saved = _case(tmp_path, navigation=True)
    item, page = _item(store, inventory_ref)
    assert item["title"] == "Synthetic program"
    assert "source_url" not in item
    assert item["fetch"]["tool"] == "read_source_window"
    source_free = {"source_context_mode": "source_free_recovery", "inventory": page}
    assert "Synthetic program" in canonical(source_free)
    assert URL not in canonical(source_free)


def test_tight_inventory_keeps_short_url_with_realistic_copy_id(tmp_path):
    store, inventory_ref, saved = _case(tmp_path, copy_id="source-copy-" + "a" * 36)
    item, _page = _item(store, inventory_ref, reply_char_limit=500)
    assert item["ref"] == saved["copy_ref"]
    assert item["source_url"] == URL
    assert item["model_input_status"] == "permitted"
    assert item["fetch"]["tool"] == "read_source_window"


def test_permitted_exact_document_keeps_url_for_another_peer_task(tmp_path):
    store, _inventory_ref, saved = _case(tmp_path)

    def add_peer_control(tx):
        control = tx.put("mission_task_control", "review-control", {
            "mission_id": "m", "run_id": "run", "actor_id": "reviewer",
            "task_id": "review-task", "source_refs": [saved["copy_ref"]],
            "selected_passage_refs": {}, "phase": "ready_model",
        })
        return {"control_ref": ref(control)}

    peer = store.transact("document-peer-control", {}, add_peer_control, mission_id="m")
    inventory = save_source_inventory(
        store, "document-peer-inventory", control_ref=peer["control_ref"],
    )
    page = source_inventory_page(
        store, inventory_ref=inventory, actor_id="reviewer", task_id="review-task",
    )
    item = page["items"][0]
    assert item["source_url"] == URL
    assert item["model_input_status"] == "permitted"
    assert item["fetch"]["source_id"] == saved["copy_ref"]["id"]


def test_missing_document_or_mismatched_copy_reference_stays_generic(tmp_path):
    for suffix, options in (
        ("missing", {"invalid_document": True}),
        ("wrong-reference", {"wrong_reference": True}),
        ("wrong-origin-mission", {"wrong_document_mission": True}),
    ):
        store, inventory_ref, _saved = _case(tmp_path / suffix, **options)
        item, _page = _item(store, inventory_ref)
        assert item["title"] == "Saved web document"
        assert "source_url" not in item
        assert item["model_input_status"] == "permitted"
        assert item["fetch"]["tool"] == "read_source_window"


def test_revoked_current_use_hides_url_and_reader(tmp_path):
    store, inventory_ref, saved = _case(tmp_path)

    def revoke(tx):
        use = tx.get(**saved["use_ref"])
        changed = tx.put("source_use", use["id"], {
            **use["body"], "allowed_uses": [], "policy_status": "revoked",
        }, expected_version=use["version"])
        return {"use_ref": ref(changed)}

    store.transact("revoke-document-inventory", {}, revoke, mission_id="m")
    item, _page = _item(store, inventory_ref)
    assert item["model_input_status"] == "restricted"
    assert item["fetch"] is None
    assert "source_url" not in item
    assert "ordinary_document_ref" not in item
    assert "saved_reading" not in item


def test_oversize_url_is_omitted_last_without_losing_reader_or_current_use(tmp_path):
    long_url = "https://example.org/" + "a" * 1000
    store, inventory_ref, _saved = _case(tmp_path, url=long_url)
    item, _page = _item(store, inventory_ref, reply_char_limit=500)
    assert item["model_input_status"] == "permitted"
    assert item["fetch"]["tool"] == "read_source_window"
    assert "ordinary_document_ref" not in item
    assert "source_url" not in item
    assert item["source_url_omitted_for_page_budget"] is True
