"""Carry checked older selections as one group without changing durable source state."""

from __future__ import annotations

import pytest

from ora.coordination.board import ref
from ora.evidence import read_source_window
from ora.evidence.intake import default_exact_submitted_passage
from ora.mission.source_context import plan_source_turn, save_working_sources
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_peer_answer_preview import _fixture
from tests.mission.test_requested_window_recovery import _two_copy_read
from tests.mission.test_selected_window_admission import _saved_tool_request
from tests.mission.test_service import service


def _read_second(host, control, late, *, suffix="current"):
    window = read_source_window(
        host.evidence, f"synthetic-multi-copy-{suffix}", late,
        block_offset=0, block_limit=1, char_offset=0, char_limit=700,
    )
    window.update(
        requested_char_limit=700, effective_char_limit=700,
        returned_chars=sum(len(item["text_preview"]) for item in window["passages"]),
        remaining_in_block_chars=next(
            (item["characters"] for item in window["omissions"]
             if item["reason"] == "remaining_characters_in_block"), 0,
        ),
        next_request={"source_id": late["id"], "source_version": late["version"],
                      **window["next_cursor"], "char_limit": 700},
        model_selection={"mode": "replace", "passage_refs": window["passage_refs"],
                         "prior_passage_refs": []},
    )
    window["passages"] = [
        {**item, "span_id": host.store.get(**item["passage_ref"])["body"]["span_id"]}
        for item in window["passages"]
    ]
    response, _ = _saved_tool_request(
        host, control, response_id=f"synthetic-multi-copy-response-{suffix}",
        tool_id=f"synthetic-multi-copy-tool-{suffix}", name="read_source_window",
        arguments={"source_id": late["id"], "source_version": late["version"],
                   "block_offset": 0, "char_offset": 0, "char_limit": 700},
        result=window,
    )
    host._update_control(
        control, response_ref=response,
        tool_turn_refs=[*control["body"]["tool_turn_refs"], response],
        read_window_refs=[*control["body"]["read_window_refs"], window["window_ref"]],
        selected_passage_refs={**control["body"]["selected_passage_refs"],
                               late["id"]: window["passage_refs"]},
    )
    return window, response


def _candidate(host, control):
    recovery = plan_source_turn(host.store, ref(control), source_input_allowance_tokens=1)
    latest = host._requested_window_source_plan(control, recovery)
    if latest is None:
        return None
    return host._older_selected_window_source_plan(control, recovery, latest)


def _append_exact_submitted_read(host, control, early, *, char_limit, mode="append"):
    default = default_exact_submitted_passage(host.evidence, early)
    assert default is not None
    host._update_control(control, selected_passage_refs={
        **control["body"]["selected_passage_refs"], early["id"]: [default],
    })
    control = host.store.get("mission_task_control", control["id"])
    window = read_source_window(
        host.evidence, f"synthetic-equivalent-{char_limit}-{mode}", early,
        block_offset=0, block_limit=1, char_offset=0, char_limit=char_limit,
    )
    window.update(
        requested_char_limit=char_limit, effective_char_limit=char_limit,
        returned_chars=sum(len(item["text_preview"]) for item in window["passages"]),
        remaining_in_block_chars=next(
            (item["characters"] for item in window["omissions"]
             if item["reason"] == "remaining_characters_in_block"), 0,
        ),
        next_request=(
            {"source_id": early["id"], "source_version": early["version"],
             **window["next_cursor"], "char_limit": char_limit,
             "selection_mode": mode}
            if window["next_cursor"] else None
        ),
        model_selection={"mode": mode,
                         "passage_refs": [default, *window["passage_refs"]],
                         "prior_passage_refs": [default]},
    )
    window["passages"] = [
        {**item, "span_id": host.store.get(**item["passage_ref"])["body"]["span_id"]}
        for item in window["passages"]
    ]
    response, _ = _saved_tool_request(
        host, control,
        response_id=f"synthetic-equivalent-response-{char_limit}-{mode}",
        tool_id=f"synthetic-equivalent-tool-{char_limit}-{mode}",
        name="read_source_window",
        arguments={"source_id": early["id"], "source_version": early["version"],
                   "block_offset": 0, "char_offset": 0, "char_limit": char_limit,
                   "selection_mode": mode}, result=window,
    )
    host._update_control(
        control, response_ref=response,
        tool_turn_refs=[*control["body"]["tool_turn_refs"], response],
        read_window_refs=[*control["body"]["read_window_refs"], window["window_ref"]],
        selected_passage_refs={**control["body"]["selected_passage_refs"],
                               early["id"]: [default, *window["passage_refs"]]},
    )
    return default, window


