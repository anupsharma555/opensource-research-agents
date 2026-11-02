"""Observe pinned-model dispatch and separate quota handling through the mission host."""

import asyncio
import json
import threading
import time
import urllib.error
from dataclasses import replace
from io import BytesIO

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.executor import WorkSpec
from ora.models.profiles import GPT_OSS_MODEL, QWEN_MODEL, model_quota_scope
from ora.state import StateError
from tests.mission.test_service import service, tool


def request(name, *, cap=512):
    return LiveStartRequest(
        request_id=name, question="Report the supplied synthetic observation: 2 + 1 = 3.",
        agent_ids=["agent-synthesis", "agent-critique"],
        required_agent_ids=["agent-synthesis", "agent-critique"],
        agent_models={"agent-critique": {
            "model": QWEN_MODEL, "max_completion_tokens": cap,
        }},
    )


def test_native_browser_eligibility_follows_each_saved_agent(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: None)
    try:
        run_id = host.start(request("browser-per-agent")).run_id
        for alias, expected in (("agent-synthesis", True), ("agent-critique", False)):
            control = host._control(run_id, alias)
            config = host._configuration(control)
            assert ("browser_search" in host._requestable_tools(control, config["body"])) == expected
            assert host._schedule_model(control)
            work = host._work_for(host._control(run_id, alias))
            spec = WorkSpec(**work["body"]["spec"])
            assert spec.quota_scopes == [model_quota_scope(
                config["body"]["settings"]["quota_domain"], config["body"]["model_id"],
            )]
            assert spec.output_reservation_tokens == (8192 if expected else 512)
    finally:
        host.close()


@pytest.mark.parametrize("version", [None, 1])
def test_figure_acceptance_uses_saved_legacy_or_model_scope(tmp_path, monkeypatch, version):
    from ora.coordination.executor import WorkResult
    from ora.mission.document_tools import validate_document_result_tx
    from ora.state import StateStore
    from tests.evidence.test_document_ocr import SCOPE
    from tests.mission.test_figure_reading import Reply, _granted_page, _prepare, _reader

    store = StateStore(tmp_path)
    document, page, ocr = _granted_page(store)
    if version is not None:
        run = store.get("mission_run", SCOPE["run_id"])
        store.transact("synthetic-model-scope-version", {}, lambda tx: tx.put(
            "mission_run", run["id"], {**run["body"], "agent_model_binding_version": version},
            expected_version=run["version"],
        ))
    reader = _reader(store, lambda *_a, **_kw: Reply({
        "model": QWEN_MODEL,
        "choices": [{"message": {"content": "Synthetic figure label."}}],
        "usage": {"prompt_tokens": 2100, "completion_tokens": 20, "total_tokens": 2120},
    }), monkeypatch)
    intent = _prepare(reader, document, page, ocr)
    captured = reader.invoke_capture(intent, allow_live_calls=True)  # Injected transport only.
    content = reader.read_result(captured.response_ref, scope=SCOPE)
    spec = WorkSpec(
        work_id="figure-one", mission_id=SCOPE["mission_id"], run_id=SCOPE["run_id"],
        actor_id=SCOPE["actor_id"], proposal_ref={"kind": "board_proposal", "id": SCOPE["task_id"], "version": 1},
        configuration_ref={"kind": "agent_configuration", "id": "synthetic-config", "version": 1},
        input_refs=(), handler_id="mission-figure", handler_version="1", kind="model", lane="figure",
        quota_scopes=(model_quota_scope("groq-selected", QWEN_MODEL, legacy=version is None),),
        output_reservation_tokens=1024 if version else None,
    )
    requested = {"id": "figure-call", "name": "inspect_figure", "arguments": {
        "document_ref": document, "page_number": 1,
    }}
    result = WorkResult(payload={"tool_call_id": "figure-call", "name": "inspect_figure", "result": content},
                        output_refs=(captured.response_ref,), used_tokens=2120,
                        used_output_tokens=20 if version else None)
    def validate(tx):
        validate_document_result_tx(tx, spec, result, requested)
        return {"validated": True}

    assert store.transact("synthetic-figure-acceptance", {}, validate) == {"validated": True}
    wrong = replace(spec, quota_scopes=(model_quota_scope("groq-selected", GPT_OSS_MODEL),))
    with pytest.raises(StateError, match="reserved work"):
        store.transact("synthetic-wrong-figure-scope", {}, lambda tx:
                       validate_document_result_tx(tx, wrong, result, requested))


def test_two_pinned_models_overlap_and_settle_their_own_output_usage(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)
    packets = []

    def transport(req, *, timeout):
        packet = json.loads(req.data)
        packets.append(packet)
        barrier.wait(timeout=5)
        response = tool("publish_result", {
            "title": "Synthetic observation", "body": "2 + 1 = 3.", "citations": [],
        }, 1 if packet["model"] == GPT_OSS_MODEL else 2)
        response.payload["model"] = packet["model"]
        return response

    host = service(tmp_path, monkeypatch, transport)
    try:
        run_id = host.start(request("overlap-pinned-models")).run_id
        for alias in ("agent-synthesis", "agent-critique"):
            assert host._schedule_model(host._control(run_id, alias))
        asyncio.run(host.executor.run_ready(host._run(run_id)["body"]["mission_id"]))
        assert {p["model"] for p in packets} == {GPT_OSS_MODEL, QWEN_MODEL}
        assert len(packets) == 2
        operations = [r["body"] for r in host._heads("capacity_operation")
                      if r["body"].get("run_id") == run_id and r["body"]["kind"] == "model"]
        assert len(operations) == 2
        assert len({tuple(o["quota_scopes"]) for o in operations}) == 2
        for operation in operations:
            assert operation["phase"] == "completed"
            assert operation["attempts"][-1]["charged_output_tokens"] == 6
    finally:
        host.close()


