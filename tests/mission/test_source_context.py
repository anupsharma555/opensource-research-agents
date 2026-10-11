"""Offline checks for exact source working sets and source-free recovery."""

from __future__ import annotations

import pytest

from ora.coordination.board import ref
from ora.evidence import EvidenceLibrary, IntakeService, read_source_window
from ora.mission.source_context import (
    SourceContextError,
    plan_source_turn,
    save_source_inventory,
    save_working_sources,
    source_inventory_page,
)
from ora.state import StateStore
from ora.state.store import canonical


def source(store: StateStore, operation_id: str, suffix: str, text: str,
           *, title: str | None = None) -> dict:
    library = EvidenceLibrary(store)
    publication = library.register_publication(
        f"publication-{suffix}",
        identifiers={"doi": f"10.1234/source-{suffix}"},
        title=title or f"Synthetic source {suffix}",
        verification="Synthetic exact DOI",
    )["publication_ref"]
    saved = IntakeService(library).inspect_response(
        operation_id,
        publication,
        content=f"DOI: 10.1234/source-{suffix}. {text}".encode(),
        content_type="text/plain",
        source_url=f"https://example.org/source-{suffix}",
        retrieved_at="2026-10-09T12:00:00Z",
        source_version="synthetic-v1",
        rights_status="permitted",
        rights_basis="Project-owned synthetic source",
        declared_surface="abstract",
        expected_doi=f"10.1234/source-{suffix}",
        identity_basis="Synthetic exact DOI",
    )
    return saved["copy_ref"]


def control(store: StateStore, source_refs: list[dict], selected_passage_refs=None) -> dict:
    def save(tx):
        tx.put("mission_run", "run", {"mission_id": "m", "run_id": "run"})
        tx.put("agent_instance", "peer", {"mission_id": "m", "run_id": "run"})
        tx.put("board_proposal", "task", {"mission_id": "m", "status": "open"})
        item = tx.put(
            "mission_task_control",
            "control",
            {
                "mission_id": "m",
                "run_id": "run",
                "actor_id": "peer",
                "task_id": "task",
                "source_refs": source_refs,
                "selected_passage_refs": selected_passage_refs or {},
                "phase": "ready_model",
            },
        )
        return ref(item)

    return store.transact("control", {}, save, mission_id="m")


def response(store: StateStore, call_id: str) -> dict:
    def save(tx):
        call = tx.put(
            "agent_call",
            call_id,
            {
                "mission_id": "m",
                "instance_id": "peer",
                "task_id": "task",
            },
        )
        item = tx.put(
            "model_response_receipt",
            f"response-{call_id}",
            {
                "mission_id": "m",
                "status": "tool_requested",
                "task_id": "task",
                "call_ref": ref(call),
            },
        )
        return ref(item)

    return store.transact(f"response-{call_id}", {}, save, mission_id="m")


def test_existing_checked_sources_are_used_when_they_fit(tmp_path) -> None:
    store = StateStore(tmp_path)
    first = source(store, "intake-a", "a", "One short observation.")
    second = source(store, "intake-b", "b", "A second short observation.")
    control_ref = control(store, [first, second])
    plan = plan_source_turn(store, control_ref, source_input_allowance_tokens=1000)
    assert plan["mode"] == "all_allowed_sources_fit"
    assert [item["copy_ref"] for item in plan["source_inputs"]] == [first, second]
    assert plan["retained_source_refs"] == [first, second]
    assert plan["excluded_source_refs"] == []


def test_oversize_sources_get_exact_inventory_then_peer_selects_subset(tmp_path) -> None:
    store = StateStore(tmp_path)
    first = source(store, "intake-a", "a", "A" * 3000)
    second = source(store, "intake-b", "b", "A small independent observation.")
    control_ref = control(store, [first, second])
    plan = plan_source_turn(store, control_ref, source_input_allowance_tokens=300)
    assert plan["mode"] == "source_free_recovery"
    assert plan["reason"] == "exact_source_text_exceeds_allowance"
    assert plan["source_inputs"] == []
    assert plan["retained_source_refs"] == [first, second]

    inventory_ref = save_source_inventory(store, "inventory", control_ref=control_ref)
    assert save_source_inventory(store, "inventory-again", control_ref=control_ref) == inventory_ref
    page1 = source_inventory_page(
        store,
        inventory_ref=inventory_ref,
        actor_id="peer",
        task_id="task",
        limit=1,
    )
    page2 = source_inventory_page(
        store,
        inventory_ref=inventory_ref,
        actor_id="peer",
        task_id="task",
        cursor=page1["next_cursor"],
        limit=1,
    )
    assert [page1["items"][0]["ref"], page2["items"][0]["ref"]] == [first, second]
    assert page2["next_cursor"] is None
    assert page1["items"][0]["fetch"]["tool"] == "read_source_window"
    assert "Synthetic source a" in page1["items"][0]["title"]
    assert "text" not in page1["items"][0]
    with pytest.raises(SourceContextError, match="another peer"):
        source_inventory_page(store, inventory_ref=inventory_ref, actor_id="other", task_id="task")

    chosen = save_working_sources(
        store,
        "choose-small",
        control_ref=control_ref,
        response_ref=response(store, "call-1"),
        selected_source_refs=[second],
    )
    assert chosen["version"] == 1
    selected = plan_source_turn(store, control_ref, source_input_allowance_tokens=300)
    assert selected["mode"] == "model_working_set"
    assert [item["copy_ref"] for item in selected["source_inputs"]] == [second]
    assert selected["retained_source_refs"] == [first, second]