@pytest.mark.parametrize("case", ["equivalent", "partial", "replace", "pending_peer"])
def test_exact_submitted_span_keeps_both_citation_refs_only_for_safe_append(
    tmp_path, monkeypatch, case,
):
    host, run, actor, early, late, _first_window, _ = _two_copy_read(
        tmp_path, monkeypatch, window_chars=2000, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        default, reread = _append_exact_submitted_read(
            host, control, early, char_limit=2000 if case == "partial" else 5000,
            mode="replace" if case == "replace" else "append",
        )
        control = host._control(run.run_id, actor)
        _read_second(host, control, late, suffix=f"equivalent-{case}")
        control = host._control(run.run_id, actor)
        if case == "pending_peer":
            host.store.transact(
                "synthetic-equivalent-pending-peer", {},
                lambda tx: tx.put("mail_request", "synthetic-equivalent-peer", {
                    "mission_id": control["body"]["mission_id"],
                    "recipient_id": control["body"]["actor_id"],
                    "requester_id": "synthetic-other-peer",
                    "status": "unanswered", "delivered_at": "2026-10-31T00:00:00Z",
                    "question": "Compare the supplied citation identifier.",
                }), mission_id=control["body"]["mission_id"],
            )
        candidate = _candidate(host, control)
        if case in {"partial", "replace"}:
            assert candidate is None
            return
        assert candidate is not None
        assert candidate["source_inputs"] == [
            {"copy_ref": late, "passage_refs": control["body"]["selected_passage_refs"][late["id"]]},
            {"copy_ref": early, "passage_refs": [default, *reread["passage_refs"]]},
        ]
        assert control["body"]["selected_passage_refs"][early["id"]] == [
            default, *reread["passage_refs"]
        ]
        assert host.store.get(**default)["body"]["text"] == host.store.get(
            **reread["passage_refs"][0])["body"]["text"]
        if case == "equivalent":
            proposal = host.store.get(**control["body"]["proposal_ref"])
            fit = host._preview_soft_source_plan(
                control, proposal, candidate, total_target=20_000,
                sequence=control["body"]["sequence"] + 1,
            )
            assert fit is not None
    finally:
        host.close()


def test_current_window_plus_all_older_checked_passages(tmp_path, monkeypatch):
    host, run, actor, early, late, older_window, _ = _two_copy_read(
        tmp_path, monkeypatch, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        current_window, _ = _read_second(host, control, late)
        control = host._control(run.run_id, actor)
        candidate = _candidate(host, control)
        assert candidate is not None
        assert candidate["source_inputs"] == [
            {"copy_ref": late, "passage_refs": current_window["passage_refs"]},
            {"copy_ref": early, "passage_refs": older_window["passage_refs"]},
        ]
        assert candidate["mode"] == "requested_window_recovery"
        assert control["body"]["source_refs"] == [early, late]
        assert control["body"]["selected_passage_refs"][early["id"]] == older_window[
            "passage_refs"
        ]
        proposal = host.store.get("board_proposal", control["body"]["task_id"])
        fit = host._preview_soft_source_plan(
            control, proposal, candidate, total_target=20_000,
            sequence=control["body"]["sequence"] + 1, defer_core_tools=True,
        )
        assert fit is not None
        assert fit["peer_answer_inline_refs"] == ()
        assert fit["peer_answer_cue_refs"] == ()
    finally:
        host.close()


def test_saved_read_choice_pages_only_exact_covered_range(tmp_path, monkeypatch):
    host, run, actor, early, late, _older_window, _ = _two_copy_read(
        tmp_path, monkeypatch, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        current_window, _ = _read_second(host, control, late)
        control = host._control(run.run_id, actor)
        exact = {"tool": "read_source_window", "source_id": late["id"],
                 "source_version": late["version"], "block_offset": 0,
                 "char_offset": 0, "char_limit": 700,
                 "selection_mode": "append", "purpose": "Inspect the selected comparison"}
        not_covered = {**exact, "char_offset": 700}
        navigation = {
            "saved_result_ref": {"kind": "context_tool_result", "id": "synthetic-list",
                                 "version": 1},
            "previewed_saved_page_matches": 2,
            "read_choices_from_saved_result": [
                {"source_ref": late, "read": exact},
                {"source_ref": late, "read": not_covered},
            ],
            "read_full_saved_listing": {"tool": "read_tool_result",
                                        "result_id": "synthetic-list"},
        }
        projected = host.context._page_covered_saved_passage_choices(
            control, navigation,
            ({"copy_ref": late, "passage_refs": current_window["passage_refs"]},),
        )
        assert projected["read_choice_projection"] == "ora.saved_passage_choices.paged.v1"
        assert projected["paged_read_choice_count"] == 1
        assert projected["read_choices_from_saved_result"] == [
            {"source_ref": late, "read": not_covered}
        ]
        assert projected["read_full_saved_listing"] == navigation["read_full_saved_listing"]
        assert host.context._page_covered_saved_passage_choices(
            control, navigation,
            ({"copy_ref": early, "passage_refs": []},),
        ) == navigation
    finally:
        host.close()


def test_richer_source_packet_needs_usable_pending_peer_answer(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    saved = _fixture(store)
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: None)
    try:
        control = host.store.get("mission_task_control", "requester-control")
        request = host.store.get(**saved["request_ref"])
        answer = host.store.get(**saved["answer_ref"])
        cue = host.context._peer_answer_review_cue(
            request, answer, mission_id="m", actor_id="requester",
            task_id="requester-task", inclusion_id="synthetic-inclusion",
        )
        assert cue is not None
        fit = {
            "peer_answer_inline_refs": (), "peer_answer_cue_views": (),
            "attached_tool_ids": ("request_capability",),
            "permitted_tool_ids": ("read_context_record", "review_peer_answer"),
            "incoming_request_view": None,
        }
        assert not host._pending_peer_input_preserved(control, fit)
        with_cue = {**fit, "peer_answer_cue_views": (cue,)}
        assert host._pending_peer_input_preserved(control, with_cue)
        assert not host._pending_peer_input_preserved(
            control, {**with_cue, "permitted_tool_ids": ("review_peer_answer",)},
        )
        assert not host._pending_peer_input_preserved(
            control, {**with_cue, "permitted_tool_ids": ("read_context_record",)},
        )
        assert host._pending_peer_input_preserved(control, {
            **fit, "peer_answer_inline_refs": (canonical(saved["answer_ref"]),),
        })
    finally:
        host.close()


def test_richer_source_packet_needs_exact_incoming_question_route(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    _fixture(store)
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: None)
    try:
        control = host.store.get("mission_task_control", "requester-control")
        question = "Synthetic incoming review question."

        def save(tx):
            return ref(tx.put("mail_request", "synthetic-incoming", {
                "mission_id": "m", "recipient_id": "requester",
                "requester_id": "other", "status": "unanswered",
                "delivered_at": "2026-10-31T00:00:00Z",
                "created_at": "2026-10-31T00:00:00Z", "question": question,
            }))

        pointer = host.store.transact("synthetic-incoming", {}, save, mission_id="m")
        fit = {
            "peer_answer_inline_refs": (), "peer_answer_cue_views": (),
            "attached_tool_ids": ("request_capability",),
            "permitted_tool_ids": ("read_context_record", "review_peer_answer"),
            "incoming_request_view": {"ref": pointer, "question": question},
        }
        # The separately pending answer must also remain reviewable.
        assert not host._pending_peer_input_preserved(control, fit)
        request = host.store.get("mail_request", "peer-preview-request")
        answer = host.store.get("mail_answer", "peer-preview-answer")
        cue = host.context._peer_answer_review_cue(
            request, answer, mission_id="m", actor_id="requester",
            task_id="requester-task", inclusion_id="synthetic-inclusion",
        )
        ready = {**fit, "peer_answer_cue_views": (cue,)}
        assert host._pending_peer_input_preserved(control, ready)
        assert not host._pending_peer_input_preserved(
            control, {**ready, "incoming_request_view": None},
        )
        assert not host._pending_peer_input_preserved(
            control, {**ready, "incoming_request_view": {
                "ref": pointer, "question_window": question[:10],
                "question_chars": len(question), "read_full_question": "",
            }},
        )
    finally:
        host.close()


@pytest.mark.parametrize("boundary", [
    "unselected_older", "working_choice", "revoked_older", "newer_bad_read",
    "missing_window", "version_changed",
])
def test_older_group_rejects_invalid_lineage_or_current_use(tmp_path, monkeypatch, boundary):
    host, run, actor, early, late, older_window, _ = _two_copy_read(
        tmp_path, monkeypatch, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        _current_window, current_response = _read_second(host, control, late)
        control = host._control(run.run_id, actor)
        assert _candidate(host, control) is not None
        if boundary == "unselected_older":
            host._update_control(control, selected_passage_refs={
                **control["body"]["selected_passage_refs"], early["id"]: [],
            })
        elif boundary == "working_choice":
            save_working_sources(
                host.store, "synthetic-multi-working-choice", control_ref=ref(control),
                response_ref=current_response, selected_source_refs=[late],
            )
        elif boundary == "revoked_older":
            use_ref = host.context._source_status(host.store.get(**early))[
                "current_source_use_ref"
            ]

            def revoke(tx):
                previous = tx.get(**use_ref)
                return ref(tx.put(previous["kind"], previous["id"], {
                    **previous["body"], "allowed_uses": [],
                    "policy_status": "requires_source_policy_resolution",
                }, expected_version=previous["version"]))

            host.store.transact("synthetic-multi-revoke", {}, revoke,
                                mission_id=control["body"]["mission_id"])
        elif boundary == "newer_bad_read":
            bad, _ = _saved_tool_request(
                host, control, response_id="synthetic-multi-bad-older-response",
                tool_id="synthetic-multi-bad-older-tool", name="read_source_window",
                arguments={"source_id": early["id"], "source_version": early["version"]},
                result={"status": "rejected"},
            )
            host._update_control(control, response_ref=bad,
                                 tool_turn_refs=[*control["body"]["tool_turn_refs"], bad])
            # A newer read terminates the current-window continuity route.
            assert _candidate(host, host._control(run.run_id, actor)) is None
            return
        elif boundary == "missing_window":
            host._update_control(control, read_window_refs=[
                pointer for pointer in control["body"]["read_window_refs"]
                if pointer != older_window["window_ref"]
            ])
        elif boundary == "version_changed":
            original = host.store.get(**early)

            def revise(tx):
                return ref(tx.put("source_copy", original["id"], original["body"],
                                  expected_version=original["version"]))

            host.store.transact("synthetic-multi-version-change", {}, revise,
                                mission_id=control["body"]["mission_id"])
        assert _candidate(host, host._control(run.run_id, actor)) is None
    finally:
        host.close()
