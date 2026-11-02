"""Append rereads can reuse a retained whole submission without duplicating it."""

import json

import pytest

from ora.coordination.board import ref
from ora.evidence.intake import default_exact_submitted_passage
from ora.mission.source_context import plan_source_turn
from tests.mission.test_requested_window_recovery import _two_copy_read
from tests.mission.test_same_copy_append_recovery import _adjacent_read, _plans


@pytest.mark.parametrize("replace", [False, True])
def test_retained_whole_submission_respects_append_and_replace(
    tmp_path, monkeypatch, replace,
):
    host, run, actor, copy_ref, _other, first, _ = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1000, crowded=False,
    )
    try:
        whole = default_exact_submitted_passage(host.evidence, copy_ref)
        assert whole is not None
        prior = [whole, *first["passage_refs"]]
        control = host._control(run.run_id, actor)
        host._update_control(control, selected_passage_refs={copy_ref["id"]: prior})
        second = _adjacent_read(
            host, host._control(run.run_id, actor), copy_ref,
            {**first, "passage_refs": prior},
            selection_mode="replace" if replace else "append",
        )
        control = host._control(run.run_id, actor)
        selected = control["body"]["selected_passage_refs"][copy_ref["id"]]
        latest, candidate = _plans(host, control)
        assert latest["source_inputs"][0]["passage_refs"] == second["passage_refs"]
        if replace:
            assert candidate is None
            assert selected == second["passage_refs"]
            return
        assert candidate is not None
        assert candidate["reason"] == "selected_whole_submission_after_append"
        assert candidate["source_inputs"] == [
            {"copy_ref": copy_ref, "passage_refs": [whole]}
        ]
        packets = {}
        original = host.session.preview_request

        def capture(**kwargs):
            prepared = original(**kwargs)
            packets[prepared.request_sha256] = prepared.body
            return prepared

        host.session.preview_request = capture
        proposal = host.store.get("board_proposal", control["body"]["task_id"])
        fit = host._preview_soft_source_plan(
            control, proposal, candidate, total_target=12_000,
            sequence=control["body"]["sequence"] + 1, defer_core_tools=True,
        )
        assert fit is not None
        packet = packets[fit["request_sha256"]]
        sources = [source for message in packet["messages"]
                   if message["role"] == "user" and message["content"].startswith("{")
                   for source in json.loads(message["content"]).get("source_packets", [])]
        assert len(sources) == 1 and sources[0]["passage_refs"] == [whole]
        assert sources[0]["text"].count(host.store.get(**whole)["body"]["text"]) == 1
        assert host._pending_peer_input_preserved(control, fit)
        assert host._control(run.run_id, actor)["body"]["selected_passage_refs"][
            copy_ref["id"]] == [*prior, *second["passage_refs"]]
        assert host.store.records("model_dispatch_attempt") == []
    finally:
        host.close()


def test_whole_submission_does_not_hide_unverified_selected_passage(tmp_path, monkeypatch):
    host, run, actor, copy_ref, other, first, _ = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1000, crowded=False,
    )
    try:
        whole = default_exact_submitted_passage(host.evidence, copy_ref)
        foreign = default_exact_submitted_passage(host.evidence, other)
        assert whole is not None and foreign is not None
        prior = [whole, *first["passage_refs"], foreign]
        control = host._control(run.run_id, actor)
        host._update_control(control, selected_passage_refs={copy_ref["id"]: prior})
        _adjacent_read(host, host._control(run.run_id, actor), copy_ref,
                       {**first, "passage_refs": prior})
        control = host._control(run.run_id, actor)
        recovery = plan_source_turn(host.store, ref(control), source_input_allowance_tokens=1)
        latest = host._requested_window_source_plan(control, recovery)
        assert latest is None or host._current_copy_append_source_plan(control, latest) is None
        assert host.store.records("model_dispatch_attempt") == []
    finally:
        host.close()
