"""A requester sees only a complete, permitted peer answer in its next input."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from ora.agents.capabilities import content_hash
from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.coordination.mailbox import PeerMailbox
from ora.mission.context import WorkingContext
from ora.mission.report_lineage import report_text_access
from ora.models.session import SessionError
from ora.models.token_estimate import estimate_request_tokens
from ora.retrieval.providers import HTTPResult
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack
from tests.mission.test_service import _search_spec, answer, service


def _fixture(store: StateStore, *, body: str = "A bounded synthetic peer assessment.",
             partial: bool = False, citations: list[dict] | None = None) -> dict:
    def save(tx):
        tx.put("mission_run", "run", {"mission_id": "m", "question": "Synthetic question"})
        tx.put("agent_instance", "requester", {
            "mission_id": "m", "run_id": "run", "display_name": "Requester",
        })
        tx.put("agent_instance", "responder", {
            "mission_id": "m", "run_id": "run", "display_name": "Responder",
        })
        tx.put("agent_instance", "other", {
            "mission_id": "m", "run_id": "run", "display_name": "Other peer",
        })
        task = tx.put("board_proposal", "requester-task", {
            "mission_id": "m", "status": "open", "objective": "Review the peer answer",
        })
        peer_task = tx.put("board_proposal", "responder-task", {
            "mission_id": "m", "status": "open", "objective": "Answer the request",
        })
        report = tx.put("report", "peer-report", {
            "mission_id": "m", "title": "Synthetic peer report", "body": body,
            "citations": citations if citations is not None else [],
            "limitations": ["Synthetic fixture"],
        })
        outcome = tx.put("board_outcome", "peer-outcome", {
            "mission_id": "m", "proposal_ref": ref(peer_task),
            "result_refs": [ref(report)],
        })
        tx.put("board_proposal", peer_task["id"], {
            **peer_task["body"], "status": "completed", "outcome_ref": ref(outcome),
        }, expected_version=peer_task["version"])
        tx.put("mission_task_control", "requester-control", {
            "mission_id": "m", "run_id": "run", "actor_id": "requester",
            "task_id": task["id"], "proposal_ref": ref(task), "phase": "ready_model",
        })
        tx.put("mission_task_control", "responder-control", {
            "mission_id": "m", "run_id": "run", "actor_id": "responder",
            "task_id": peer_task["id"], "report_ref": ref(report), "phase": "done",
        })
        config_body = {
            "mission_id": "m", "instance_id": "requester", "provider": "groq",
            "endpoint": "https://example.invalid", "model_id": "synthetic-model",
            "settings": {}, "reasoning_settings": {}, "instructions": "Synthetic instructions",
        }
        config_body["configuration_sha256"] = content_hash(config_body)
        config = tx.put("agent_configuration", "requester-config", config_body)
        call = tx.put("agent_call", "requester-call", {
            "mission_id": "m", "instance_id": "requester", "task_id": task["id"],
            "configuration_id": config["id"],
            "configuration_sha256": config_body["configuration_sha256"],
            "model_id": "synthetic-model",
        })
        return {"task_ref": ref(task), "peer_task_ref": ref(peer_task),
                "report_ref": ref(report),
                "call_ref": ref(call)}

    saved = store.transact("peer-preview-fixture", {"body": body, "citations": citations},
                          save, mission_id="m")
    mailbox = PeerMailbox(store)
    request = mailbox.send_request(
        "send-peer-preview-request", mission_id="m", request_id="peer-preview-request",
        message_id="peer-preview-message", requester_id="requester",
        recipient_id="responder", question="Please review this synthetic claim.",
        expected_artifact="A report", cause_ref=saved["task_ref"],
        payload={"question": "Please review this synthetic claim."},
        task_ref=saved["task_ref"],
    )
    assert request["status"] == "accepted"
    mailbox.mark_delivered("deliver-peer-preview-request", mission_id="m",
                           message_id="peer-preview-message")
    result = mailbox.record_answer(
        "answer-peer-preview-request", mission_id="m", request_id="peer-preview-request",
        answer_id="peer-preview-answer", responder_id="responder",
        artifact_ref=saved["report_ref"], partial=partial,
    )
    assert result["status"] == "accepted"
    return {**saved, "request_ref": result["request_ref"],
            "answer_ref": result["answer_ref"]}


def _pack(store: StateStore, saved: dict, *, allowance: int | None = None):
    context = WorkingContext(store)
    kwargs = ({"allowance": allowance, "estimate_tokens": lambda value: len(value)}
              if allowance is not None else {})
    control = store.get("mission_task_control", "requester-control")
    pack = context.assemble(saved["call_ref"], saved["task_ref"],
                            control_ref=ref(control), **kwargs)
    return context, pack, json.loads(pack.messages[-1]["content"])


def _other_call(store: StateStore, actor_id: str, task_ref: dict) -> dict:
    def save(tx):
        config_body = {
            "mission_id": "m", "instance_id": actor_id, "provider": "groq",
            "endpoint": "https://example.invalid", "model_id": "synthetic-model",
            "settings": {}, "reasoning_settings": {}, "instructions": "Synthetic instructions",
        }
        config_body["configuration_sha256"] = content_hash(config_body)
        config = tx.put("agent_configuration", f"{actor_id}-config", config_body)
        call = tx.put("agent_call", f"{actor_id}-call", {
            "mission_id": "m", "instance_id": actor_id, "task_id": task_ref["id"],
            "configuration_id": config["id"],
            "configuration_sha256": config_body["configuration_sha256"],
            "model_id": "synthetic-model",
        })
        return ref(call)

    return store.transact(f"{actor_id}-call", {}, save, mission_id="m")


@pytest.mark.parametrize("partial", [False, True])
def test_requester_gets_complete_exact_peer_answer_only(tmp_path, partial):
    store = StateStore(tmp_path)
    saved = _fixture(store, partial=partial)
    context, pack, index = _pack(store, saved)
    preview = index["peer_answer_previews"][0]
    assert preview["request_ref"] == saved["request_ref"]
    assert preview["answer_ref"] == saved["answer_ref"]
    assert preview["report_ref"] == saved["report_ref"]
    assert preview["authority"] == "model_authored_peer_data_not_verified_evidence"
    assert preview["question"] == "Please review this synthetic claim."
    assert preview["request_status"] == (
        "partial_awaiting_review" if partial else "awaiting_review"
    )
    assert preview["partial"] is partial
    assert preview["report"] == {
        "title": "Synthetic peer report", "body": "A bounded synthetic peer assessment.",
        "citations": [], "limitations": ["Synthetic fixture"],
    }
    assert preview["action"] == "review_peer_answer"
    assert preview["report_delivery"] == "complete_in_input"
    assert "read_request" not in preview
    assert saved["request_ref"] in pack.included_refs
    assert saved["answer_ref"] in pack.included_refs
    assert saved["report_ref"] in pack.included_refs
    assert context.context_source_dependency_refs(pack) == ()


def test_long_answer_keeps_exact_refs_without_prefix_preview(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store, body="x" * 2100)
    context, pack, index = _pack(store, saved)
    assert "peer_answer_previews" not in index
    assert saved["answer_ref"] in index["request_answer_refs"]
    assert "x" * 100 not in canonical(index)
    entry = index["shared_materials"]["entries"][0]
    assert entry["ref"] == saved["report_ref"]
    assert entry["read_request"]["tool"] == "read_context_record"
    inclusion_ref = bind_no_source_pack(store, context, pack, "long-peer-answer")
    offset, parts = 0, []
    while True:
        page = context.read_omitted_record(
            mission_id="m", actor_id="requester", task_id="requester-task",
            inclusion_ref=inclusion_ref, pointer=saved["report_ref"],
            offset=offset, limit=2000,
        )
        assert page["report_text_status"] == "available"
        parts.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert json.loads("".join(parts))["body"] == "x" * 2100


def test_answer_preview_is_not_sent_to_recipient_or_unrelated_peer(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store)
    recipient_call = _other_call(store, "responder", saved["peer_task_ref"])
    third_task = store.transact("third-task", {}, lambda tx: ref(tx.put(
        "board_proposal", "third-task", {"mission_id": "m", "status": "open"},
    )), mission_id="m")
    third_call = _other_call(store, "other", third_task)
    for call_ref, task_ref in ((recipient_call, saved["peer_task_ref"]),
                               (third_call, third_task)):
        pack = WorkingContext(store).assemble(call_ref, task_ref)
        index = json.loads(pack.messages[-1]["content"])
        assert "peer_answer_previews" not in index
        assert "A bounded synthetic peer assessment." not in canonical(index)


def test_answer_cannot_preview_a_different_peers_accepted_report(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store)
    control = store.get("mission_task_control", "responder-control")
    store.transact("different-report-producer", {}, lambda tx: tx.put(
        "mission_task_control", control["id"], {
            **control["body"], "actor_id": "other",
        }, expected_version=control["version"]), mission_id="m")
    _context, _pack_result, index = _pack(store, saved)
    assert "peer_answer_previews" not in index
    assert saved["answer_ref"] in index["request_answer_refs"]


def test_tight_allowance_keeps_answer_navigation_when_preview_drops(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store)
    _context, _pack_result, bounded = _pack(store, saved, allowance=1800)
    assert "peer_answer_previews" not in bounded
    assert bounded["peer_answer_review_cues"][0]["answer_ref"] == saved["answer_ref"]


def test_card_preserves_status_when_its_duplicate_index_refs_are_paged(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store, partial=True)
    _context, _pack_result, bounded = _pack(store, saved, allowance=2050)
    card = bounded["peer_answer_previews"][0]
    assert card["request_ref"] == saved["request_ref"]
    assert card["answer_ref"] == saved["answer_ref"]
    assert card["request_status"] == "partial_awaiting_review"
    assert card["partial"] is True
    assert saved["answer_ref"] not in bounded.get("request_answer_refs", [])
    assert bounded["older_records"]["tool"] == "read_context_page"


def test_complete_answer_precedes_optional_historical_detail(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store)
    mailbox = PeerMailbox(store)
    other = mailbox.send_request(
        "unrelated-peer-request", mission_id="m", request_id="unrelated-request",
        message_id="unrelated-message", requester_id="requester",
        recipient_id="other", question="Check a separate synthetic point.",
        expected_artifact="Separate assessment", cause_ref=saved["task_ref"],
        payload={"question": "Check a separate synthetic point."},
        task_ref=saved["task_ref"],
    )
    assert other["status"] == "accepted"
    mailbox.mark_delivered("unrelated-peer-delivery", mission_id="m",
                           message_id="unrelated-message")
    store.transact("optional-peer-dissent", {}, lambda tx: tx.put(
        "board_dissent", "optional-dissent", {
            "mission_id": "m", "proposal_ref": saved["task_ref"],
            "critique": "Synthetic optional historical detail. " * 25,
        },
    ), mission_id="m")
    _context, _pack_result, unbounded = _pack(store, saved)
    assert any(item["type"] == "shared_update" for item in unbounded["details"])
    without_dissent = {**unbounded, "details": [
        item for item in unbounded["details"] if item["type"] != "shared_update"
    ]}
    _context, _pack_result, bounded = _pack(
        store, saved, allowance=len(canonical(without_dissent)) + 50,
    )
    assert bounded["peer_answer_previews"][0]["question"] == (
        "Please review this synthetic claim."
    )
    assert bounded["peer_answer_previews"][0]["report"]["body"] == (
        "A bounded synthetic peer assessment."
    )
    assert any(item["ref"]["id"] == "unrelated-request"
               for item in bounded["request_dispositions"])
    assert not any(item["type"] == "shared_update" for item in bounded["details"])


def test_unchecked_citation_cannot_promote_report_text(tmp_path):
    store = StateStore(tmp_path)
    saved = _fixture(store, citations=[{"sourceId": "unchecked", "spanId": "unread"}])
    _context, _pack_result, index = _pack(store, saved)
    assert "peer_answer_previews" not in index
    assert saved["answer_ref"] in index["request_answer_refs"]
    assert "A bounded synthetic peer assessment." not in canonical(index)


def test_reopened_run_uses_new_preview_without_rewriting_old_input(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    saved = _fixture(store)
    with monkeypatch.context() as old:
        old.setattr("ora.mission.context.MAX_PEER_ANSWER_REPORT_CHARS", 0)
        old.setattr("ora.mission.context.PACKER_VERSION", "ora.working_context.packer.v54")
        context, old_pack, old_index = _pack(store, saved)
        assert "peer_answer_previews" not in old_index
        old_inclusion = bind_no_source_pack(store, context, old_pack, "old-peer-preview")
    old_intent = store.get("model_request_intent", "model-intent:requester-call:1")
    old_bytes = store.read_blob(old_intent["body"]["blob"]["sha256"])
    assert store.get(**old_inclusion)["body"]["packer_version"].endswith("v54")

    old_call = store.get(**saved["call_ref"])
    next_call = store.transact("new-peer-preview-call", {}, lambda tx: ref(tx.put(
        "agent_call", "requester-call-next", old_call["body"],
    )), mission_id="m")
    reopened = StateStore(tmp_path)
    next_pack = WorkingContext(reopened).assemble(
        next_call, saved["task_ref"],
        control_ref=ref(reopened.get("mission_task_control", "requester-control")),
    )
    next_index = json.loads(next_pack.messages[-1]["content"])
    assert next_index["peer_answer_previews"][0]["report_ref"] == saved["report_ref"]
    next_inclusion = bind_no_source_pack(reopened, WorkingContext(reopened), next_pack,
                                         "new-peer-preview")
    assert reopened.get(**next_inclusion)["body"]["packer_version"].endswith("v71")
    assert reopened.read_blob(old_intent["body"]["blob"]["sha256"]) == old_bytes


@pytest.mark.parametrize("long_report", [False, True])
def test_source_revocation_blocks_prepared_peer_preview_dispatch(
    tmp_path, monkeypatch, long_report,
):
    source_text = "Synthetic checked source passage about an uncertain result. " * 4
    report_text = ("Synthetic source-backed peer assessment. " * 60
                   if long_report else "Synthetic source-backed peer assessment.")
    host = service(tmp_path, monkeypatch, lambda _request, **_kw: answer(report_text, 1))
    host.discovery_transport = lambda *_args, **_kw: HTTPResult(200, json.dumps({
        "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
            "source": "MED", "id": "9876", "pmid": "9876",
            "title": "Synthetic checked peer source", "abstractText": source_text,
        }]},
    }).encode(), {})
    try:
        run = host.start(LiveStartRequest(
            request_id="peer-preview-revocation", question="Review a synthetic source",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        requester = host._control(run.run_id, "agent-synthesis")
        responder = host._control(run.run_id, "agent-critique")
        spec = replace(_search_spec(host, run.run_id, suffix="peer-preview"),
                       actor_id=responder["body"]["actor_id"],
                       proposal_ref=responder["body"]["proposal_ref"])
        found, _ = host._search(spec, {"route": "europe_pmc.search",
                                       "query": "synthetic", "limit": 1})
        inspected, outputs = host._inspect_hit(spec, {"hit_id": found["hits"][0]["hit_id"]})
        assert inspected["status"] == "inspected" and len(outputs) == 1
        copy_ref = outputs[0]
        host._update_control(responder, source_refs=[copy_ref])
        mission_id = host._run(run.run_id)["body"]["mission_id"]
        request = host.mailbox.send_request(
            "peer-preview-source-request", mission_id=mission_id,
            request_id="peer-preview-source-request", message_id="peer-preview-source-message",
            requester_id=requester["body"]["actor_id"],
            recipient_id=responder["body"]["actor_id"],
            question="Check the synthetic source.", expected_artifact="A report",
            cause_ref=requester["body"]["proposal_ref"],
            payload={"question": "Check the synthetic source."},
            task_ref=requester["body"]["proposal_ref"],
        )
        assert request["status"] == "accepted"
        host.mailbox.mark_delivered("peer-preview-source-delivery",
                                    mission_id=mission_id,
                                    message_id="peer-preview-source-message")
        assert host._schedule_model(host._control(run.run_id, "agent-critique"))
        asyncio.run(host.executor.run_ready(mission_id))
        assert host._advance(host._control(run.run_id, "agent-critique"))
        report = host.store.records("report")[0]
        assert report_text_access(host.store, report)["source_dependency_refs"] == [copy_ref]
        answered = host.mailbox.record_answer(
            "peer-preview-source-answer", mission_id=mission_id,
            request_id="peer-preview-source-request", answer_id="peer-preview-source-answer",
            responder_id=responder["body"]["actor_id"], artifact_ref=ref(report),
        )
        assert answered["status"] == "accepted"
        if long_report:
            # Match the capability change that reduced room for the pending
            # review cue: the review tool is callable in the next saved packet.
            host._update_control(
                host._control(run.run_id, "agent-synthesis"),
                requested_capabilities=[
                    "review_peer_answer", "read_source_window", "search_saved_passages",
                ],
                input_budget={"observed_limit_tokens": 7000, "target_fraction": .70},
            )
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        requester = host._control(run.run_id, "agent-synthesis")
        call = host.store.get(**requester["body"]["call_ref"])
        intent = host.store.get("model_request_intent",
                                f"model-intent:{call['id']}:{call['version']}")
        prepared = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        index = json.loads(prepared["messages"][-1]["content"])
        if long_report:
            assert "peer_answer_previews" not in index
            assert len(prepared["tools"]) >= 14
            cue = index["peer_answer_review_cues"][0]
            assert cue["question"] == (
                "Check the synthetic source."
            )
            assert cue["owner"] == "you"
            assert cue["partial"] is False
            assert cue["request_ref"] == answered["request_ref"]
            assert cue["answer_ref"] == answered["answer_ref"]
            assert cue["read_request"]["tool"] == "read_context_record"
            assert {key: cue["read_request"][key] for key in (
                "kind", "id", "version",
            )} == ref(report)
            assert "review_peer_answer" in {
                item["function"]["name"] for item in prepared["tools"]
            }
            assert "read_context_record" in {
                item["function"]["name"] for item in prepared["tools"]
            }
            assert prepared["model"] == call["body"]["model_id"]
            budget = next(item["body"] for item in host.store.records(
                "mission_call_budget",
            ) if item["body"].get("call_ref") == ref(call))
            estimate = budget["request_stage_token_estimate"]
            assert estimate_request_tokens(canonical(prepared).encode()).tokens == (
                estimate["final_input_tokens"]
            )
            assert estimate["final_input_tokens"] <= estimate["admission_target_tokens"]
            assert estimate["admission_target_tokens"] - estimate["final_input_tokens"] <= 100
            assert report_text not in canonical(index)
        else:
            assert index["peer_answer_previews"][0]["report"]["body"] == report_text
        assert copy_ref in intent["body"]["context_source_dependency_refs"]
        host.session.load(ref(intent), for_dispatch=True)
        use = host.store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
        host.store.transact("revoke-peer-preview-source", {}, lambda tx: tx.put(
            "source_use", use["id"], {
                **use["body"], "policy_status": "revoked", "allowed_uses": [],
            }, expected_version=use["version"]), mission_id=mission_id)
        with pytest.raises(SessionError):
            host.session.load(ref(intent), for_dispatch=True)
        current_request = host.store.get("mail_request", "peer-preview-source-request")
        current_answer = host.store.get(**answered["answer_ref"])
        assert host.context._peer_answer_preview(
            current_request, current_answer, mission_id=mission_id,
            actor_id=requester["body"]["actor_id"], task_id=requester["body"]["task_id"],
            inclusion_id=f"context:{intent['id']}",
        ) is None
        assert host.context._peer_answer_review_cue(
            current_request, current_answer, mission_id=mission_id,
            actor_id=requester["body"]["actor_id"], task_id=requester["body"]["task_id"],
            inclusion_id=f"context:{intent['id']}",
        ) is None
    finally:
        host.close()
