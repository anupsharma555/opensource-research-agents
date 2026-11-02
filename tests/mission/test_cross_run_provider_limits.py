"""Fresh missions reuse scoped provider packing evidence without replaying research."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO

import pytest

import ora.models.session as session_module
from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission import MissionService
from ora.mission.service import RATE_INPUT_HEADROOM_FRACTION
from ora.models import GroqSelectedAdapter
from ora.models.token_estimate import estimate_request_tokens
from tests.mission.test_service import ROOT, answer, drain, two_supplied_sources


def _project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    for name in ("roles", "runtime_skills", "src/ora"):
        shutil.copytree(ROOT / name, project / name, ignore=shutil.ignore_patterns("__pycache__"))
    return project


@pytest.mark.parametrize("source_count", [1, 2])
@pytest.mark.parametrize("profile", ["primary", "backup"])
def test_captured_limit_reaches_first_request_in_an_isolated_next_case(tmp_path, monkeypatch, profile, source_count):
    project = _project(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "synthetic-token-backup")
    packets = []

    def transport(request, *, timeout):
        packets.append(bytes(request.data))
        response = answer("Synthetic transport observation, not a scientific result.", len(packets))
        response.headers = {"x-ratelimit-limit-tokens": "8000"}
        return response

    monkeypatch.setattr(session_module, "GroqSelectedAdapter", lambda root:
                        GroqSelectedAdapter(root, transport=transport))
    first = MissionService(project, tmp_path / "first", execution_mode="live", start_supervisor=False)
    try:
        run = first.start(LiveStartRequest(
            request_id="limit-donor", question="Assess supplied synthetic material.",
            evidence_text="Synthetic report records an uncertain comparison.",
            agent_ids=["agent-synthesis"], credential_profile=profile,
        ))
        drain(first, run.run_id)
        assert first.state(run.run_id).snapshot.mission.status == "completed"
        assert first.store.records("model_response_receipt")
    finally:
        first.close()
    assert len(packets) == 1
    # The next fresh case has no prior mission/capacity history. Only the
    # response-derived numeric packing hint crosses the store boundary.
    second = MissionService(project, tmp_path / "second", execution_mode="live", start_supervisor=False)
    try:
        text = " ".join(
            f"Synthetic row {i}: mean-response uncertainty differs from a new measurement; "
            "the conditional interval preserves units, assumptions, and uncertainty."
            for i in range(76)
        )
        run = second.start(LiveStartRequest(
            request_id="fresh-source-heavy", question="Assess the supplied report and its limits.",
            evidence_text=text, agent_ids=["agent-synthesis"], credential_profile=profile,
        ))
        if source_count == 2:
            two_supplied_sources(second, run.run_id)
        assert second._schedule_model(second._control(run.run_id, "agent-synthesis"))
        intent = second.store.records("model_request_intent")[0]
        raw = second.store.read_blob(intent["body"]["blob"]["sha256"])
        packet = json.loads(raw)
        budget = second.store.get("mission_call_budget", intent["body"]["call_ref"]["id"])["body"]
        evidence = second.store.get(**budget["account_limit_evidence_ref"])
        assert evidence["kind"] == "provider_limit_observation"
        control = second._control(run.run_id, "agent-synthesis")
        with ThreadPoolExecutor(max_workers=4) as pool:
            hints = list(pool.map(lambda _: second._effective_input_budget(control), range(8)))
        assert all(hint["response_ref"] == budget["account_limit_evidence_ref"] for hint in hints)
        assert len(second.store.records("provider_limit_observation")) == 1
        assert second.store.records("provider_limit_observation")[0]["version"] == 1
        assert evidence["body"]["scope"]["credential_profile"] == profile
        assert evidence["body"]["source_state_path"] == str((tmp_path / "first").resolve())
        assert budget["observed_account_limit"] == 8000
        assert budget["request_token_estimate"]["tokens"] == estimate_request_tokens(raw).tokens
        assert budget["exact_complete_request_sha256"] == hashlib.sha256(raw).hexdigest()
        assert len(intent["body"]["source_refs"]) == source_count
        assert text in "\n".join(m["content"] for m in packet["messages"])
        assert packet["max_completion_tokens"] == 8192
        assert budget["request_stage_token_estimate"]["input_only_limit_check"] == "within_observed_hint"
        assert len(second.store.records("mission_run")) == 1
        assert not second.store.records("model_response_receipt")  # Prepared, not dispatched.
        assert str(tmp_path / "first") not in raw.decode()  # Cache provenance is not model context.
        assert len(packets) == 1
        second.dispatch.poll(second._run(run.run_id)["body"]["mission_id"])
        drain(second, run.run_id)
        assert len(packets) == 2
        assert packets[-1] == raw
    finally:
        second.close()


def test_offline_host_does_not_open_live_limit_cache(tmp_path):
    project = _project(tmp_path)
    host = MissionService(project, tmp_path / "state", execution_mode="offline_test", start_supervisor=False)
    try:
        assert host.provider_limit_cache is None
        assert host.session.limit_observer is None
        assert not (project / ".local/provider-limits").exists()
    finally:
        host.close()


def test_failed_hint_write_does_not_lose_a_captured_answer(tmp_path, monkeypatch, caplog):
    from tests.mission.test_service import service

    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: answer("Saved result.", 1))
    host.session.limit_observer = lambda *_args: (_ for _ in ()).throw(OSError("synthetic cache failure"))
    try:
        run = host.start(LiveStartRequest(
            request_id="cache-write-failure", question="Assess the source.",
            evidence_text="A synthetic uncertain result.", agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        assert len(host.store.records("model_response_receipt")) == 1
        assert len(host.store.records("report")) == 1
        assert "Provider limit hint could not be saved" in caplog.text
    finally:
        host.close()


@pytest.mark.parametrize("observed_limit,strip_review,oversize_source", [
    (8000, False, False), (16000, False, False),
    (8000, True, False), (8000, False, True),
])
def test_peer_answer_and_three_sources_pack_before_size_probe(
    tmp_path, monkeypatch, observed_limit, strip_review, oversize_source,
):
    """A fresh reply must stay complete or readable when source text is indexed."""
    from tests.mission.test_service import service

    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: answer("unused", 1))
    try:
        run = host.start(LiveStartRequest(
            request_id=f"three-source-peer-{observed_limit}",
            question="Compare three synthetic reports and assess the peer answer.",
            evidence_text="Synthetic first report. " * 85,
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        first, second = two_supplied_sources(host, run.run_id)
        requester = host._control(run.run_id, "agent-synthesis")
        responder = host._control(run.run_id, "agent-critique")
        copy = host.store.get(**second)
        original_passage = host.store.get(
            "source_passage", copy["body"]["passage_ids"][0],
        )
        third_text = "\n".join(
            f"Synthetic source row {number}: group, comparator, and time point are unverified."
            for number in range(7500 if oversize_source else 120)
        )
        blob = host.store.put_blob(third_text.encode())
        third_id = "third-synthetic-copy"
        passage_id = f"{third_id}:supplied-1"

        def third_source(tx):
            third = tx.put("source_copy", third_id, {
                **copy["body"], "blob": blob, "content_sha256": blob["sha256"],
                "source_version": "synthetic-third-v1", "passage_ids": [passage_id],
            })
            tx.put("source_passage", passage_id, {
                **original_passage["body"], "copy_ref": ref(third),
                "text": third_text, "start": 0, "end": len(third_text),
                "locator": f"submitted text; chars=0:{len(third_text)}",
            })
            return ref(third)

        third = host.store.transact("third-synthetic-source", {}, third_source,
                                    mission_id=requester["body"]["mission_id"])
        source_refs = [first, second, third]
        for control in (requester, responder):
            host._update_control(control, source_refs=source_refs)
        current_run = host._run(run.run_id)
        host.store.transact("three-source-run-link", {}, lambda tx: tx.put(
            "mission_run", run.run_id,
            {**current_run["body"], "source_refs": source_refs},
            expected_version=current_run["version"],
        ), mission_id=requester["body"]["mission_id"])
        requester = host._control(run.run_id, "agent-synthesis")
        responder = host._control(run.run_id, "agent-critique")
        peer_task = host.store.get("board_proposal", responder["body"]["task_id"])

        def save_peer_report(tx):
            report = tx.put("report", "synthetic-peer-report", {
                "mission_id": requester["body"]["mission_id"],
                "title": "Synthetic peer assessment",
                "body": "The synthetic comparison needs its source limits retained. " * 13,
                "citations": [], "limitations": ["Synthetic fixture only"],
            })
            outcome = tx.put("board_outcome", "synthetic-peer-outcome", {
                "mission_id": requester["body"]["mission_id"],
                "proposal_ref": ref(peer_task), "result_refs": [ref(report)],
            })
            tx.put("board_proposal", peer_task["id"], {
                **peer_task["body"], "status": "completed", "outcome_ref": ref(outcome),
            }, expected_version=peer_task["version"])
            tx.put("mission_task_control", responder["id"], {
                **responder["body"], "phase": "done", "report_ref": ref(report),
            }, expected_version=responder["version"])
            return ref(report)

        report_ref = host.store.transact(
            "save-synthetic-peer-report", {}, save_peer_report,
            mission_id=requester["body"]["mission_id"],
        )
        sent = host.mailbox.send_request(
            "ask-synthetic-peer", mission_id=requester["body"]["mission_id"],
            request_id="synthetic-three-source-request", message_id="synthetic-three-source-message",
            requester_id=requester["body"]["actor_id"],
            recipient_id=responder["body"]["actor_id"],
            question="Which synthetic source limits affect this comparison?",
            expected_artifact="Peer report", cause_ref=requester["body"]["proposal_ref"],
            payload={"question": "Which synthetic source limits affect this comparison?"},
            task_ref=requester["body"]["proposal_ref"],
        )
        assert sent["status"] == "accepted"
        host.mailbox.mark_delivered(
            "deliver-synthetic-peer", mission_id=requester["body"]["mission_id"],
            message_id="synthetic-three-source-message",
        )
        saved = host.mailbox.record_answer(
            "answer-synthetic-peer", mission_id=requester["body"]["mission_id"],
            request_id="synthetic-three-source-request", answer_id="synthetic-three-source-answer",
            responder_id=responder["body"]["actor_id"], artifact_ref=report_ref,
        )
        assert saved["status"] == "accepted"
        requester = host._control(run.run_id, "agent-synthesis")
        host._update_control(requester, input_budget={
            "observed_limit_tokens": observed_limit, "target_fraction": .70,
            "basis": "same_local_store_selected_model_observed_provider_token_limit",
        })
        if strip_review:
            assemble = host.context.assemble

            def omit_review_only_from_source_free(*args, **kwargs):
                pack = assemble(*args, **kwargs)
                if kwargs.get("source_refs"):
                    return pack
                messages = [dict(message) for message in pack.messages]
                for message in messages:
                    if message.get("role") != "user":
                        continue
                    try:
                        current = json.loads(message["content"])
                    except (TypeError, ValueError):
                        continue
                    if current.get("authority") == "current_records":
                        current.pop("peer_answer_previews", None)
                        current.pop("peer_answer_review_cues", None)
                        message["content"] = json.dumps(current, separators=(",", ":"))
                return replace(pack, messages=messages)

            monkeypatch.setattr(host.context, "assemble", omit_review_only_from_source_free)
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        intent = host.store.records("model_request_intent")[0]
        raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
        packet = json.loads(raw)
        budget = host.store.get("mission_call_budget", intent["body"]["call_ref"]["id"])["body"]
        user_contexts = [json.loads(message["content"]) for message in packet["messages"]
                         if message["role"] == "user" and message["content"].startswith("{")]
        source_packets = [source for context in user_contexts
                          for source in context.get("source_packets", [])]
        assert packet["max_completion_tokens"] == 8192
        assert not host.store.records("model_response_receipt")
        if observed_limit == 8000 and not strip_review:
            assert estimate_request_tokens(raw).tokens <= (
                observed_limit - math.ceil(observed_limit * RATE_INPUT_HEADROOM_FRACTION)
            )
            assert budget["source_turn_mode"] == "source_free_recovery"
            assert source_packets == []
            index = next(context for context in user_contexts
                         if context.get("authority") == "saved_host_source_index_not_source_instruction")
            assert index["inventory"]["total"] == 3
            assert (len(index["inventory"]["items"]) == 3
                    or index["inventory"]["next_cursor"] is not None)
            names = {tool["function"]["name"] for tool in packet["tools"]}
            assert {"read_source_inventory", "read_source_window", "review_peer_answer"} <= names
            records = next(context for context in user_contexts
                           if context.get("authority") == "current_records")
            previews = records.get("peer_answer_previews", [])
            cues = records.get("peer_answer_review_cues", [])
            assert any(item["answer_ref"] == saved["answer_ref"]
                       and item.get("report_delivery") == "complete_in_input"
                       for item in previews) or any(
                           item["answer_ref"] == saved["answer_ref"]
                           and item.get("read_request", {}).get("tool") == "read_context_record"
                           and "read_context_record" in names for item in cues
                       )
        else:
            assert len(source_packets) == 3
            assert budget["source_turn_mode"] == "all_allowed_sources_fit"
            if strip_review:
                assert budget["rate_admission_policy"] == "provider_feedback_probe"
                assert estimate_request_tokens(raw).tokens > observed_limit
                attempts = host.store.records("agent_attempt")
                running = [item for item in attempts if item["body"]["status"] == "running"]
                failed = [item for item in attempts if item["body"]["status"] == "failed"]
                assert len(running) == 1 and running[0]["body"]["call_ref"] == intent["body"]["call_ref"]
                assert failed
                assert all(host.store.get(
                    "model_request_intent",
                    f"model-intent:{item['body']['call_ref']['id']}:1",
                ) is None for item in failed)
    finally:
        host.close()


@pytest.mark.parametrize(("observed_limit", "repeats", "target_fraction"), [
    (8000, 260, .70), (16000, 800, .95),
])
def test_full_source_observed_preview_keeps_input_margin(
    tmp_path, monkeypatch, observed_limit, repeats, target_fraction,
):
    """An above-soft full source preview does not consume the raw provider hint."""
    from tests.mission.test_service import service

    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: answer("unused", 1))
    try:
        safe_target = observed_limit - math.ceil(observed_limit * RATE_INPUT_HEADROOM_FRACTION)
        text = "Synthetic comparison with no scientific claim. " * repeats
        run = host.start(LiveStartRequest(
            request_id="full-source-observed-preview", question="Inspect the supplied synthetic text.",
            evidence_text=text, agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, input_budget={
            "observed_limit_tokens": observed_limit, "target_fraction": target_fraction,
            "basis": "same_local_store_selected_model_observed_provider_token_limit",
        })
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        intent = host.store.records("model_request_intent")[0]
        raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
        packet = json.loads(raw)
        budget = host.store.get("mission_call_budget", intent["body"]["call_ref"]["id"])["body"]
        assert budget["source_turn_mode"] == "all_allowed_sources_fit"
        if math.floor(observed_limit * target_fraction) < safe_target:
            assert math.floor(observed_limit * target_fraction) < estimate_request_tokens(raw).tokens
        assert budget["preview_admission_target_tokens"] == safe_target
        assert estimate_request_tokens(raw).tokens <= safe_target
        assert text in json.dumps(packet["messages"])
        assert packet["max_completion_tokens"] == 8192
        assert not host.store.records("model_dispatch_attempt")
    finally:
        host.close()


def test_confirmed_413_below_observed_limit_repacks_with_margin(tmp_path, monkeypatch):
    """A provider refusal disproves the local estimate even when it was below 8K."""
    from tests.mission.test_service import service

    calls = []

    def transport(request, *, timeout):
        calls.append(bytes(request.data))
        if len(calls) > 1:
            raise AssertionError("Repair is prepared offline, not dispatched")
        raise urllib.error.HTTPError(
            "https://example.invalid", 413, "synthetic size refusal",
            {"Retry-After": "0", "x-ratelimit-limit-tokens": "8000",
             "x-ratelimit-remaining-tokens": "7885",
             "x-ratelimit-reset-tokens": "862ms"}, BytesIO(b""),
        )

    host = service(tmp_path, monkeypatch, transport)
    try:
        safe_target = 8000 - math.ceil(8000 * RATE_INPUT_HEADROOM_FRACTION)
        text = (
            "Synthetic comparison with no scientific claim. " * 170
            + "Synthetic protocol note: check the comparator arm, assessment time point, "
            "outcome scale, group sizes, and uncertainty interval as separate fields. "
            "A follow-up observation cannot be counted as a new trial without checking "
            "whether it belongs to the original cohort. Synthetic audit note: map each "
            "publication to its enrolled cohort, randomized comparison, outcome "
            "instrument, and follow-up schedule. This fixture provides no numeric effect "
            "or confidence interval, so preserve uncertainty instead of treating a new "
            "report title as independent evidence. Distinguish participants from "
            "observations and state which denominator each percentage would use. "
            "The synthetic record separates prespecified from exploratory endpoints "
            "and asks whether missing follow-up data would affect interpretation. "
            "These are methodological prompts; no trial result is supplied."
        )
        run = host.start(LiveStartRequest(
            request_id="underestimated-413", question="Inspect the supplied synthetic text.",
            evidence_text=text, agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        original = host.store.records("model_request_intent")[0]
        assert safe_target < original["body"]["request_token_estimate"]["tokens"] < 8000
        original_raw = host.store.read_blob(original["body"]["blob"]["sha256"])

        def source_packets(raw):
            packet = json.loads(raw)
            return [source for message in packet["messages"]
                    if message["role"] == "user" and message["content"].startswith("{")
                    for source in json.loads(message["content"]).get("source_packets", [])]

        original_sources = source_packets(original_raw)
        assert len(original_sources) == 1
        assert text in original_sources[0]["text"]
        assert original["body"]["source_refs"]
        control = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(control)
        capacity_id = host.executor._capacity_id(host.executor._spec_from(work))
        host.dispatch.poll(host._run(run.run_id)["body"]["mission_id"])
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and (
            (observed_cap := host.capacity.get(capacity_id)) is None
            or observed_cap["phase"] != "failed"
        ):
            time.sleep(.01)
        cap = host.capacity.get(capacity_id)
        assert cap["phase"] == "failed" and len(calls) == 1
        assert calls[0] == original_raw
        assert host._repair_oversize(control, work, cap)
        ready = host._control(run.run_id, "agent-synthesis")
        assert ready["body"]["input_budget"]["observed_limit_tokens"] == 8000
        if ready["body"]["phase"] == "retry_at":
            host._update_control(ready, phase="ready_model", retry_at=None)
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        intents = host.store.records("model_request_intent")
        assert len(intents) == 2
        repaired = next(item for item in intents if item["id"] != original["id"])
        raw = host.store.read_blob(repaired["body"]["blob"]["sha256"])
        budget = host.store.get("mission_call_budget", repaired["body"]["call_ref"]["id"])["body"]
        assert repaired["body"]["request_token_estimate"]["tokens"] <= safe_target
        assert budget["rate_admission_policy"] == "confirmed_size_repair"
        assert budget["preview_admission_target_tokens"] <= safe_target
        assert len(raw) < len(calls[0])
        assert repaired["body"]["source_refs"] == original["body"]["source_refs"]
        assert source_packets(raw) == original_sources
        assert len(calls) == 1
    finally:
        host.close()


def test_observed_recovery_accepts_exact_reader_for_long_peer_question(tmp_path, monkeypatch):
    from tests.mission.test_pending_review_tools import _setup

    host, control, _config, saved = _setup(
        tmp_path, monkeypatch, report_body="A synthetic peer assessment. " * 100,
    )
    try:
        request = host.store.get(**saved["request_ref"])
        host.store.transact("long-observed-question", {}, lambda tx: tx.put(
            "mail_request", request["id"],
            {**request["body"], "question": "Q" * 601},
            expected_version=request["version"],
        ), mission_id=control["body"]["mission_id"])
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
            "basis": "same_local_store_selected_model_observed_provider_token_limit",
        })
        control = host._control(control["body"]["run_id"], "agent-synthesis")
        assert host._prepare_model(
            control, force_soft_recovery=True, allow_rate_probe=True,
            observed_recovery_only=True,
        )
        intent = host.store.records("model_request_intent")[0]
        raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
        packet = json.loads(raw)
        assert estimate_request_tokens(raw).tokens <= 8000
        assert packet["max_completion_tokens"] == 8192
        records = next(json.loads(message["content"]) for message in packet["messages"]
                       if message.get("role") == "user"
                       and '"authority":"current_records"' in message.get("content", ""))
        cue = records["peer_answer_review_cues"][0]
        assert "question" not in cue
        assert cue["read_full_question"]["tool"] == "read_context_record"
        assert cue["read_request"]["tool"] == "read_context_record"
        names = {tool["function"]["name"] for tool in packet["tools"]}
        assert {"read_context_record", "review_peer_answer"} <= names
        assert not host.store.records("model_response_receipt")
    finally:
        host.close()


@pytest.mark.parametrize("prior_version", [
    "source_working_set_v19", "source_working_set_v20", "current",
])
def test_prior_wait_can_reprepare_under_new_observed_window_rules(
    tmp_path, monkeypatch, prior_version,
):
    from ora.mission.context import PACKER_VERSION
    from ora.mission.service import ESTIMATOR_VERSION
    from tests.mission.test_service import service

    prior_version = ESTIMATOR_VERSION if prior_version == "current" else prior_version
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: answer("unused", 1))
    try:
        run = host.start(LiveStartRequest(
            request_id="prior-v19-context-wait", question="Review synthetic source context.",
            evidence_text="A bounded synthetic source.", agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        old_call_id, _ = host._model_ids(control, control["body"]["sequence"] + 1)
        old_budget = host._save("mission_call_budget", old_call_id, {
            "mission_id": control["body"]["mission_id"], "run_id": run.run_id,
            "estimator_version": prior_version,
            "packer_version": PACKER_VERSION, "context_allowance": 1,
        }, mission_id=control["body"]["mission_id"],
            operation_id="synthetic-prior-wait-budget")
        host._update_control(
            control, phase="waiting", work_id=None,
            waiting_reason="ContextOverflow: this branch needs local repair",
            input_budget={"observed_limit_tokens": 8000, "target_fraction": .70},
        )
        if prior_version == ESTIMATOR_VERSION:
            unchanged = host._control(run.run_id, "agent-synthesis")
            assert not host._recover_preparation(unchanged)
            assert not host._recover_preparation(unchanged)
            assert host._control(run.run_id, "agent-synthesis") == unchanged
            assert ref(host.store.get("mission_call_budget", old_call_id)) == old_budget
            assert not host.store.records("model_request_intent")
            return
        assert host._recover_preparation(host._control(run.run_id, "agent-synthesis"))
        ready = host._control(run.run_id, "agent-synthesis")
        assert ready["body"]["phase"] == "ready_model"
        assert ready["body"]["preparation_repair_attempted_for"] == ref(old_budget)
        assert host._schedule_model(ready)
        new_budget = host.store.get("mission_call_budget", old_call_id)
        assert new_budget["body"]["estimator_version"] == ESTIMATOR_VERSION
        assert new_budget["version"] > old_budget["version"]
        historical = host.store.get("mission_call_budget", old_call_id, 1)
        assert historical["body"]["estimator_version"] == prior_version
        assert historical["body"]["context_allowance"] == 1
        assert len(host.store.records("model_request_intent")) == 1
        assert not host.store.records("model_response_receipt")
    finally:
        host.close()
