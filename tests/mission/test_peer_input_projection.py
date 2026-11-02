"""Peer obligations page only exact duplicate scope metadata in model input."""

from __future__ import annotations

import json

from ora.coordination.board import ref
from ora.mission.context import WorkingContext
from ora.state import StateStore
from tests.mission.test_context import bind_no_source_pack
from tests.mission.test_peer_answer_preview import _fixture, _pack


def _run_scope(store: StateStore, *, question: str, scope: str) -> dict:
    current = store.get("mission_run", "run")

    def save(tx):
        return ref(tx.put(
            "mission_run", "run",
            {**current["body"], "question": question, "scope": scope},
            expected_version=current["version"],
        ))

    return store.transact(
        "synthetic-peer-projection-scope", {"question": question, "scope": scope},
        save, mission_id="m",
    )


def test_short_pending_answer_keeps_full_text_with_compact_exact_scope(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store)
    run_ref = _run_scope(
        store, question="Review the peer answer", scope="Full synthetic user scope.",
    )
    context, pack, index = _pack(store, saved)
    scope = index["run_scope"]
    assert scope["run_ref"] == run_ref
    assert scope["question_in_task_objective"] is True
    assert scope["question_truncated"] is False
    assert scope["scope_preview"] == "Full synthetic user scope."
    assert scope["scope_truncated"] is False
    assert not {"question_ref", "question_chars", "scope_chars", "scope_sha256"} & set(scope)
    preview = index["peer_answer_previews"][0]
    assert preview["question"] == "Please review this synthetic claim."
    assert preview["report"]["body"] == "A bounded synthetic peer assessment."
    assert preview["request_ref"] == saved["request_ref"]
    assert preview["answer_ref"] == saved["answer_ref"]
    assert saved["request_ref"] in pack.included_refs
    assert saved["answer_ref"] in pack.included_refs
    assert saved["report_ref"] in pack.included_refs
    assert context.context_source_dependency_refs(pack) == ()
    inclusion = bind_no_source_pack(store, context, pack, "compact-peer-projection")
    assert store.get(**inclusion)["body"]["included_refs"]


def test_long_scope_and_answer_keep_exact_reader_and_truncation_state(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store, body="Long synthetic answer. " * 140)
    run_ref = _run_scope(
        store, question="Review the peer answer", scope="Long scoped request. " * 100,
    )
    context, pack, index = _pack(store, saved)
    scope = index["run_scope"]
    assert scope["run_ref"] == run_ref
    assert scope["scope_truncated"] is True
    assert scope["scope_chars"] == len("Long scoped request. " * 100)
    assert scope["scope_sha256"]
    assert scope["fetch_full_scope_and_source_refs"]["tool"] == "read_context_record"
    assert "question_ref" not in scope  # The exact run_ref is the same pointer.
    assert index["peer_answer_review_cues"][0]["question"] == (
        "Please review this synthetic claim."
    )
    cue = index["peer_answer_review_cues"][0]
    assert cue["owner"] == "you"
    assert cue["request_ref"] == saved["request_ref"]
    assert cue["answer_ref"] == saved["answer_ref"]
    assert cue["read_request"]["tool"] == "read_context_record"
    assert {key: cue["read_request"][key] for key in ("kind", "id", "version")} == (
        saved["report_ref"]
    )
    assert "Long synthetic answer." not in json.dumps(index)
    assert context.context_source_dependency_refs(pack) == ()


def test_no_peer_obligation_keeps_previous_scope_projection(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store)
    _run_scope(store, question="Review the peer answer", scope="Full synthetic user scope.")
    requester_control = store.get("mission_task_control", "requester-control")
    request = store.get("mail_request", "peer-preview-request")

    def close_review(tx):
        updated = tx.put("mail_request", request["id"], {
            **request["body"], "status": "reviewed",
        }, expected_version=request["version"])
        return ref(updated)

    store.transact("synthetic-peer-review-complete", {}, close_review, mission_id="m")
    context = WorkingContext(store)
    pack = context.assemble(
        saved["call_ref"], saved["task_ref"], control_ref=ref(requester_control),
    )
    index = json.loads(pack.messages[-1]["content"])
    assert index["run_scope"]["question_ref"] == index["run_scope"]["run_ref"]
    assert index["run_scope"]["scope_sha256"]
    assert index["run_scope"]["scope_chars"] == len("Full synthetic user scope.")
