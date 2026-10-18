"""Current scope and saved reading choices survive a normal model handoff."""

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.evidence.intake import IntakeService
from ora.evidence.library import EvidenceLibrary
from ora.models.session import SessionError
from tests.evidence import test_document_ocr as document_fixture
from tests.mission.test_service import answer, drain, service, tool


def _index(packet):
    return next(json.loads(message["content"])["run_scope"]
                for message in packet["messages"]
                if message.get("role") == "user"
                and '"run_scope"' in message.get("content", ""))


def test_full_current_question_and_short_scope_reach_serialized_request(tmp_path, monkeypatch):
    question = "Explain the exact synthetic outcome and its qualification."
    scope = "Use only the selected synthetic source; say when the qualification is absent."
    packets = []

    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        visible = _index(packet)
        assert question in json.dumps(packet["messages"])
        assert (visible.get("question") == question
                or visible.get("question_in_task_objective") is True)
        assert visible["question_truncated"] is False
        assert visible["scope_preview"] == scope
        assert visible["scope_truncated"] is False
        return answer("Synthetic offline response", len(packets))

    host = service(tmp_path, monkeypatch, model)
    try:
        started = host.start(LiveStartRequest(
            request_id="scope-continuity", question=question, scope=scope,
            agent_ids=["agent-synthesis"],
        ))
        drain(host, started.run_id, limit=10)
        assert packets
    finally:
        host.close()


def test_long_scope_declares_omission_and_exact_reader(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: answer("offline", 1))
    try:
        scope = "Synthetic boundary sentence. " * 75
        started = host.start(LiveStartRequest(
            request_id="long-scope-continuity", question="Read the full current task.",
            scope=scope, agent_ids=["agent-synthesis"],
        ))
        control = host._control(started.run_id, "agent-synthesis")
        projected, omissions, run_ref, _, _ = host.context._scope_projection(
            control, "synthetic-inclusion", (),
        )
        view = projected["run_scope"]
        assert view["question"] == "Read the full current task."
        assert view["scope_truncated"] is True
        assert view["scope_chars"] == len(scope)
        assert view["scope_preview"] == scope[:1200]
        assert view["fetch_full_scope_and_source_refs"] == {
            "tool": "read_context_record", "inclusion_id": "synthetic-inclusion",
            **run_ref, "offset": 0, "limit": 2000,
        }
        assert any(item["type"] == "run_scope_detail" for item in omissions)
    finally:
        host.close()


