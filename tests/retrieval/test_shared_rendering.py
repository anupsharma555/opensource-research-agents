"""Offline shared Browserless rendering with task-local source use."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from ora.coordination.board import ref
from ora.evidence import EvidenceLibrary, IntakeService, read_source_window
from ora.retrieval.providers import HTTPResult
from ora.retrieval.rendering import render_public_document
from ora.state import StateError, StateStore
from tests.evidence.test_shared_fetch import URL, _leads, _receipt
from tests.retrieval.test_rendering import VALUES

ORDINARY = b"<html><body><p>Public page omits a dynamic appendix.</p></body></html>"
RENDERED = "<html><body><main>Separate rendered appendix evidence.</main></body></html>"


def _spec(suffix: str):
    return SimpleNamespace(
        mission_id="mission", run_id="run", actor_id=f"peer-{suffix}",
        proposal_ref={"id": f"task-{suffix}"}, work_id=f"render-{suffix}",
    )


def _setup(tmp_path):
    store = StateStore(tmp_path / "state")
    intake = IntakeService(EvidenceLibrary(store))
    leads = _leads(store)
    documents = []
    for suffix, (hit_ref, response_ref) in zip(("one", "two"), leads):
        document = intake.save_public_document_fetch(
            f"ordinary-{suffix}", hit_ref, response_ref,
            mission_id="mission", run_id="run", actor_id=f"peer-{suffix}",
            task_id=f"task-{suffix}", fetched=_receipt(URL, ORDINARY),
        )
        documents.append(document)
    return store, intake, documents


def _render_response(*, final=URL, redirects=None):
    return HTTPResult(200, json.dumps({"data": {
        "html": RENDERED, "requested_url": URL, "final_url": final,
        "navigation_url": final, "navigation_status": 200,
        "redirect_chain": redirects if redirects is not None else [],
    }}).encode(), {})


def _render(store, intake, document, suffix, transport, *, values=VALUES):
    return render_public_document(
        store, intake, _spec(suffix), {
            "ordinary_document_ref": document,
            "reason": "The selected public page omits a JavaScript appendix",
        }, values, transport=transport, shared_enabled=True,
    )


def test_two_peers_share_one_render_and_extraction_but_get_distinct_grants(tmp_path, monkeypatch):
    store, intake, documents = _setup(tmp_path)
    from ora.evidence import shared_fetch as module

    original_extract = module.extract_blocks
    counts = {"render": 0, "extraction": 0}

    def transport(*_args, **_kwargs):
        counts["render"] += 1
        return _render_response()

    def extract(*args, **kwargs):
        counts["extraction"] += 1
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(module, "extract_blocks", extract)
    first, first_refs = _render(store, intake, documents[0], "one", transport)
    second, second_refs = _render(store, intake, documents[1], "two", transport)
    assert first["status"] == second["status"] == "inspected"
    assert counts == {"render": 1, "extraction": 1}
    assert first["document_ref"] != second["document_ref"]
    assert first["copy_ref"] != second["copy_ref"]
    assert first_refs == (first["copy_ref"],)
    assert second_refs == (second["copy_ref"],)
    assert store.get(**first["document_ref"])["body"]["ordinary_document_ref"] == documents[0]
    assert store.get(**second["document_ref"])["body"]["ordinary_document_ref"] == documents[1]
    assert len(store.records("shared_render_family")) == 1
    assert len(store.records("shared_extraction")) == 1
    assert len(store.records("shared_public_fetch")) == 1
    restarted = StateStore(tmp_path / "state")
    after_restart, _ = _render(restarted, IntakeService(EvidenceLibrary(restarted)),
                               documents[0], "one", lambda *_a, **_k: pytest.fail("render replayed"))
    assert after_restart["copy_ref"] == first["copy_ref"]
    assert counts == {"render": 1, "extraction": 1}

    use = store.resolve("source_use.copy", f"{first['copy_ref']['id']}:{first['copy_ref']['version']}")
    store.transact("revoke-first-rendered-copy", {}, lambda tx: {
        "use": ref(tx.put("source_use", use["id"], {
            **use["body"], "policy_status": "revoked", "allowed_uses": [],
        }, expected_version=use["version"]))
    })
    with pytest.raises(ValueError, match="does not grant"):
        read_source_window(EvidenceLibrary(store), "revoked-rendered-window", first["copy_ref"])
    window = read_source_window(EvidenceLibrary(store), "other-rendered-window", second["copy_ref"])
    assert "rendered appendix evidence" in str(window)


def test_inflight_and_restart_unknown_never_duplicate_render(tmp_path):
    store, intake, documents = _setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return _render_response()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_render, store, intake, documents[0], "one", slow)
        assert entered.wait(5)
        pending, refs = _render(store, intake, documents[1], "two", slow)
        assert pending["status"] == "pending" and refs == ()
        release.set()
        assert first.result(timeout=5)[0]["status"] == "inspected"
    joined, _ = _render(store, intake, documents[1], "two", slow)
    assert joined["status"] == "inspected" and len(calls) == 1

    another_store, another_intake, another_documents = _setup(tmp_path / "unknown")
    attempts = []

    def interrupted(*_args, **_kwargs):
        attempts.append(1)
        raise TimeoutError("synthetic transport outcome unknown")

    with pytest.raises(TimeoutError):
        _render(another_store, another_intake, another_documents[0], "one", interrupted)
    restarted = StateStore(tmp_path / "unknown" / "state")
    uncertain, _ = _render(restarted, IntakeService(EvidenceLibrary(restarted)),
                           another_documents[1], "two", interrupted)
    assert uncertain["status"] == "uncertain" and len(attempts) == 1


def test_peer_policy_and_navigation_checks_precede_shared_adoption(tmp_path):
    store, intake, documents = _setup(tmp_path)
    calls = []

    def transport(*_args, **_kwargs):
        calls.append(1)
        return _render_response()

    first, _ = _render(store, intake, documents[0], "one", transport)
    assert first["status"] == "inspected" and len(calls) == 1
    second = store.get(**documents[1])
    restricted = store.transact("restrict-second-ordinary", {}, lambda tx: ref(tx.put(
        "public_document_fetch", second["id"], {
            **second["body"], "hit_rights_status": "restricted",
        }, expected_version=second["version"])))
    with pytest.raises(StateError, match="permissions"):
        _render(store, intake, restricted, "two", transport)
    assert len(calls) == 1

    fresh_store, fresh_intake, fresh_documents = _setup(tmp_path / "bad-navigation")
    with pytest.raises(StateError, match="unverifiable"):
        _render(fresh_store, fresh_intake, fresh_documents[0], "one",
                lambda *_a, **_k: _render_response(final="https://other.example/page"))
    uncertain, _ = _render(fresh_store, fresh_intake, fresh_documents[1], "two", transport)
    assert uncertain["status"] == "uncertain" and len(calls) == 1


def test_provider_binding_separates_render_attempts_without_saving_token(tmp_path):
    store, intake, documents = _setup(tmp_path)
    calls = []

    def transport(*_args, **_kwargs):
        calls.append(1)
        return _render_response()

    _render(store, intake, documents[0], "one", transport)
    changed_values = {**VALUES, "BROWSERLESS_API_KEY": "second-synthetic-token"}
    _render(store, intake, documents[1], "two", transport, values=changed_values)
    assert len(calls) == 2
    assert len(store.records("shared_render_family")) == 2
    assert all("synthetic-token" not in json.dumps(item["body"])
               for item in store.records("shared_public_fetch"))