def test_later_inventory_pages_recover_saved_windows_without_text_or_revoked_use(tmp_path) -> None:
    store = StateStore(tmp_path)
    copies = [
        source(store, f"intake-{index}", str(index), f"Section {index}. " + "X" * 2100)
        for index in range(4)
    ]
    control_ref = control(store, copies)
    library = EvidenceLibrary(store)
    third = copies[2]
    first = read_source_window(
        library, "third-first", third, block_offset=0, char_offset=0,
        block_limit=1, char_limit=800,
    )
    farther = read_source_window(
        library, "third-farther", third, block_offset=0, char_offset=800,
        block_limit=1, char_limit=800,
    )
    repeated = read_source_window(
        library, "third-repeat", third, block_offset=0, char_offset=0,
        block_limit=1, char_limit=400,
    )

    def save_reading(tx):
        previous = tx.get(**control_ref)
        return ref(tx.put("mission_task_control", previous["id"], {
            **previous["body"],
            "selected_passage_refs": {third["id"]: repeated["passage_refs"]},
            "read_window_refs": [
                first["window_ref"], farther["window_ref"], repeated["window_ref"],
            ],
        }, expected_version=control_ref["version"]))

    current_ref = store.transact("third-reading-control", {}, save_reading, mission_id="m")
    inventory_ref = save_source_inventory(
        store, "all-reading-inventory", control_ref=current_ref,
    )
    pages, found, cursor = [], [], 0
    while True:
        page = source_inventory_page(
            store, inventory_ref=inventory_ref, actor_id="peer", task_id="task",
            cursor=cursor, limit=20,
        )
        pages.append(page)
        found.extend(page["items"])
        assert len(canonical(page)) <= 1800
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
    assert len(pages) > 1
    assert [item["ref"] for item in found] == copies
    reading = found[2]["saved_reading"]
    assert reading["saved_coverage"]["status"] == "incomplete_extracted_text"
    assert reading["saved_coverage"]["covered_chars"] == 1600
    assert reading["saved_coverage"]["first_uncovered_cursor"] == {
        "block_offset": 0, "char_offset": 1600,
    }
    assert reading["latest_window_ref"] == repeated["window_ref"]
    assert reading["latest_window_next_cursor"] == {
        "block_offset": 0, "char_offset": 400,
    }
    assert reading["furthest_start_saved_window"]["window_ref"] == farther["window_ref"]
    assert reading["source_text_in_this_call"] is False
    assert reading["past_model_delivery"] == "unverified"
    assert found[2]["fetch"] == {
        "tool": "read_source_window", "source_id": third["id"],
        "source_version": third["version"],
    }
    assert "Section 2" not in canonical(pages)

    use = store.resolve("source_use.copy", f"{third['id']}:{third['version']}")

    def revoke(tx):
        tx.put("source_use", use["id"], {
            **use["body"], "allowed_uses": [], "policy_status": "revoked",
        }, expected_version=use["version"])
        return {"revoked": True}

    store.transact("revoke-third-reading", {}, revoke, mission_id="m")
    restricted = source_inventory_page(
        store, inventory_ref=inventory_ref, actor_id="peer", task_id="task",
        cursor=2, limit=1,
    )["items"][0]
    assert restricted["ref"] == third
    assert restricted["model_input_status"] == "restricted"
    assert restricted["fetch"] is None
    assert "saved_reading" not in restricted
    with pytest.raises(SourceContextError, match="another peer"):
        source_inventory_page(
            store, inventory_ref=inventory_ref, actor_id="foreign", task_id="task",
            cursor=2, limit=1,
        )


