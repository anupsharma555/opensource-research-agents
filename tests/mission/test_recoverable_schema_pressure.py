"""Under input pressure, retain peer and source access while deferring defaults."""

import json

from ora.contracts.models import LiveStartRequest
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack
from tests.mission.test_peer_answer_preview import _fixture, _pack
from tests.mission.test_service import service


def _host(tmp_path, monkeypatch, *, catalog_version=2):
    def no_provider(*_args, **_kwargs):
        raise AssertionError("Packing must not dispatch a model request")

    host = service(tmp_path, monkeypatch, no_provider)
    run = host.start(LiveStartRequest(
        request_id="synthetic-recoverable-schema-pressure",
        question="Compare this synthetic source and coordinate a peer review.",
        evidence_text="Synthetic retained source passage with exact text.",
        agent_ids=["agent-synthesis", "agent-critique"],
    ))
    if catalog_version != 2:
        saved_run = host._run(run.run_id)
        host.store.transact(
            "synthetic-older-catalog-pin", {},
            lambda tx: tx.put("mission_run", saved_run["id"], {
                **saved_run["body"],
                "tool_catalog_projection_version": catalog_version,
            }, expected_version=saved_run["version"]),
            mission_id=saved_run["body"]["mission_id"],
        )
    control = host._control(run.run_id, "agent-synthesis")
    host._update_control(control, requested_capabilities=["peer_request"])
    control = host._control(run.run_id, "agent-synthesis")
    config = host._configuration(control, [])
    return host, control, config


def test_lean_source_index_keeps_selected_peer_and_exact_read_review_routes(
    tmp_path, monkeypatch,
):
    host, control, config = _host(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(
            host, "_pending_answer_tools",
            lambda *_args, **_kwargs: {"read_context_record", "review_peer_answer"},
        )
        exposed, unavailable = host._exposed_tools(
            control, config, source_recovery=True, defer_core_tools=True,
            defer_recoverable_schemas=True,
        )
        names = set(exposed)
        assert {
            "peer_request", "request_capability", "read_source_window",
            "read_source_inventory", "read_context_record", "review_peer_answer",
        } <= names
        assert not {"publish_result", "select_working_sources",
                    "release_capability"} & names
        assert {"publish_result", "select_working_sources",
                "release_capability"} <= set(config["body"]["permitted_tools"])
        assert not unavailable
    finally:
        host.close()


def test_due_incoming_question_keeps_publication_schema_under_pressure(
    tmp_path, monkeypatch,
):
    host, control, config = _host(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(
            host, "_pending_answer_tools",
            lambda *_args, **_kwargs: {"read_context_record", "review_peer_answer"},
        )
        real_heads = host._heads

        def heads(kind, mission_id):
            if kind == "mail_request":
                return [{"body": {
                    "recipient_id": control["body"]["actor_id"],
                    "delivered_at": "2026-01-01T00:00:00Z",
                    "status": "unanswered",
                }}]
            return real_heads(kind, mission_id)

        monkeypatch.setattr(host, "_heads", heads)
        exposed, _unavailable = host._exposed_tools(
            control, config, source_recovery=True, defer_core_tools=True,
            defer_recoverable_schemas=True,
        )
        assert "publish_result" in exposed
        assert "discover_capabilities" not in exposed
        assert "select_working_sources" not in exposed
        assert "review_peer_answer" in exposed
        assert "read_source_window" in exposed
    finally:
        host.close()


def test_old_id_catalog_and_explicit_selection_keep_discovery_callable(
    tmp_path, monkeypatch,
):
    host, control, config = _host(tmp_path, monkeypatch, catalog_version=1)
    try:
        saved = host._run(control["body"]["run_id"])
        assert saved["body"]["tool_catalog_projection_version"] == 1
        real_heads = host._heads

        def heads(kind, mission_id):
            if kind == "mail_request":
                return [{"body": {
                    "recipient_id": control["body"]["actor_id"],
                    "delivered_at": "2026-01-01T00:00:00Z",
                    "status": "unanswered",
                }}]
            return real_heads(kind, mission_id)

        monkeypatch.setattr(host, "_heads", heads)
        exposed, _ = host._exposed_tools(
            control, config, source_recovery=True, defer_core_tools=True,
            defer_recoverable_schemas=True,
        )
        assert "discover_capabilities" in exposed
        assert "publish_result" in exposed

        host._update_control(control, requested_capabilities=[
            "peer_request", "discover_capabilities", "select_working_sources",
        ])
        selected = host._control(control["body"]["run_id"], "agent-synthesis")
        exposed_selected, _ = host._exposed_tools(
            selected, config, source_recovery=True, defer_core_tools=True,
            defer_recoverable_schemas=True,
        )
        assert {"discover_capabilities", "select_working_sources"} <= set(
            exposed_selected
        )
    finally:
        host.close()


def test_complete_exact_answer_read_needs_current_question_and_full_pages(
    tmp_path, monkeypatch,
):
    store = StateStore(tmp_path)
    saved = _fixture(store, body="Synthetic answer text. " * 105)
    context, pack, _index = _pack(store, saved)
    inclusion_ref = bind_no_source_pack(store, context, pack, "pressure-report-read")
    pages = []
    offset = 0
    while True:
        page = context.read_omitted_record(
            mission_id="m", actor_id="requester", task_id="requester-task",
            inclusion_ref=inclusion_ref, pointer=saved["report_ref"],
            offset=offset, limit=2000,
        )
        pages.append({"role": "tool", "name": "read_context_record",
                      "content": canonical(page)})
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert len(pages) > 1
    assert json.loads(pages[-1]["content"])["report_text_status"] == "available"

    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Peer guard must not dispatch a provider")))
    try:
        control = host.store.get("mission_task_control", "requester-control")
        request = host.store.get(**saved["request_ref"])
        disposition = {
            "ref": saved["request_ref"], "answer_refs": [saved["answer_ref"]],
            "question": request["body"]["question"], "owner": "you",
            "action": "review_peer_answer", "status": "awaiting_review",
        }
        fit = {
            "peer_answer_inline_refs": (), "peer_answer_cue_views": (),
            "attached_tool_ids": ("review_peer_answer", "read_context_record"),
            "permitted_tool_ids": ("review_peer_answer", "read_context_record"),
            "incoming_request_view": None,
            "peer_answer_tool_read_messages": tuple(pages),
            "request_disposition_views": (disposition,),
        }
        assert host._pending_peer_input_preserved(control, fit)
        assert not host._pending_peer_input_preserved(
            control, {**fit, "peer_answer_tool_read_messages": tuple(pages[:1])},
        )
        assert not host._pending_peer_input_preserved(control, {
            **fit, "request_disposition_views": (
                {**disposition, "question": "Different question"},
            ),
        })
        assert not host._pending_peer_input_preserved(control, {
            **fit, "request_disposition_views": (
                {**disposition, "answer_refs": []},
            ),
        })
        assert not host._pending_peer_input_preserved(control, {
            **fit, "request_disposition_views": (
                {**disposition, "owner": "peer"},
            ),
        })
        assert not host._pending_peer_input_preserved(control, {
            **fit, "request_disposition_views": (
                {**disposition, "action": "publish_result"},
            ),
        })
        altered = json.loads(pages[0]["content"])
        altered["text"] = "X" + altered["text"][1:]
        assert not host._pending_peer_input_preserved(control, {
            **fit, "peer_answer_tool_read_messages": (
                {**pages[0], "content": canonical(altered)}, *pages[1:],
            ),
        })
    finally:
        host.close()