def test_saved_text_search_navigation_and_full_saved_page(tmp_path, monkeypatch):
    packets = []
    target = {}

    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        if step == 1:
            return tool("search_saved_passages", {"query": "Table 1"}, step)
        if step == 2:
            assert not any(message.get("role") == "tool"
                           for message in packet["messages"])
            index = next(json.loads(message["content"])
                         for message in packet["messages"]
                         if message.get("role") == "user"
                         and '"latest_saved_passage_navigation"' in message.get("content", ""))
            navigation = index["latest_saved_passage_navigation"]
            assert navigation["query"] == "Table 1"
            assert navigation["status"] == "saved_lexical_navigation_not_checked_claim_evidence"
            assert navigation["coverage"] == "complete_retained_extracted_text"
            assert navigation["read_choices_from_saved_result"]
            action = navigation["read_choices_from_saved_result"][0]["read"]
            assert action["tool"] == "read_source_window"
            target["action"] = action
            assert navigation["read_full_saved_listing"]["tool"] == "read_tool_result"
            target["result_id"] = navigation["read_full_saved_listing"]["result_id"]
            return tool("read_tool_result", {
                key: value for key, value in navigation["read_full_saved_listing"].items()
                if key != "tool"
            }, step)
        page = next(json.loads(message["content"])
                    for message in reversed(packet["messages"])
                    if message.get("role") == "tool")
        assert page["schema"] == "ora.saved_tool_result_window.v1"
        assert '"block_offset"' in page["text"]
        return answer("Navigation retained", step)

    host = service(tmp_path, monkeypatch, model)
    try:
        started = host.start(LiveStartRequest(
            request_id="saved-search-continuity", question="Find Table 1.",
            scope="Keep the exact table location.", agent_ids=["agent-synthesis"],
        ))
        control = host._control(started.run_id, "agent-synthesis")
        scope = {key: control["body"][key] for key in
                 ("mission_id", "run_id", "actor_id", "task_id")}
        monkeypatch.setattr(document_fixture, "SCOPE", scope)
        html = (b"<html><body><p>Overview.</p><h2>Findings</h2>"
                + b"".join(f"<p>Table 1 row {number} records a synthetic value.</p>".encode()
                           for number in range(5)) + b"</body></html>")
        document = document_fixture.saved_public_pdf(
            host.store, html, content_type="text/html",
            url="https://example.org/synthetic-search",
        )
        inspected = IntakeService(EvidenceLibrary(host.store)).inspect_public_document(
            "inspect-synthetic-search", document, **scope,
        )
        assert inspected["model_input_status"] == "permitted"
        host._update_control(control, source_refs=[inspected["copy_ref"]])
        host._update_control(host._control(started.run_id, "agent-synthesis"),
            input_budget={"observed_limit_tokens": 8000, "target_fraction": 1.0})
        drain(host, started.run_id, limit=15)
        assert len(packets) >= 3
        assert target["action"]["source_id"] == inspected["copy_ref"]["id"]
        control = host._control(started.run_id, "agent-synthesis")
        search_response = control["body"]["tool_turn_refs"][0]
        exchange, compaction = host.context._tool_exchange(search_response)
        immediate = json.loads(exchange[-1]["content"])
        assert immediate["schema"] == "ora.saved_tool_result_window.v1"
        assert immediate["next_request"] is not None
        assert compaction["status"] == "saved_result_window_rechecked"
        nav_intent = next(item for item in host.store.records("model_request_intent")
                          if b'latest_saved_passage_navigation' in host.store.read_blob(
                              item["body"]["blob"]["sha256"]))
        assert inspected["copy_ref"] in nav_intent["body"]["context_source_dependency_refs"]
        # A producing input can carry another permitted source that the saved
        # lexical search did not scan. Preserve that transitive dependency too.
        source = host.store.get(**inspected["copy_ref"])
        source_use = host.store.resolve(
            "source_use.copy",
            f"{inspected['copy_ref']['id']}:{inspected['copy_ref']['version']}",
        )

        def synthetic_producer(tx):
            copy = tx.put("source_copy", "synthetic-producer-only-copy", source["body"])
            use = tx.put("source_use", "synthetic-producer-only-use",
                {**source_use["body"], "copy_ref": ref(copy)})
            tx.bind("source_use.copy", f"{copy['id']}:{copy['version']}",
                    "source_use", use["id"])
            return ref(copy)

        second_ref = host.store.transact("synthetic-producer-only-copy", {},
            synthetic_producer, mission_id=scope["mission_id"])
        saved_result, saved_payload = host.context._saved_result_scope(
            target["result_id"], mission_id=scope["mission_id"],
            actor_id=scope["actor_id"], task_id=scope["task_id"],
        )
        assert second_ref not in json.loads(saved_payload["content"])["source_dependency_refs"]
        original_dependencies = host.context.response_source_dependencies

        def producing_dependencies(pointer, *, require_permitted=True):
            refs = original_dependencies(pointer, require_permitted=require_permitted)
            return (*refs, second_ref) if pointer == search_response else refs

        monkeypatch.setattr(host.context, "response_source_dependencies", producing_dependencies)
        nav_call = host.store.get(**nav_intent["body"]["call_ref"])
        task = host.store.get("board_proposal", nav_call["body"]["task_id"])
        pack = host.context.assemble(ref(nav_call), ref(task),
            control_ref=ref(host._control(started.run_id, "agent-synthesis")),
            archived_tool_turn_refs=[search_response])
        dependencies = host.context.context_source_dependency_refs(pack)
        assert second_ref in dependencies
        producing_intent = host.store.transact("synthetic-producing-only-intent", {},
            lambda tx: ref(tx.put("model_request_intent", "synthetic-producing-only-intent",
                {**nav_intent["body"], "context_source_dependency_refs": list(dependencies)})),
            mission_id=scope["mission_id"])
        monkeypatch.setattr(host.context, "response_source_dependencies", original_dependencies)
        assert host.session.load(producing_intent, for_dispatch=True)
        producer_use = host.store.resolve(
            "source_use.copy", f"{second_ref['id']}:{second_ref['version']}",
        )
        host.store.transact("revoke-producer-only-source", {}, lambda tx: ref(tx.put(
            "source_use", producer_use["id"],
            {**producer_use["body"], "policy_status": "revoked"},
            expected_version=producer_use["version"],
        )), mission_id=scope["mission_id"])
        with pytest.raises(SessionError, match="source dependency|model-use grant"):
            host.session.load(producing_intent, for_dispatch=True)
        selected_use = host.store.resolve(
            "source_use.copy",
            f"{inspected['copy_ref']['id']}:{inspected['copy_ref']['version']}",
        )
        host.store.transact("revoke-search-continuity-source", {}, lambda tx: ref(tx.put(
            "source_use", selected_use["id"],
            {**selected_use["body"], "policy_status": "revoked"},
            expected_version=selected_use["version"],
        )), mission_id=scope["mission_id"])
        control = host._control(started.run_id, "agent-synthesis")
        assert host.context._saved_passage_navigation(
            control, mission_id=scope["mission_id"], actor_id=scope["actor_id"],
            task_id=scope["task_id"],
        ) is None
        page = host.context.read_tool_result(
            mission_id=scope["mission_id"], actor_id=scope["actor_id"],
            task_id=scope["task_id"], result_id=target["result_id"],
            offset=0, limit=1800, projection_sha256="",
        )
        assert json.loads(page["text"])["status"] == "data_unavailable_due_source_use"
        with pytest.raises(SessionError):
            host.session.load(ref(nav_intent), for_dispatch=True)
    finally:
        host.close()