def test_escaped_title_and_tiny_page_still_advance_exact_inventory_cursor(tmp_path) -> None:
    store = StateStore(tmp_path)
    copy_ref = source(
        store, "escaped-title-intake", "escaped", "Synthetic checked text. " * 30,
        title='"\\' * 80,
    )
    control_ref = control(store, [copy_ref])
    library = EvidenceLibrary(store)
    first = read_source_window(
        library, "escaped-title-first", copy_ref,
        block_offset=0, char_offset=0, block_limit=1, char_limit=100,
    )
    later = read_source_window(
        library, "escaped-title-later", copy_ref,
        block_offset=0, char_offset=100, block_limit=1, char_limit=100,
    )
    repeated = read_source_window(
        library, "escaped-title-repeat", copy_ref,
        block_offset=0, char_offset=0, block_limit=1, char_limit=50,
    )

    def save_reading(tx):
        prior = tx.get(**control_ref)
        return ref(tx.put("mission_task_control", prior["id"], {
            **prior["body"],
            "selected_passage_refs": {copy_ref["id"]: repeated["passage_refs"]},
            "read_window_refs": [
                first["window_ref"], later["window_ref"], repeated["window_ref"],
            ],
        }, expected_version=control_ref["version"]))

    control_ref = store.transact(
        "escaped-title-reading", {}, save_reading, mission_id="m",
    )
    inventory_ref = save_source_inventory(
        store, "escaped-title-inventory", control_ref=control_ref,
    )
    ordinary = source_inventory_page(
        store, inventory_ref=inventory_ref, actor_id="peer", task_id="task",
    )
    assert len(canonical(ordinary)) <= 1800
    assert ordinary["next_cursor"] is None
    assert ordinary["items"][0]["ref"] == copy_ref
    assert ordinary["items"][0]["saved_reading"]["source_text_in_this_call"] is False

    trimmed = source_inventory_page(
        store, inventory_ref=inventory_ref, actor_id="peer", task_id="task",
        reply_char_limit=1500,
    )
    assert trimmed["items"][0]["title"] is None
    assert trimmed["items"][0]["title_omitted_for_page_budget"] is True
    assert trimmed["items"][0]["saved_reading"]["latest_window_ref"] == (
        repeated["window_ref"]
    )

    tiny = source_inventory_page(
        store, inventory_ref=inventory_ref, actor_id="peer", task_id="task",
        reply_char_limit=500,
    )
    assert len(canonical(tiny)) <= 500
    assert tiny["next_cursor"] is None
    assert tiny["items"][0]["ref"] == copy_ref
    assert tiny["items"][0]["model_input_status"] == "permitted"
    assert tiny["items"][0]["fetch"] == {"tool": "read_source_window"}
    assert tiny["items"][0]["saved_reading_omitted_for_page_budget"] is True
    assert tiny["items"][0]["title_omitted_for_page_budget"] is True


def test_supplied_selected_window_is_used_instead_of_full_copy(tmp_path, monkeypatch) -> None:
    store = StateStore(tmp_path)
    content = b"Synthetic supplied text " + b"X" * 4000
    blob = store.put_blob(content)
    selected = {"kind": "source_passage", "id": "selected-window", "version": 1}

    def save(tx):
        publication = tx.put(
            "publication", "supplied-publication", {"title": "Synthetic supplied source"}
        )
        encounter = tx.put(
            "source_encounter",
            "supplied-encounter",
            {
                "publication": ref(publication),
                "route": "user_supplied",
                "status": "accepted",
            },
        )
        copy = tx.put(
            "source_copy",
            "supplied-copy",
            {
                "publication": ref(publication),
                "encounter": ref(encounter),
                "mime_type": "text/plain",
                "rights_status": "permitted",
                "blob": blob,
                "retain_bytes": True,
            },
        )
        tx.put(
            "source_passage",
            "selected-window",
            {"copy_ref": ref(copy), "text": "Short selected text"},
        )
        return ref(copy)

    copy_ref = store.transact("supplied", {}, save, mission_id="m")
    control_ref = control(store, [copy_ref], {copy_ref["id"]: [selected]})
    observed = []

    def checked(_library, pointer, passage_refs):
        observed.append((pointer, passage_refs))
        return {"text": "Short selected text"}

    monkeypatch.setattr("ora.mission.source_context.checked_model_text", checked)
    plan = plan_source_turn(store, control_ref, source_input_allowance_tokens=200)
    assert plan["mode"] == "all_allowed_sources_fit"
    assert plan["source_inputs"] == [{"copy_ref": copy_ref, "passage_refs": [selected]}]
    assert observed == [(copy_ref, [selected])]


def test_changed_selected_use_forces_recovery_without_silent_fallback(tmp_path) -> None:
    store = StateStore(tmp_path)
    chosen_copy = source(store, "intake-a", "a", "A short observation.")
    other_copy = source(store, "intake-b", "b", "Another observation.")
    control_ref = control(store, [chosen_copy, other_copy])
    save_working_sources(
        store,
        "choose-a",
        control_ref=control_ref,
        response_ref=response(store, "call-1"),
        selected_source_refs=[chosen_copy],
    )
    use = store.resolve("source_use.copy", f"{chosen_copy['id']}:{chosen_copy['version']}")

    def revoke(tx):
        tx.put(
            "source_use",
            use["id"],
            {
                **use["body"],
                "allowed_uses": ["discovery"],
                "policy_status": "revoked",
            },
            expected_version=use["version"],
        )
        return {"revoked": True}

    store.transact("revoke", {}, revoke)
    plan = plan_source_turn(store, control_ref, source_input_allowance_tokens=1000)
    assert plan["mode"] == "source_free_recovery"
    assert plan["reason"] == "selected_source_use_changed"
    assert plan["source_inputs"] == []
    assert plan["retained_source_refs"] == [chosen_copy, other_copy]
