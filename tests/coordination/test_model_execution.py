"""Two saved GPT-OSS calls cross the real offline executor and model bridge."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from ora.agents import COMMON_TOOL_IDS, AgentRegistry, CapabilityCatalog, ConfigurationRegistry
from ora.agents.capabilities import content_hash
from ora.coordination.board import ProposalBoard, ref
from ora.coordination.capacity import CapacityPolicy, CapacityScheduler, QuotaLimit
from ora.coordination.executor import ConcurrentExecutor, WorkResult, WorkSpec
from ora.coordination.mailbox import PeerMailbox
from ora.coordination.traces import TraceJournal
from ora.evidence import EvidenceLibrary
from ora.models import GroqSelectedAdapter, ModelSession, SourceSelection
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]
MODEL = "openai/gpt-oss-120b"
URL = "https://api.groq.com/openai/v1/chat/completions"


class FakeResponse:
    def __init__(self, label: str) -> None:
        self.headers = {"x-request-id": f"header-{label}"}
        self.body = json.dumps({
            "id": f"chat-{label}", "model": MODEL,
            "x_groq": {"id": f"provider-{label}"},
            "choices": [{"message": {"role": "assistant",
                                      "content": f"Synthetic result for {label}.",
                                      "reasoning": f"Synthetic returned reasoning for {label}."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20,
                      "total_tokens": 30, "queue_time": 0.01,
                      "completion_time": 0.02},
        }).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return self.body


def saved_abstract(store: StateStore) -> dict:
    library = EvidenceLibrary(store)
    publication = library.register_publication(
        "integration-publication", identifiers={"doi": "10.1234/synthetic.integration"},
        title="Synthetic abstract for concurrency", verification="Fixture DOI printed in metadata",
    )["publication_ref"]
    encounter = library.record_encounter(
        "integration-encounter", publication_ref=publication, retrieval_hit_ref=None,
        status="accepted", access_surface="abstract",
        retrieved_at="2026-01-02T00:00:00Z", route="synthetic-integration",
    )["encounter_ref"]
    return library.add_copy(
        "integration-copy", publication, encounter,
        content=b"Synthetic abstract has two independent questions.",
        content_sha256=None, mime_type="text/plain", source_version="synthetic-v1",
        access_surface="abstract", retrieved_at="2026-01-02T00:00:00Z",
        rights_basis="Project-owned synthetic fixture", retain_bytes=True,
        reference="simulated://integration-abstract", passages=[],
        inspection_state="checked", validation_record="synthetic-check",
    )["copy_ref"]


def test_two_selected_model_calls_overlap_and_accept_exact_saved_receipts(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store = StateStore(tmp_path)
    source_ref = saved_abstract(store)
    source = store.get(**source_ref)
    registry = AgentRegistry(store, ROOT)
    catalog = CapabilityCatalog.with_common()
    selections = ConfigurationRegistry(store, ROOT, catalog)
    board = ProposalBoard(store)
    capacity = CapacityScheduler(store, CapacityPolicy(
        permits={"model": 2, "retrieval": 1, "compute": 1},
        quotas={"groq-selected": QuotaLimit(60, requests=2, tokens=20000)},
        review_reserve=0,
    ))
    journal = TraceJournal(store)
    rendezvous = threading.Barrier(2)
    lock = threading.Lock()
    observed: list[dict] = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        label = next(name for name in ("task-a", "task-b")
                     if name in json.dumps(body["messages"]))
        with lock:
            observed.append({"label": label, "body": body, "timeout": timeout})
        # Both provider requests must be in flight before either can return.
        rendezvous.wait(timeout=5)
        return FakeResponse(label)

    session = ModelSession(store, ROOT, adapter=GroqSelectedAdapter(ROOT, transport=transport),
                           journal=journal)
    intent_by_call: dict[str, dict] = {}
    specs: list[WorkSpec] = []
    configurations = []
    calls = []
    for label in ("task-a", "task-b"):
        actor = registry.register(
            f"register-{label}", mission_id="mission", run_id="run",
            role_id="literature_acquisition", display_name=f"Synthetic {label}",
        )
        proposal = board.propose(
            f"propose-{label}", mission_id="mission", proposal_id=label,
            actor_id=actor["id"], objective=f"Inspect {label}",
            expected_artifact="Synthetic model receipt", next_action="Review saved abstract",
            prerequisite_refs=[source_ref],
        )["proposal_ref"]
        config = selections.save_configuration(
            f"config-{label}", instance_id=actor["id"], mission_id="mission",
            task_scope="synthetic source review", provider="groq", endpoint=URL,
            model_id=MODEL,
            settings={"max_completion_tokens": 8192, "stream": False,
                      "unknown_source_policy": "reject"},
            reasoning_settings={"reasoning_effort": "high", "include_reasoning": True},
            instructions="Use only the saved synthetic abstract.", skill_choices={},
            grants={f"common:{name}" for name in COMMON_TOOL_IDS},
            input_refs=[{**source_ref, "sha256": content_hash(source["body"])}],
        )
        call = selections.start_call(
            f"call-op-{label}", call_id=f"call-{label}", configuration_id=config["id"],
            mission_id="mission", task_id=label, attempt_id=f"attempt-{label}",
            loaded_skill_ids=[], exposed_tool_ids=["evidence_lookup"],
            require_common_tools=False,
        )
        if label == "task-b":
            call = selections.expose_tool(
                "expand-task-b", call_id=call["id"], mission_id="mission",
                tool_id="board_read",
            )
        prepared = session.prepare(
            call_id=call["id"], call_version=call["version"], phase="collect",
            messages=[{"role": "user", "content": f"Review {label} using the abstract."}],
            sources=(SourceSelection(source_ref, "permitted", True),),
        )
        intent_by_call[call["id"]] = prepared.intent_ref
        configurations.append(config)
        calls.append(call)
        specs.append(WorkSpec(
            work_id=label, mission_id="mission", run_id="run", proposal_ref=proposal,
            actor_id=actor["id"], configuration_ref=ref(config),
            input_refs=(source_ref,), handler_id="saved-groq", handler_version="1",
            kind="model", lane=label, quota_scopes=("groq-selected",),
            estimated_tokens=8192, attempt_id=f"attempt-{label}",
            call_id=call["id"], call_version=call["version"],
        ))

    assert [call["version"] for call in calls] == [1, 2]
    assert {config["body"]["model_id"] for config in configurations} == {MODEL}
    assert len({config["body"]["input_refs"][0]["version"]
                for config in configurations}) == 1

    def callback(work: WorkSpec) -> WorkResult:
        # The exact intent was prepared before submission. After the executor
        # records dispatch, an exception here is conservatively uncertain;
        # SessionError must not be reclassified as confirmed no-effect.
        receipt_ref = session.invoke_capture(intent_by_call[work.call_id])
        receipt = store.get(**receipt_ref)
        return WorkResult(payload={"synthetic_provider_status": receipt["body"]["status"]},
                          output_refs=(receipt_ref,),
                          used_tokens=receipt["body"]["usage"]["total_tokens"])

    executor = ConcurrentExecutor(
        store, board, PeerMailbox(store), capacity, journal,
        {("saved-groq", "1"): callback}, owner="integration-host",
    )
    for spec in specs:
        executor.submit(spec)
    result = asyncio.run(asyncio.wait_for(executor.run_ready("mission"), timeout=8))

    assert result["work"] == {"task-a": "completed", "task-b": "completed"}
    assert {item["label"] for item in observed} == {"task-a", "task-b"}
    assert len(observed) == 2
    for item in observed:
        body = item["body"]
        assert item["timeout"] == 120
        assert body["model"] == MODEL
        assert body["reasoning_effort"] == "high"
        assert body["include_reasoning"] is True
        assert body["max_completion_tokens"] == 8192
        assert "Synthetic abstract has two independent questions." in json.dumps(body)
        assert body["parallel_tool_calls"] is False
        if item["label"] == "task-b":
            assert {tool["function"]["name"] for tool in body["tools"]} == {
                "board_read", "evidence_lookup",
            }
    assert len(store.records("model_response_receipt")) == 2
    assert len(store.records("executor_artifact")) == 2
    assert len(store.records("executor_receipt")) == 2
    assert len(store.records("board_outcome")) == 2
    assert len(store.records("coordination_trace")) == 4  # model and executor per call
    assert all(item["body"]["status"] == "captured"
               for item in store.records("model_dispatch_attempt"))
    assert all(store.get("board_proposal", label)["body"]["status"] == "completed"
               for label in ("task-a", "task-b"))
    for spec in specs:
        operation = capacity.get(executor._capacity_id(spec))
        assert operation["phase"] == "completed"
        assert operation["attempts"][0]["charged_tokens"] == 30
    receipts = store.records("model_response_receipt")
    assert {item["body"]["call_ref"]["version"] for item in receipts} == {1, 2}
    for receipt in receipts:
        intent = store.get(**receipt["body"]["intent_ref"])
        assert receipt["body"]["request_sha256"] == intent["body"]["request_sha256"]
        assert intent["body"]["source_refs"] == [source_ref]
    for artifact in store.records("executor_artifact"):
        output_ref, = artifact["body"]["output_refs"]
        model_receipt = store.get(**output_ref)
        assert model_receipt["kind"] == "model_response_receipt"
        assert model_receipt["body"]["call_ref"]["id"] == \
            f"call-{artifact['body']['work_id']}"
        proposal = store.get("board_proposal", artifact["body"]["work_id"])
        outcome = store.get(**proposal["body"]["outcome_ref"])
        assert ref(artifact) in outcome["body"]["result_refs"]
    totals = journal.usage_totals("mission")
    assert next(item["amount"] for item in totals if item["unit"] == "total_tokens") == 60
    assert result["mission_status"] == "open"  # The executor cannot certify science.
