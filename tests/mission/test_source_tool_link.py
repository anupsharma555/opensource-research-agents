"""Saved-source navigation is callable only for retained, permitted task text."""

from __future__ import annotations

import json

from ora.contracts.models import LiveStartRequest
from ora.mission.passage_search import task_text_tool_availability
from tests.mission.test_capability_catalog import _index
from tests.mission.test_service import service


def _prepared(host, run_id: str) -> tuple[dict, dict]:
    control = host._control(run_id, "agent-synthesis")
    assert host._schedule_model(control)
    prepared = host._control(run_id, "agent-synthesis")
    call = host.store.get(**prepared["body"]["call_ref"])
    intent = host.store.get(
        "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
    )
    packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
    availability = next(
        json.loads(message["content"])
        for message in packet["messages"]
        if message.get("role") == "user"
        and "trusted_host_attached_tool_availability" in (message.get("content") or "")
    )
    return packet, availability


def test_retained_task_text_attaches_both_exact_navigation_tools(tmp_path, monkeypatch):
    def no_http(*_args, **_kwargs):
        raise AssertionError("Preparation must not dispatch")

    host = service(tmp_path, monkeypatch, no_http)
    try:
        run = host.start(LiveStartRequest(
            request_id="retained-tool-link", question="Find a synthetic detail.",
            evidence_text="DOI: 10.1234/link. Synthetic zephyr detail is retained.",
            agent_ids=["agent-synthesis"],
        ))
        packet, availability = _prepared(host, run.run_id)
        attached = {item["function"]["name"] for item in packet["tools"]}
        assert {"search_saved_passages", "read_source_window"} <= attached
        assert {"search_saved_passages", "read_source_window"} <= set(
            availability["call_now_local_functions"]
        )
        assert not {"search_saved_passages", "read_source_window"} & set(
            availability["request_first_local_functions"]
        )
        assert len(_index(packet["messages"][0]["content"])["tools"]) == 54
        assert packet["max_completion_tokens"] == 8192
        prepared = host._control(run.run_id, "agent-synthesis")
        call = host.store.get(**prepared["body"]["call_ref"])
        config = host.store.get("agent_configuration", call["body"]["configuration_id"], 1)
        limited = {**config, "body": {**config["body"], "permitted_tools": {
            name: descriptor for name, descriptor in config["body"]["permitted_tools"].items()
            if name != "search_saved_passages"
        }}}
        exposed, _ = host._exposed_tools(prepared, limited)
        assert "search_saved_passages" not in exposed
        assert "read_source_window" in exposed

        def omitted_notice(exposed_tools):
            test_call = {**call, "body": {
                **call["body"], "exposed_tools": exposed_tools,
            }}
            history = host._model_base_history(
                prepared, [], exposed_tools, selected_source_refs=[], call=test_call,
            )
            return next(json.loads(message["content"])["next_action"]
                        for message in history if "omitted_source_refs" in message["content"])

        assert "Call attached read_source_window" in omitted_notice(
            call["body"]["exposed_tools"]
        )
        deferred = [name for name in call["body"]["exposed_tools"]
                    if name != "read_source_window"]
        assert "request_capability with tool_id=read_source_window" in omitted_notice(
            deferred
        )
    finally:
        host.close()


def test_source_free_and_revoked_text_do_not_autoattach_readers(tmp_path, monkeypatch):
    def no_http(*_args, **_kwargs):
        raise AssertionError("Preparation must not dispatch")

    for revoked in (False, True):
        state = tmp_path / ("revoked" if revoked else "source-free")
        host = service(state, monkeypatch, no_http)
        try:
            run = host.start(LiveStartRequest(
                request_id=f"unavailable-tool-link-{revoked}",
                question="Answer only from available evidence.",
                evidence_text=("DOI: 10.1234/link. Synthetic retained text."
                               if revoked else ""),
                agent_ids=["agent-synthesis"],
            ))
            control = host._control(run.run_id, "agent-synthesis")
            if revoked:
                pointer = control["body"]["source_refs"][0]
                use = host.store.resolve(
                    "source_use.copy", f"{pointer['id']}:{pointer['version']}",
                )
                host.store.transact("revoke-tool-link", {}, lambda tx: tx.put(
                    "source_use", use["id"], {
                        **use["body"], "allowed_uses": [], "policy_status": "revoked",
                    }, expected_version=use["version"],
                ), mission_id=control["body"]["mission_id"])
            packet, availability = _prepared(host, run.run_id)
            attached = {item["function"]["name"] for item in packet["tools"]}
            assert not {"search_saved_passages", "read_source_window"} & attached
            assert not {"search_saved_passages", "read_source_window"} & set(
                availability["call_now_local_functions"]
            )
            assert "request_capability" in attached
            if revoked:
                prepared = host._control(run.run_id, "agent-synthesis")
                call = host.store.get(**prepared["body"]["call_ref"])
                history = host._model_base_history(
                    prepared, [], call["body"]["exposed_tools"],
                    selected_source_refs=[], call=call,
                )
                notice = next(json.loads(message["content"])
                              for message in history
                              if "omitted_source_refs" in message["content"])
                assert "request_capability" not in notice["next_action"]
        finally:
            host.close()


def test_missing_copy_metadata_cannot_make_navigation_callable(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: None)
    try:
        run = host.start(LiveStartRequest(
            request_id="missing-tool-link", question="Synthetic no-source task.",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        missing = {"kind": "source_copy", "id": "missing-exact-copy", "version": 1}
        synthetic = {**control, "body": {
            **control["body"], "source_refs": [missing],
        }}
        assert task_text_tool_availability(host.context, synthetic) == (
            False, False
        )
    finally:
        host.close()
