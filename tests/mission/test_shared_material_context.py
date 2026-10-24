"""Shared peer pointers stay scoped, pageable and separate from source text."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextError
from ora.mission.report_lineage import report_text_access
from ora.models.session import SessionError
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import _search_spec, answer, serialized_peer_name, service, tool


def _intent(host, control):
    call = host.store.get(**control["body"]["call_ref"])
    return host.store.get(
        "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
    )


def _index(host, intent):
    packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
    return packet, json.loads(packet["messages"][-1]["content"])


def test_saved_shared_catalog_without_read_request_remains_dispatchable(tmp_path, monkeypatch):
    from ora.mission import context

    host = service(tmp_path, monkeypatch, lambda _request, **_kw: answer("Synthetic memo.", 1))
    try:
        run = host.start(LiveStartRequest(
            request_id="legacy-shared-reader", question="Read a saved peer memo",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        assert host._schedule_model(host._control(run.run_id, "agent-critique"))
        asyncio.run(host.executor.run_ready(host._run(run.run_id)["body"]["mission_id"]))
        assert host._advance(host._control(run.run_id, "agent-critique"))
        with monkeypatch.context() as old:
            overview = context._shared_material_overview
            old.setattr(context, "_shared_material_overview",
                        lambda entry, _inclusion=None: overview(entry))
            old.setattr(context, "_shared_material_reader_view",
                        lambda entry, _inclusion: context.shared_material_view(entry))
            assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        intent = _intent(host, host._control(run.run_id, "agent-synthesis"))
        before = host.store.read_blob(intent["body"]["blob"]["sha256"])
        _, index = _index(host, intent)
        assert "read_request" not in index["shared_materials"]["entries"][0]
        host.session.load(ref(intent), for_dispatch=True)
        assert host.store.read_blob(intent["body"]["blob"]["sha256"]) == before
    finally:
        host.close()


@pytest.mark.parametrize("compact", [False, True])
def test_peer_accepted_report_is_indexed_then_read_from_exact_inclusion(tmp_path, monkeypatch, compact):
    if compact:
        monkeypatch.setattr("ora.mission.context.MAX_SHARED_MATERIAL_PREVIEWS", 0)
    host = service(tmp_path, monkeypatch, lambda _request, **_kw: answer(
        "Synthetic peer finding; methods remain uncertain.", 1,
    ))
    try:
        run = host.start(LiveStartRequest(
            request_id="shared-peer-report", question="Compare peer findings",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        critique = host._control(run.run_id, "agent-critique")
        assert host._schedule_model(critique)
        asyncio.run(host.executor.run_ready(host._run(run.run_id)["body"]["mission_id"]))
        assert host._advance(host._control(run.run_id, "agent-critique"))
        report = host.store.records("report")[0]
        sage = host._control(run.run_id, "agent-synthesis")
        assert sage["body"]["source_refs"] == []
        assert host._schedule_model(sage)
        prepared = host._control(run.run_id, "agent-synthesis")
        intent = _intent(host, prepared)
        packet, index = _index(host, intent)
        shared = index["shared_materials"]
        entries = shared.get("entries") or [shared["overview"]]
        report_entry = next(item for item in entries if item["ref"] == ref(report))
        if compact:
            assert "entries" not in shared and "read" not in report_entry
        assert report_entry["type"] == "shared_accepted_report"
        assert report_entry["title"] == report["body"]["title"][:160]
        read_request = report_entry["read_request"]
        assert read_request == {
            "tool": "read_context_record", "inclusion_id": f"context:{intent['id']}",
            **ref(report), "offset": 0, "limit": 2000,
        }
        assert report["body"]["body"] not in json.dumps(packet["messages"])
        assert index["record_counts"]["shared_materials"] >= 1
        scope = prepared["body"]
        inclusion_ref = {"kind": "context_inclusion", "id": f"context:{intent['id']}",
                         "version": 1}
        page = host.context.read_omitted_page(
            mission_id=scope["mission_id"], actor_id=scope["actor_id"],
            task_id=scope["task_id"], inclusion_ref=inclusion_ref, cursor=0, limit=20,
        )
        item = next(item for item in page["items"] if item["ref"] == ref(report))
        assert item["type"] == "shared_material"
        assert item["summary"]["title"]["text"] == report["body"]["title"][:160]
        assert item["summary"]["read_request"] == read_request
        window = host.context.read_omitted_record(
            mission_id=scope["mission_id"], actor_id=scope["actor_id"],
            task_id=scope["task_id"], inclusion_ref=inclusion_ref,
            pointer={key: read_request[key] for key in ("kind", "id", "version")},
            offset=read_request["offset"], limit=read_request["limit"],
        )
        assert window["report_text_status"] == "available"
        assert report["body"]["body"] in window["text"]
        with pytest.raises(ContextError, match="context index"):
            host.context.read_omitted_record(
                mission_id=scope["mission_id"], actor_id=scope["actor_id"],
                task_id=scope["task_id"],
                inclusion_ref={**inclusion_ref, "id": report["id"]},
                pointer=ref(report), offset=0, limit=2000,
            )
        host.session.load(ref(intent), for_dispatch=True)
        host._update_control(host._control(run.run_id, "agent-critique"), report_ref=None)
        with pytest.raises(SessionError):
            host.session.load(ref(intent), for_dispatch=True)
        changed_page = host.context.read_omitted_page(
            mission_id=scope["mission_id"], actor_id=scope["actor_id"],
            task_id=scope["task_id"], inclusion_ref=inclusion_ref, cursor=0, limit=20,
        )
        assert next(item for item in changed_page["items"] if item["ref"] == ref(report))[
            "summary"
        ]["status"] == "shared_material_unavailable"
        with pytest.raises(ContextError):
            host.context.read_omitted_record(
                mission_id=scope["mission_id"], actor_id="other-actor",
                task_id=scope["task_id"], inclusion_ref=inclusion_ref,
                pointer=ref(report), offset=0, limit=2000,
            )
        foreign = host.start(LiveStartRequest(
            request_id="foreign-peer-report", question="A separate mission",
            agent_ids=["agent-synthesis"],
        ))
        assert host._schedule_model(host._control(foreign.run_id, "agent-synthesis"))
        asyncio.run(host.executor.run_ready(host._run(foreign.run_id)["body"]["mission_id"]))
        assert host._advance(host._control(foreign.run_id, "agent-synthesis"))
        foreign_report = next(item for item in host.store.records("report")
                              if item["body"]["mission_id"]
                              == host._run(foreign.run_id)["body"]["mission_id"])
        with pytest.raises(ContextError):
            host.context.read_omitted_record(
                mission_id=scope["mission_id"], actor_id=scope["actor_id"],
                task_id=scope["task_id"], inclusion_ref=inclusion_ref,
                pointer=ref(foreign_report), offset=0, limit=2000,
            )
    finally:
        host.close()


@pytest.mark.parametrize("already_checked", [False, True])
def test_shared_hit_metadata_is_pageable_without_auto_source_text(
    tmp_path, monkeypatch, already_checked,
):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: pytest.fail(
        "Shared hit changed before any provider transport"))
    host.discovery_transport = lambda *_a, **_kw: HTTPResult(200, json.dumps({
        "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
            "source": "MED", "id": "123", "pmid": "123",
            "title": "Synthetic peer lead", "abstractText": "Private synthetic abstract lead text.",
        }]},
    }).encode(), {})
    try:
        run = host.start(LiveStartRequest(
            request_id="shared-peer-hit", question="Inspect peer leads",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        spec = _search_spec(host, run.run_id, suffix="peer-hit")
        critique = host._control(run.run_id, "agent-critique")
        spec = replace(spec, actor_id=critique["body"]["actor_id"],
                       proposal_ref=critique["body"]["proposal_ref"])
        found, _ = host._search(spec, {"route": "europe_pmc.search",
                                       "query": "synthetic", "limit": 1})
        assert found["hits"]
        sage = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(sage)
        intent = _intent(host, host._control(run.run_id, "agent-synthesis"))
        packet, index = _index(host, intent)
        hit_id = found["hits"][0]["hit_id"]
        assert index["record_counts"]["shared_materials"] == 1
        assert any(item["ref"]["id"] == hit_id for item in index["shared_materials"]["entries"])
        assert "Private synthetic abstract lead text" not in json.dumps(packet["messages"])
        assert not host.store.records("source_copy")
        actual = {item["function"]["name"] for item in packet["tools"]
                  if item.get("type") == "function"}
        notice = next(json.loads(item["content"]) for item in packet["messages"]
                      if item.get("role") == "user"
                      and "trusted_host_attached_tool_availability" in item["content"])
        assert set(notice["call_now_local_functions"]) == actual
        assert notice["native_browser_now"] is False
        host.session.load(ref(intent), for_dispatch=True)
        prior_copy = None
        if already_checked:
            inspected, outputs = host._inspect_hit(
                replace(spec, work_id="offline-permitted-peer-inspection"),
                {"hit_id": hit_id},
            )
            assert inspected["status"] == "inspected" and len(outputs) == 1
            prior_copy = outputs[0]
            # New, still-permitted checked material must not invalidate a
            # previously accurate saved hit lead.
            host.session.load(ref(intent), for_dispatch=True)
        original_load = host.session.load
        late = []

        def load_then_change(pointer, *, for_dispatch=False):
            prepared = original_load(pointer, for_dispatch=for_dispatch)
            if for_dispatch and not late:
                if prior_copy is None:
                    inspected, outputs = host._inspect_hit(
                        replace(spec, work_id="offline-late-peer-inspection"),
                        {"hit_id": hit_id},
                    )
                    assert inspected["status"] == "inspected" and len(outputs) == 1
                    copy_ref = outputs[0]
                else:
                    copy_ref = prior_copy
                use = host.store.resolve(
                    "source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}",
                )
                host.store.transact(
                    "revoke-late-shared-hit", {},
                    lambda tx: tx.put("source_use", use["id"], {
                        **use["body"], "policy_status": "revoked", "allowed_uses": [],
                    }, expected_version=use["version"]),
                    mission_id=sage["body"]["mission_id"],
                )
                late.append(copy_ref)
            return prepared

        host.session.load = load_then_change
        with pytest.raises(SessionError):
            host.session.invoke_capture(ref(intent))
        assert late and host.store.records("model_dispatch_attempt") == []
        assert host.store.records("model_response_receipt") == []
        inclusion_ref = {"kind": "context_inclusion", "id": f"context:{intent['id']}",
                         "version": 1}
        page = host.context.read_omitted_page(
            mission_id=sage["body"]["mission_id"], actor_id=sage["body"]["actor_id"],
            task_id=sage["body"]["task_id"], inclusion_ref=inclusion_ref,
            cursor=0, limit=20,
        )
        assert page["items"][0]["summary"]["status"] == "shared_material_unavailable"
    finally:
        host.close()


def test_source_backed_peer_title_pins_use_and_revocation_withholds_read(tmp_path, monkeypatch):
    packet_text = "Synthetic source says the randomized arms had uncertain comparative outcomes. " * 4
    calls = []
    report_pointer = None

    def model(request, *, timeout):
        packet = json.loads(request.data)
        calls.append(packet)
        if serialized_peer_name(packet) == "Mxra":
            assert packet_text[:80] in request.data.decode()
            return answer("Synthetic source-linked peer conclusion.", len(calls))
        index = json.loads(packet["messages"][-1]["content"])
        entry = next(item for item in index["shared_materials"]["entries"]
                     if item["ref"] == report_pointer)
        read_request = dict(entry["read_request"])
        return tool(read_request.pop("tool"), read_request, len(calls))

    host = service(tmp_path, monkeypatch, model)
    host.discovery_transport = lambda *_a, **_kw: HTTPResult(200, json.dumps({
        "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
            "source": "MED", "id": "456", "pmid": "456",
            "title": "Synthetic sourced peer lead", "abstractText": packet_text,
        }]},
    }).encode(), {})
    try:
        run = host.start(LiveStartRequest(
            request_id="shared-source-report", question="Review methods and compare peers",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        critique = host._control(run.run_id, "agent-critique")
        spec = replace(_search_spec(host, run.run_id, suffix="sourced-peer"),
                       actor_id=critique["body"]["actor_id"],
                       proposal_ref=critique["body"]["proposal_ref"])
        found, _ = host._search(spec, {"route": "europe_pmc.search",
                                       "query": "synthetic", "limit": 1})
        inspected, outputs = host._inspect_hit(spec, {"hit_id": found["hits"][0]["hit_id"]})
        assert inspected["status"] == "inspected" and len(outputs) == 1
        copy_ref = outputs[0]
        host._update_control(critique, source_refs=[copy_ref])
        assert host._schedule_model(host._control(run.run_id, "agent-critique"))
        asyncio.run(host.executor.run_ready(host._run(run.run_id)["body"]["mission_id"]))
        assert host._advance(host._control(run.run_id, "agent-critique"))
        report = host.store.records("report")[0]
        report_pointer = ref(report)
        assert report_text_access(host.store, report)["source_dependency_refs"] == [copy_ref]
        # The producing peer later retains an unrelated B copy whose use is
        # revoked. Exact report A remains readable to a sibling without B.
        mission_id = host._run(run.run_id)["body"]["mission_id"]

        def unrelated(tx):
            other = tx.put("source_copy", "unrelated-peer-copy-b", {
                "mission_id": mission_id, "rights_status": "permitted",
            })
            use = tx.put("source_use", "unrelated-peer-use-b", {
                "copy_ref": ref(other), "allowed_uses": [],
                "policy_status": "revoked",
            })
            tx.bind("source_use.copy", f"{other['id']}:{other['version']}",
                    "source_use", use["id"])
            return {"copy": ref(other)}

        copy_b = host.store.transact("unrelated-peer-source-b", {}, unrelated,
                                     mission_id=mission_id)["copy"]
        host._update_control(host._control(run.run_id, "agent-critique"),
                             source_refs=[copy_ref, copy_b])
        assert report_text_access(host.store, report)["status"] == "available"
        sage = host._control(run.run_id, "agent-synthesis")
        assert sage["body"]["source_refs"] == []
        assert host._schedule_model(sage)
        prepared = host._control(run.run_id, "agent-synthesis")
        intent = _intent(host, prepared)
        assert copy_ref in intent["body"]["context_source_dependency_refs"]
        packet, index = _index(host, intent)
        assert report["body"]["title"] in json.dumps(packet["messages"])
        assert report["body"]["body"] not in json.dumps(packet["messages"])
        host.session.load(ref(intent), for_dispatch=True)
        asyncio.run(host.executor.run_ready(host._run(run.run_id)["body"]["mission_id"]))
        assert host._advance(host._control(run.run_id, "agent-synthesis"))
        assert host._schedule_tool(host._control(run.run_id, "agent-synthesis"))
        asyncio.run(host.executor.run_ready(host._run(run.run_id)["body"]["mission_id"]))
        assert host._advance(host._control(run.run_id, "agent-synthesis"))
        saved_results = host.store.records("context_tool_result")
        assert any(report["body"]["body"] in host.store.read_blob(
            result["body"]["blob"]["sha256"]
        ).decode() for result in saved_results)
        use = host.store.resolve(
            "source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}",
        )
        host.store.transact(
            "revoke-shared-title-source", {},
            lambda tx: tx.put("source_use", use["id"], {
                **use["body"], "policy_status": "revoked", "allowed_uses": [],
            }, expected_version=use["version"]),
            mission_id=prepared["body"]["mission_id"],
        )
        before_copies = len(host.store.records("source_copy"))
        blocked, new_outputs = host._inspect_hit(
            replace(spec, work_id="offline-reinspect-revoked-hit"),
            {"hit_id": found["hits"][0]["hit_id"]},
        )
        assert blocked["status"] == "source_unavailable" and new_outputs == ()
        assert len(host.store.records("source_copy")) == before_copies
        assert not host._has_inspectable_hit(
            host._control(run.run_id, "agent-synthesis")
        )
        with pytest.raises(SessionError):
            host.session.load(ref(intent), for_dispatch=True)
        scope = prepared["body"]
        inclusion_ref = {"kind": "context_inclusion", "id": f"context:{intent['id']}",
                         "version": 1}
        page = host.context.read_omitted_page(
            mission_id=scope["mission_id"], actor_id=scope["actor_id"],
            task_id=scope["task_id"], inclusion_ref=inclusion_ref, cursor=0, limit=20,
        )
        item = next(item for item in page["items"] if item["ref"] == ref(report))
        assert item["summary"]["status"] == "accepted_text_withheld"
        assert item["summary"]["title"] is None
        window = host.context.read_omitted_record(
            mission_id=scope["mission_id"], actor_id=scope["actor_id"],
            task_id=scope["task_id"], inclusion_ref=inclusion_ref,
            pointer=ref(report), offset=0, limit=2000,
        )
        assert window["report_text_status"] != "available"
        assert report["body"]["body"] not in window["text"]
        assert report["body"]["title"] not in window["text"]
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        replay_intent = _intent(host, host._control(run.run_id, "agent-synthesis"))
        replay_packet, _ = _index(host, replay_intent)
        assert report["body"]["body"] not in json.dumps(replay_packet["messages"])
        assert report["body"]["title"] not in json.dumps(replay_packet["messages"])
    finally:
        host.close()


def test_many_peer_hits_keep_count_and_every_exact_page_without_text(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: pytest.fail(
        "Only request preparation is expected"))
    batches = []

    def discovery(*_args, **_kwargs):
        batch = len(batches)
        batches.append(batch)
        return HTTPResult(200, json.dumps({
            "hitCount": 8, "nextCursorMark": "*", "resultList": {"result": [
                {"source": "MED", "id": str(batch * 8 + n + 1000),
                 "pmid": str(batch * 8 + n + 1000),
                 "title": f"Synthetic peer title {batch}-{n}",
                 "abstractText": f"Withheld synthetic abstract body {batch}-{n}."}
                for n in range(8)
            ]},
        }).encode(), {})

    host.discovery_transport = discovery
    try:
        run = host.start(LiveStartRequest(
            request_id="many-shared-hits", question="Review many peer leads",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        critique = host._control(run.run_id, "agent-critique")
        for batch in range(4):
            spec = replace(_search_spec(host, run.run_id, suffix=f"many-{batch}"),
                           actor_id=critique["body"]["actor_id"],
                           proposal_ref=critique["body"]["proposal_ref"])
            found, _ = host._search(spec, {
                "route": "europe_pmc.search", "query": f"synthetic batch {batch}", "limit": 8,
            })
            assert len(found["hits"]) == 8
        sage = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(sage)
        intent = _intent(host, host._control(run.run_id, "agent-synthesis"))
        packet, index = _index(host, intent)
        assert index["record_counts"]["shared_materials"] == 32
        assert len(index.get("shared_materials", {}).get("entries", [])) <= 6
        assert index["omitted"]["count"] >= 32
        assert "Withheld synthetic abstract body" not in json.dumps(packet["messages"])
        scope = sage["body"]
        inclusion = {"kind": "context_inclusion", "id": f"context:{intent['id']}",
                     "version": 1}
        cursor, shared = 0, []
        while True:
            page = host.context.read_omitted_page(
                mission_id=scope["mission_id"], actor_id=scope["actor_id"],
                task_id=scope["task_id"], inclusion_ref=inclusion,
                cursor=cursor, limit=20,
            )
            shared.extend(item for item in page["items"] if item["type"] == "shared_material")
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]
        assert len(shared) == 32
        assert len({item["ref"]["id"] for item in shared}) == 32
        assert all("Withheld synthetic abstract body" not in json.dumps(item) for item in shared)
        with pytest.raises(ContextError):
            host.context.read_omitted_record(
                mission_id=scope["mission_id"], actor_id=scope["actor_id"],
                task_id=scope["task_id"], inclusion_ref=inclusion,
                pointer={"kind": "retrieval_hit", "id": "unrelated-forged-hit", "version": 1},
                offset=0, limit=2000,
            )
    finally:
        host.close()


def test_sibling_selects_shared_hit_then_checked_text_enters_next_input(tmp_path, monkeypatch):
    abstract = "Synthetic peer abstract on randomized care arms and uncertain outcomes. " * 3
    requests = []
    hit_id = None

    def model(request, *, timeout):
        requests.append(json.loads(request.data))
        return tool("inspect_hit", {"hit_id": hit_id}, len(requests))

    host = service(tmp_path, monkeypatch, model)
    host.discovery_transport = lambda *_a, **_kw: HTTPResult(200, json.dumps({
        "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
            "source": "MED", "id": "789", "pmid": "789",
            "title": "Synthetic source-bearing peer hit", "abstractText": abstract,
        }]},
    }).encode(), {})
    try:
        run = host.start(LiveStartRequest(
            request_id="sibling-checked-hit", question="Read a peer's candidate source",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        critique = host._control(run.run_id, "agent-critique")
        spec = replace(_search_spec(host, run.run_id, suffix="sibling-checked"),
                       actor_id=critique["body"]["actor_id"],
                       proposal_ref=critique["body"]["proposal_ref"])
        found, _ = host._search(spec, {
            "route": "europe_pmc.search", "query": "synthetic", "limit": 1,
        })
        hit_id = found["hits"][0]["hit_id"]
        sage = host._control(run.run_id, "agent-synthesis")
        assert sage["body"]["source_refs"] == []
        assert host._schedule_model(sage)
        mission_id = host._run(run.run_id)["body"]["mission_id"]
        asyncio.run(host.executor.run_ready(mission_id))
        assert host._advance(host._control(run.run_id, "agent-synthesis"))
        assert host._schedule_tool(host._control(run.run_id, "agent-synthesis"))
        asyncio.run(host.executor.run_ready(mission_id))
        assert host._advance(host._control(run.run_id, "agent-synthesis"))
        sage = host._control(run.run_id, "agent-synthesis")
        assert len(sage["body"]["source_refs"]) == 1
        assert len(requests) == 1
        assert abstract[:70] not in json.dumps(requests[0]["messages"])
        assert host._schedule_model(sage)
        intent = _intent(host, host._control(run.run_id, "agent-synthesis"))
        packet, _ = _index(host, intent)
        assert abstract[:70] in json.dumps(packet["messages"])
        assert intent["body"]["source_refs"] == sage["body"]["source_refs"]
    finally:
        host.close()


def test_saved_shared_hit_page_replay_withholds_revoked_title_and_fetch(tmp_path, monkeypatch):
    title = "Synthetic peer hit title withheld after policy change"
    requests = []

    def model(request, *, timeout):
        packet = json.loads(request.data)
        requests.append(packet)
        index = json.loads(packet["messages"][-1]["content"])
        return tool("read_context_page", {
            "inclusion_id": index["older_records"]["inclusion_id"],
            "cursor": 0, "limit": 20,
        }, len(requests))

    host = service(tmp_path, monkeypatch, model)
    host.discovery_transport = lambda *_a, **_kw: HTTPResult(200, json.dumps({
        "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
            "source": "MED", "id": "900", "pmid": "900", "title": title,
            "abstractText": "Synthetic retained abstract text for a peer hit.",
        }]},
    }).encode(), {})
    try:
        run = host.start(LiveStartRequest(
            request_id="shared-hit-page-replay", question="Review a peer's saved hit",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        critique = host._control(run.run_id, "agent-critique")
        spec = replace(_search_spec(host, run.run_id, suffix="page-replay"),
                       actor_id=critique["body"]["actor_id"],
                       proposal_ref=critique["body"]["proposal_ref"])
        found, _ = host._search(spec, {"route": "europe_pmc.search",
                                       "query": "synthetic", "limit": 1})
        hit_id = found["hits"][0]["hit_id"]
        sage = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(sage)
        mission_id = host._run(run.run_id)["body"]["mission_id"]
        asyncio.run(host.executor.run_ready(mission_id))
        assert host._advance(host._control(run.run_id, "agent-synthesis"))
        assert host._schedule_tool(host._control(run.run_id, "agent-synthesis"))
        asyncio.run(host.executor.run_ready(mission_id))
        assert host._advance(host._control(run.run_id, "agent-synthesis"))
        saved_result = host.store.records("context_tool_result")[-1]
        saved_bytes = host.store.read_blob(saved_result["body"]["blob"]["sha256"])
        assert title in saved_bytes.decode()
        inspected, outputs = host._inspect_hit(
            replace(spec, work_id="offline-page-hit-inspection"), {"hit_id": hit_id},
        )
        assert inspected["status"] == "inspected" and len(outputs) == 1
        copy_ref = outputs[0]
        use = host.store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
        host.store.transact(
            "revoke-page-hit", {},
            lambda tx: tx.put("source_use", use["id"], {
                **use["body"], "policy_status": "revoked", "allowed_uses": [],
            }, expected_version=use["version"]),
            mission_id=mission_id,
        )
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        next_intent = _intent(host, host._control(run.run_id, "agent-synthesis"))
        packet, _ = _index(host, next_intent)
        text = json.dumps(packet["messages"])
        assert title not in text
        assert "shared_material_unavailable" in text
        assert host.store.read_blob(saved_result["body"]["blob"]["sha256"]) == saved_bytes
    finally:
        host.close()


def test_attached_tool_notice_matches_local_and_native_manifests(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: pytest.fail(
        "Only exact request preparation is expected"))
    try:
        for suffix, native in (("local", False), ("native", True)):
            run = host.start(LiveStartRequest(
                request_id=f"attached-notice-{suffix}", question="Check available tools",
                agent_ids=["agent-synthesis"],
            ))
            control = host._control(run.run_id, "agent-synthesis")
            if native:
                host._update_control(control, requested_capabilities=["browser_search"])
                control = host._control(run.run_id, "agent-synthesis")
            assert host._schedule_model(control)
            intent = _intent(host, host._control(run.run_id, "agent-synthesis"))
            packet, _ = _index(host, intent)
            notices = [json.loads(item["content"]) for item in packet["messages"]
                       if item.get("role") == "user"
                       and "trusted_host_attached_tool_availability" in item["content"]]
            assert len(notices) == 1
            attached_local = {item["function"]["name"] for item in packet["tools"]
                              if item.get("type") == "function"}
            attached_native = any(item.get("type") == "browser_search"
                                  for item in packet["tools"])
            assert set(notices[0]["call_now_local_functions"]) == attached_local
            assert notices[0]["native_browser_now"] is attached_native
            assert attached_native is native
            if native:
                assert attached_local == set()
                assert notices[0]["request_first_local_functions"] == []
                assert notices[0]["local_functions_resume_after_native_turn"] is True
            else:
                assert "board_read" in attached_local
                assert "write_note" in notices[0]["request_first_local_functions"]
                assert not {"activate_skill", "release_skill", "release_capability"} & set(
                    notices[0]["request_first_local_functions"]
                )
                assert "inspect_hit" not in notices[0]["request_first_local_functions"]
                assert notices[0]["not_ready_local_functions"] == {
                    "inspect_hit": "Get an eligible saved search result first.",
                    "modify_plan": "Create a plan with save_plan before modifying it.",
                    "inspect_figure": "Vision is not authorized for this run. Use local text/OCR; "
                    "a new run can explicitly set allowVision=true.",
                }
                assert not ({"claim_work", "subscribe"} & set(
                    notices[0]["request_first_local_functions"]
                ))
    finally:
        host.close()
