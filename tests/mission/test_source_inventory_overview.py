"""Compact identity pages spend their allowance on sources, not discarded detail."""

import pytest

from ora.mission.source_context import (
    SourceContextError,
    save_source_inventory,
    source_inventory_page,
)
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_source_context import control, source


@pytest.mark.parametrize("title", ["Synthetic study", 'Synthetic "study" \\ Ω ' * 10])
def test_initial_overview_keeps_three_identities_within_one_page_budget(tmp_path, title):
    store = StateStore(tmp_path / "state")
    copies = [source(store, f"intake-{i}", str(i), "Synthetic evidence. " * 30,
                     title=f"{title} {i}") for i in range(3)]
    control_ref = control(store, copies)
    inventory = save_source_inventory(store, "inventory", control_ref=control_ref)
    args = dict(inventory_ref=inventory, actor_id="peer", task_id="task", limit=3)
    overview = source_inventory_page(store, **args, include_saved_reading=False,
                                    reply_char_limit=10_000)
    allowance = len(canonical(overview))
    bounded = source_inventory_page(store, **args, include_saved_reading=False,
                                   reply_char_limit=allowance)
    detailed = source_inventory_page(store, **args, reply_char_limit=allowance)
    assert bounded == overview
    assert [row["ref"] for row in bounded["items"]] == copies
    assert bounded["next_cursor"] is None
    assert len(detailed["items"]) < len(bounded["items"])
    assert detailed["next_cursor"] == len(detailed["items"])
    assert len(canonical(detailed)) <= allowance
    assert all(row["model_input_status"] == "permitted" and row["fetch"]
               for row in bounded["items"])
    assert all(row["title"] and "saved_reading" not in row for row in bounded["items"])
    # A compact initial page does not change the normal reader or save a read.
    full = source_inventory_page(store, **args, reply_char_limit=10_000)
    assert [row["ref"] for row in full["items"]] == copies
    assert all("saved_reading" in row for row in full["items"])
    assert store.records("source_read_window") == []
    assert store.get(**control_ref)["body"]["selected_passage_refs"] == {}


def test_compact_inventory_rechecks_revocation_and_peer_ownership(tmp_path):
    store = StateStore(tmp_path / "state")
    copies = [source(store, f"intake-{i}", str(i), "Synthetic evidence.") for i in range(3)]
    control_ref = control(store, copies)
    inventory = save_source_inventory(store, "inventory", control_ref=control_ref)
    use = store.resolve("source_use.copy", f"{copies[1]['id']}:{copies[1]['version']}")

    def revoke(tx):
        tx.put("source_use", use["id"], {
            **use["body"], "allowed_uses": [], "policy_status": "revoked",
        }, expected_version=use["version"])
        return {"revoked": True}

    store.transact("revoke-inventory-source", {}, revoke, mission_id="m")
    args = dict(inventory_ref=inventory, actor_id="peer", task_id="task",
                include_saved_reading=False, reply_char_limit=10_000)
    page = source_inventory_page(store, **args)
    assert [row["ref"] for row in page["items"]] == copies
    restricted = page["items"][1]
    assert restricted["model_input_status"] == "restricted"
    assert restricted["fetch"] is None
    assert "saved_reading" not in restricted
    assert "saved_reading_fetch" not in restricted
    with pytest.raises(SourceContextError, match="another peer"):
        source_inventory_page(store, **{**args, "actor_id": "foreign"})