def test_confirmed_output_limit_does_not_retry_unchanged_cap_or_switch_model(tmp_path, monkeypatch):
    packets = []

    def transport(req, *, timeout):
        packet = json.loads(req.data)
        packets.append(packet)
        error = {"error": {"type": "tokens", "code": "rate_limit_exceeded", "message":
            f"Request too large for model `{QWEN_MODEL}` on output tokens per minute "
            "(OTPM): Limit 1000, Requested 1024."}}
        raise urllib.error.HTTPError(req.full_url, 429, "synthetic output limit", {},
                                     BytesIO(json.dumps(error).encode()))

    host = service(tmp_path, monkeypatch, transport)
    try:
        selected = request("confirmed-output-limit", cap=1024)
        selected.agent_ids = ["agent-critique"]
        selected.required_agent_ids = ["agent-critique"]
        run_id = host.start(selected).run_id
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            host._drive_run(host._run(run_id))
            if host._heads("capacity_output_quota"):
                break
            time.sleep(0.01)
        assert len(packets) == 1
        for _ in range(5):
            host._drive_run(host._run(run_id))
        assert len(packets) == 1
        assert "Waiting alone cannot make this request fit" in host._control(
            run_id, "agent-critique")["body"]["waiting_reason"]
        assert packets[0]["model"] == QWEN_MODEL
        assert packets[0]["max_completion_tokens"] == 1024
        operations = [r for r in host._heads("capacity_operation")
                      if r["body"].get("run_id") == run_id and r["body"]["kind"] == "model"]
        assert len(operations) == 1
        status = host.capacity.output_capacity_status(operations[0]["id"])
        assert status["state"] == "request_exceeds_observed_output_minute_limit"
        assert status["limit_tokens"] == 1000
        assert host._configuration(host._control(run_id, "agent-critique"))["body"][
            "settings"]["max_completion_tokens"] == 1024
    finally:
        host.close()


def test_initial_input_limit_lookup_uses_actor_model_and_saved_profile(tmp_path, monkeypatch):
    observed = []

    class Cache:
        def latest(self, selection):
            observed.append(selection)
            return None

    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: None)
    try:
        selected = request("actor-input-limit-hints")
        selected.credential_profile = "backup"
        run_id = host.start(selected).run_id
        host.provider_limit_cache = Cache()
        for alias in ("agent-synthesis", "agent-critique"):
            host._effective_input_budget(host._control(run_id, alias))
        assert [entry["model"] for entry in observed] == [GPT_OSS_MODEL, QWEN_MODEL]
        assert {entry["credential_profile"] for entry in observed} == {"backup"}
    finally:
        host.close()


@pytest.mark.parametrize("discrepancy,headroom", [(50, 128), (220, 284)])
def test_verified_itpm_hint_bounds_prepared_input_without_repricing_combined_limit(
    tmp_path, monkeypatch, discrepancy, headroom,
):
    class Cache:
        def latest(self, selection):
            if selection["model"] != QWEN_MODEL or selection["credential_profile"] != "backup":
                return None
            return {
                "scope": selection, "observed_limit_tokens": 8000,
                "observed_input_limit_tokens": 7000,
                "input_admission_discrepancy_tokens": discrepancy,
                "input_limit_receipt_ref": {
                    "kind": "model_response_receipt", "id": "synthetic-ref", "version": 1,
                },
                "input_limit_source_state_path": str(tmp_path / "prior-case"),
                "input_limit_basis": "verified_saved_provider_itpm_413",
            }

    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: None)
    try:
        host.provider_limit_cache = Cache()
        selected = request("itpm-preparation", cap=960)
        selected.credential_profile = "backup"
        run_id = host.start(selected).run_id
        control = host._control(run_id, "agent-critique")
        fresh_budget = host._effective_input_budget(control)
        assert fresh_budget["observed_limit_tokens"] == 8000
        assert fresh_budget["observed_input_limit_tokens"] == 7000
        assert fresh_budget["basis"] == "project_scoped_observed_provider_token_limit"
        host._update_control(control, input_budget={
            "observed_limit_tokens": 7000,
            "target_fraction": .9,
            "basis": "qualification_imposed_limit",
        })
        control = host._control(run_id, "agent-critique")
        effective = host._effective_input_budget(control)
        assert effective["observed_limit_tokens"] == 7000
        assert effective["observed_input_limit_tokens"] == 7000
        assert host._schedule_model(control)
        budget = host.store.get(
            "mission_call_budget", host._control(run_id, "agent-critique")["body"]["call_ref"]["id"],
        )["body"]
        stage = budget["request_stage_token_estimate"]
        assert stage["observed_limit_tokens"] == 7000
        assert stage["observed_input_limit_tokens"] == 7000
        assert stage["input_limit_headroom_tokens"] == headroom
        assert budget["request_token_estimate"]["tokens"] <= 7000 - headroom
        assert not host.store.records("model_response_receipt")
    finally:
        host.close()
