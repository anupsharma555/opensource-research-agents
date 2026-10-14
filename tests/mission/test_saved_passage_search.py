"""Search retained task text without leaking another task or revoked copy."""

from __future__ import annotations

import pytest

from ora.coordination.board import ref
from ora.evidence import EvidenceLibrary, IntakeService, read_source_window
from ora.mission.context import WorkingContext
from ora.mission.passage_search import SavedPassageSearchError, search_saved_passages
from ora.state import StateConflict, StateStore
from ora.state.store import canonical


def _source(store: StateStore, suffix: str, text: str) -> dict:
    library = EvidenceLibrary(store)
    publication = library.register_publication(
        f"publication-{suffix}",
        identifiers={"doi": f"10.1234/search-{suffix}"},
        title=f"Synthetic publication {suffix}",
        verification="Synthetic DOI",
    )["publication_ref"]
    inspected = IntakeService(library).inspect_response(
        f"inspect-{suffix}", publication,
        content=f"DOI: 10.1234/search-{suffix}. {text}".encode(),
        content_type="text/plain", source_url=f"https://example.org/{suffix}",
        retrieved_at="2026-10-12T12:00:00Z", source_version="synthetic-v1",
        rights_status="permitted", rights_basis="Project-owned synthetic source",
        declared_surface="abstract", expected_doi=f"10.1234/search-{suffix}",
        identity_basis="Synthetic exact DOI",
    )
    return inspected["copy_ref"]


def _control(store: StateStore, suffix: str, sources: list[dict]) -> dict:
    def save(tx):
        return ref(tx.put("mission_task_control", f"control-{suffix}", {
            "mission_id": f"mission-{suffix}", "run_id": f"run-{suffix}",
            "actor_id": f"peer-{suffix}", "task_id": f"task-{suffix}",
            "source_refs": sources, "phase": "ready_model",
        }))

    pointer = store.transact(f"control-{suffix}", {}, save, mission_id=f"mission-{suffix}")
    return store.get(**pointer)


def _revoke(store: StateStore, pointer: dict) -> None:
    use = store.resolve("source_use.copy", f"{pointer['id']}:{pointer['version']}")

    def revoke(tx):
        tx.put("source_use", use["id"], {
            **use["body"], "allowed_uses": [], "policy_status": "revoked",
        }, expected_version=use["version"])
        return {"revoked": True}

    store.transact(f"revoke-{pointer['id']}", {}, revoke)


def test_search_is_task_scoped_and_returns_executable_exact_read(tmp_path) -> None:
    store = StateStore(tmp_path)
    own = _source(store, "own", "A unique zephyr observation is retained here.")
    foreign = _source(store, "foreign", "Zephyr also occurs in another mission.")
    control = _control(store, "own", [own])
    _control(store, "foreign", [foreign])

    result = search_saved_passages(WorkingContext(store), control, "zephyr")
    assert result["result_count"] == 1
    assert result["searched_copy_count"] == 1
    assert result["source_dependency_refs"] == [own]
    assert foreign["id"] not in canonical(result)
    hit = result["results"][0]
    assert hit["copy_ref"] == own
    assert "zephyr" in hit["snippet"].lower()
    assert hit["block_locator"].startswith("text; chars=")
    request = hit["next_request"]
    assert request["source_id"] == own["id"]
    assert request["source_version"] == own["version"]
    window = read_source_window(
        EvidenceLibrary(store), "read-own-hit", own,
        block_offset=request["block_offset"], char_offset=request["char_offset"],
        block_limit=1, char_limit=request["char_limit"],
    )
    assert "zephyr" in window["passages"][0]["text_preview"].lower()


def test_permission_filters_before_match_rank_and_counts(tmp_path) -> None:
    store = StateStore(tmp_path)
    allowed = _source(store, "allowed", "Quiet cedar note.")
    revoked = _source(store, "revoked", "Secret zephyr token in a revoked source.")
    control = _control(store, "task", [allowed, revoked])
    _revoke(store, revoked)

    result = search_saved_passages(WorkingContext(store), control, "zephyr")
    assert result["result_count"] == 0
    assert result["searched_copy_count"] == 1
    assert result["source_dependency_refs"] == [allowed]
    assert revoked["id"] not in canonical(result)
    assert "no token match in searched extracted text" in result["interpretation"]


def test_continuation_is_hash_bound_to_current_permission(tmp_path) -> None:
    store = StateStore(tmp_path)
    first = _source(store, "first", "Zephyr in the first copy.")
    second = _source(store, "second", "Zephyr in the second copy.")
    control = _control(store, "task", [first, second])
    context = WorkingContext(store)
    page = search_saved_passages(context, control, "zephyr", limit=1)
    assert page["next_cursor"] == 1
    assert page["source_dependency_refs"] == [first, second]
    # Normal model/tool turns advance control versions without changing the
    # authorized search projection. Continuation must survive that bookkeeping.
    updated = store.transact("advance-search-control", {}, lambda tx: ref(tx.put(
        "mission_task_control", control["id"], {**control["body"], "phase": "ready_tool"},
        expected_version=control["version"],
    )))
    control = store.get(**updated)
    continued = search_saved_passages(
        context, control, "zephyr", cursor=page["next_cursor"], limit=1,
        projection_sha256=page["projection_sha256"],
    )
    assert continued["results"][0]["copy_ref"] != page["results"][0]["copy_ref"]
    _revoke(store, second)
    with pytest.raises(StateConflict, match="search changed"):
        search_saved_passages(
            context, control, "zephyr", cursor=1, limit=1,
            projection_sha256=page["projection_sha256"],
        )
    fresh = search_saved_passages(context, control, "zephyr")
    assert fresh["result_count"] == 1
    assert second["id"] not in canonical(fresh)


def test_no_match_and_partial_extraction_bounds_are_explicit(tmp_path) -> None:
    store = StateStore(tmp_path)
    small = _source(store, "small", "No target term is here.")
    large = _source(store, "large", "A" * (2 * 1024 * 1024 + 1))
    control = _control(store, "task", [small, large])
    result = search_saved_passages(WorkingContext(store), control, "zephyr")
    assert result["result_count"] == 0
    assert result["searched_copy_count"] == 1
    assert result["coverage"] == "partial_retained_extracted_text"
    assert result["omitted_permitted_copies"] == [
        {"copy_ref": large, "reason": "search_byte_budget"},
    ]


def test_invalid_page_and_stale_task_control_are_rejected(tmp_path) -> None:
    store = StateStore(tmp_path)
    source = _source(store, "single", "Zephyr is here.")
    control = _control(store, "task", [source])
    context = WorkingContext(store)
    with pytest.raises(SavedPassageSearchError, match="hash-bound"):
        search_saved_passages(context, control, "zephyr", cursor=1)
    with pytest.raises(SavedPassageSearchError, match="bounded page"):
        search_saved_passages(context, control, "zephyr", limit=11)

    def update(tx):
        tx.put("mission_task_control", control["id"], {
            **control["body"], "phase": "updated",
        }, expected_version=control["version"])
        return {"updated": True}

    store.transact("update-control", {}, update)
    with pytest.raises(StateConflict, match="Task control changed"):
        search_saved_passages(context, control, "zephyr")
