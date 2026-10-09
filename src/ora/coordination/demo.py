"""Create one disposable offline concurrent run and its honest Atlas snapshot.

The callbacks are synthetic local work. No provider, source adapter, network
service, model call or scientific report is created by this fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ora.agents import AgentRegistry
from ora.state import StateStore
from ora.state.inspection import load_snapshot, save_snapshot
from ora.state.store import canonical

from .board import ProposalBoard, ref
from .capacity import CapacityPolicy, CapacityScheduler, QuotaLimit
from .executor import ConcurrentExecutor, WorkResult, WorkSpec
from .mailbox import PeerMailbox
from .traces import TraceJournal

ROOT = Path(__file__).resolve().parents[3]
MISSION = "stage3-synthetic-mission"
RUN = "stage3-synthetic-run"
SNAPSHOT_ID = "stage3-offline-concurrent-snapshot-v1"
ROLES = (
    ("literature_acquisition", "Acquisition peer"),
    ("evidence_preparation_synthesis", "Synthesis peer"),
    ("independent_methods_reviewer_critic", "Review peer"),
)
WORK = ("a-fast", "b-slow", "c-dependent")
PRIVATE_MARKER = "SYNTHETIC_PRIVATE_INSTRUCTION_NOT_FOR_EXPORT"


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def _records(store: StateStore, kind: str) -> list[dict]:
    result, after = [], ""
    while True:
        page = store.records(kind, after_id=after)
        result.extend(page)
        if len(page) < 1000:
            return result
        after = page[-1]["id"]


def _configuration_body(actor_id: str, input_ref: dict) -> dict:
    body = {"mission_id": MISSION, "instance_id": actor_id,
            "provider": "offline_fixture", "model_id": "no_model_invoked",
            "instructions": PRIVATE_MARKER,
            "instructions_sha256": hashlib.sha256(PRIVATE_MARKER.encode()).hexdigest(),
            "input_refs": [input_ref], "proof": "offline_test"}
    body["configuration_sha256"] = hashlib.sha256(canonical(body).encode()).hexdigest()
    return body


async def _run_fixture(store: StateStore, board: ProposalBoard,
                       mailbox: PeerMailbox, actors: list[dict],
                       configurations: list[dict], input_ref: dict) -> dict:
    capacity = CapacityScheduler(store, CapacityPolicy(
        permits={"model": 2, "retrieval": 1, "compute": 1},
        quotas={"fixture-route": QuotaLimit(60, requests=10, tokens=100)},
        review_reserve=0,
    ))
    slow_started = asyncio.Event()
    dependent_started = asyncio.Event()
    intervals: dict[str, dict[str, int]] = {}

    async def callback(work: WorkSpec) -> WorkResult:
        started = time.perf_counter_ns()
        if work.work_id == "a-fast":
            await slow_started.wait()
        elif work.work_id == "b-slow":
            slow_started.set()
            await dependent_started.wait()
        else:
            dependent_started.set()
        await asyncio.sleep(0.01)
        ended = time.perf_counter_ns()
        intervals[work.work_id] = {"started_ns": started, "ended_ns": ended}
        return WorkResult(
            payload={"synthetic": True, "observation": f"Local callback {work.work_id} completed",
                     "input_ref": input_ref}, used_tokens=1,
        )

    executor = ConcurrentExecutor(
        store, board, mailbox, capacity, TraceJournal(store),
        {("synthetic_callback", "1"): callback}, owner="fixture-host",
    )
    proposals = {}
    for index, name in enumerate(WORK):
        proposed = board.propose(
            f"propose-{name}", mission_id=MISSION, proposal_id=name,
            actor_id=actors[index]["id"],
            objective=f"Inspect the synthetic input for {name}",
            expected_artifact="Local callback receipt",
            next_action="Run the registered offline callback",
            prerequisite_refs=[input_ref],
            depends_on=["a-fast"] if name == "c-dependent" else [],
        )
        if proposed["status"] != "accepted":
            raise RuntimeError(f"Fixture proposal was rejected: {name}")
        proposals[name] = proposed["proposal_ref"]
        executor.submit(WorkSpec(
            work_id=name, mission_id=MISSION, run_id=RUN,
            proposal_ref=proposals[name], actor_id=actors[index]["id"],
            configuration_ref=ref(configurations[index]), input_refs=(input_ref,),
            handler_id="synthetic_callback", handler_version="1", kind="model",
            lane=name, quota_scopes=("fixture-route",), estimated_tokens=1,
            attempt_id=f"attempt-{name}",
        ))
    result = await executor.run_ready(MISSION)
    if result["work"] != {name: "completed" for name in WORK}:
        raise RuntimeError("Offline fixture did not accept all three callback results")
    fast, slow, dependent = (intervals[name] for name in WORK)
    overlap = max(fast["started_ns"], slow["started_ns"]) < min(
        fast["ended_ns"], slow["ended_ns"])
    dependent_before_slow = dependent["started_ns"] < slow["ended_ns"]
    if not overlap or not dependent_before_slow:
        raise RuntimeError("Offline fixture did not demonstrate same-run overlap")
    proof = store.transact("stage3-fixture-proof", {"intervals": intervals},
                           lambda tx: tx.put("stage3_fixture_proof", "concurrency", {
                               "mission_id": MISSION, "run_id": RUN,
                               "intervals_monotonic_ns": intervals,
                               "independent_overlap_observed": overlap,
                               "dependent_started_before_slow_finished": dependent_before_slow,
                               "provider_calls": 0, "proof": "offline_local_callbacks",
                           }), mission_id=MISSION)
    return {"result": result, "proof_ref": ref(proof), "proposal_refs": proposals}


def _projection(store: StateStore, actors: list[dict], configurations: list[dict],
                input_ref: dict, fixture: dict, deferred_ref: dict,
                request_ref: dict) -> dict:
    work = {item["id"]: item for item in _records(store, "executor_work")
            if item["body"].get("mission_id") == MISSION}
    artifacts = {item["body"]["work_id"]: item for item in _records(store, "executor_artifact")
                 if item["body"].get("mission_id") == MISSION}
    receipts = {item["body"]["work_id"]: item for item in _records(store, "executor_receipt")
                if item["body"].get("mission_id") == MISSION}
    capacity = {item["body"].get("task_id"): item for item in _records(store, "capacity_operation")
                if item["body"].get("mission_id") == MISSION}
    if len(work) != 3 or len(artifacts) != 3 or len(receipts) != 3:
        raise RuntimeError("Fixture projection needs three exact accepted contributions")
    # Capacity operation task IDs are proposal IDs in this fixture. If a future
    # capacity body changes, reconstruct from its saved attempts rather than
    # inventing callback start/finish timestamps.
    attempts, provenance, tasks = [], [], []
    for index, name in enumerate(WORK):
        candidate = next((item for item in capacity.values()
                          if item["body"].get("task_id") == name), None)
        if candidate is None or not candidate["body"].get("attempts"):
            raise RuntimeError("Accepted fixture work has no saved dispatch timing")
        attempt = candidate["body"]["attempts"][-1]
        if attempt["status"] != "completed" or attempt.get("completed_at") is None:
            raise RuntimeError("Fixture attempt lacks a saved completion")
        actor = actors[index]
        artifact = artifacts[name]
        task = {
            "id": name, "title": f"Inspect synthetic input: {name}", "status": "completed",
            "parentTaskId": "a-fast" if name == "c-dependent" else None,
            "latencyClass": "medium", "latencyBasis": "Offline callback; no provider timing",
            "activeOrWaiting": "completed", "attemptIds": [f"attempt-{name}"],
            "firstUsefulAt": artifact["created_at"],
        }
        tasks.append(task)
        attempts.append({
            "id": f"attempt-{name}", "taskId": name, "agentId": actor["id"],
            "status": "completed", "startedAt": _iso(attempt["at"]),
            "endedAt": _iso(attempt["completed_at"]), "toolCalls": [],
            "selectedModel": None, "skillVersions": [],
            "resultSummary": artifact["body"]["payload"]["observation"],
            "returnedReasoning": {"availability": "absent", "text": None},
        })
        provenance.append({
            "attemptId": f"attempt-{name}", "instanceReference": ref(actor),
            "configurationReference": ref(configurations[index]),
            "callReferences": [], "inputReferences": [input_ref],
            "acceptedOutputReferences": [],
            "explanation": "Offline local callback; executor artifact and receipt are diagnostic records, not a scientific report.",
        })
    deferred = store.get(**deferred_ref)
    tasks.append({"id": deferred["id"], "title": deferred["body"]["objective"],
                  "status": "waiting", "latencyClass": "long",
                  "latencyBasis": "Peer deferred pending more synthetic evidence",
                  "activeOrWaiting": "waiting", "attemptIds": []})
    request = store.get("mail_request", request_ref["id"])
    message = store.get("mail_message", request["body"]["message_ref"]["id"])
    messages = [{
        "id": message["id"], "requestId": request["id"],
        "fromAgentId": request["body"]["requester_id"],
        "toAgentId": request["body"]["recipient_id"],
        "taskId": "a-fast", "status": "acknowledged",
        "summary": request["body"]["question"],
        "createdAt": request["body"]["created_at"],
        "updatedAt": message["body"]["acknowledged_at"],
    }]
    detail_refs = [input_ref, fixture["proof_ref"], deferred_ref,
                   request_ref, ref(message)]
    detail_refs += [ref(item) for item in [*actors, *configurations,
                                             *work.values(), *artifacts.values(),
                                             *receipts.values(), *capacity.values()]]
    for kind in ("board_proposal", "board_claim", "board_outcome", "board_dissent",
                 "coordination_trace", "mail_outbox", "mail_request", "mail_answer",
                 "mail_message"):
        detail_refs.extend(ref(item) for item in _records(store, kind)
                           if item["body"].get("mission_id") == MISSION)
    detail_refs = list({(p["kind"], p["id"], p["version"]): p for p in detail_refs}.values())
    allowed_events = {"proposal_created", "executor_work_submitted",
                      "proposal_result_accepted", "peer_request_opened",
                      "peer_message_delivered", "peer_message_acknowledged"}
    timeline = []
    for event in store.events(MISSION):
        if event["type"] not in allowed_events:
            continue
        subjects = event["body"].get("subjects", [])
        task_id = next((item["id"] for item in subjects
                        if item.get("kind") == "task"), None)
        timeline.append({"id": event["id"], "time": event["created_at"],
                         "kind": event["type"], "taskId": task_id,
                         "summary": event["type"].replace("_", " ")})
    snapshot = {
        "schemaVersion": "1.0", "snapshotId": SNAPSHOT_ID,
        "sampleVersion": "stage3-offline-1", "capturedAt": store.now(),
        "proofKind": "offline_test", "inputQuestion": "How do three peers process one synthetic input?",
        "mission": {"id": MISSION, "version": 1,
                    "question": "How do three peers process one synthetic input?",
                    "scope": "Offline callback and coordination fixture; no scientific conclusion",
                    "status": "active", "latencyClass": "long",
                    "latencyBasis": "A peer request and deferred proposal remain open",
                    "startedAt": actors[0]["created_at"], "reportArtifactId": None},
        "agents": [{"id": actor["id"], "name": actor["body"]["display_name"],
                    "role": actor["body"]["role_id"].replace("_", " "),
                    "status": "completed"} for actor in actors],
        "tasks": tasks, "attempts": attempts, "messages": messages,
        "sources": [], "claims": [], "artifacts": [], "events": timeline,
        "diagnostics": [
            {"id": "stage3-fixture-overlap", "kind": "local_callback_overlap",
             "status": "observed", "message": "Independent local callbacks overlapped; the dependent callback began while another sibling was still active. No provider call occurred.",
             "occurredAt": store.now()},
            {"id": "stage3-deferred-next-action", "taskId": deferred["id"],
             "kind": "deferred_work", "status": "waiting",
             "message": deferred["body"]["next_action"], "occurredAt": store.now()},
            {"id": "stage3-unanswered-request", "taskId": "a-fast",
             "kind": "peer_request", "status": request["body"]["status"],
             "message": "Delivery and acknowledgment were saved; the peer question remains unanswered.",
             "occurredAt": store.now()},
        ],
    }
    return {"snapshot": snapshot, "attempt_provenance": provenance,
            "detail_refs": detail_refs}


def build_fixture(root: Path, state_dir: Path) -> dict:
    """Create a new private store, run actual local executor callbacks, and save one view."""
    if state_dir.exists():
        raise FileExistsError("Stage 3 demo requires a fresh disposable state directory")
    store = StateStore(state_dir, record_context={
        "proof_basis": "offline_test", "timing_basis": "observed_local",
        "implementation_ref": "ora.coordination.demo:stage3-offline-1",
    })
    registry = AgentRegistry(store, root)
    actors = [registry.register(
        f"stage3-register-{index}", mission_id=MISSION, run_id=RUN,
        role_id=role_id, display_name=display_name,
    ) for index, (role_id, display_name) in enumerate(ROLES)]
    input_record = store.transact("stage3-input", {}, lambda tx: tx.put(
        "stage3_fixture_input", "stage3-synthetic-input", {
            "mission_id": MISSION, "synthetic": True,
            "question": "Inspect a synthetic evidence packet",
            "content": {"observations": ["A", "B"], "uncertainty": "No source study attached"},
        }), mission_id=MISSION)
    input_ref = ref(input_record)
    configurations = [store.transact(
        f"stage3-configuration-{index}", {"actor": actor["id"], "input": input_ref},
        lambda tx, index=index, actor=actor: tx.put(
            "agent_configuration", f"stage3-config-{index}",
            _configuration_body(actor["id"], input_ref)), mission_id=MISSION,
    ) for index, actor in enumerate(actors)]
    board, mailbox = ProposalBoard(store), PeerMailbox(store)
    fixture = asyncio.run(_run_fixture(store, board, mailbox, actors,
                                       configurations, input_ref))
    deferred = board.propose(
        "stage3-deferred", mission_id=MISSION, proposal_id="d-evidence-gap",
        actor_id=actors[2]["id"], objective="Resolve the remaining synthetic evidence gap",
        expected_artifact="Source-backed appraisal when evidence exists",
        next_action="Find a permitted exact source version, then invite an independent peer",
        prerequisite_refs=[input_ref], status="deferred",
    )
    if deferred["status"] != "accepted":
        raise RuntimeError("Deferred fixture work was rejected")
    first_artifact = next(item for item in _records(store, "executor_artifact")
                          if item["body"].get("work_id") == "a-fast")
    request = mailbox.send_request(
        "stage3-peer-question", mission_id=MISSION, request_id="stage3-question",
        message_id="stage3-question-message", requester_id=actors[0]["id"],
        recipient_id=actors[2]["id"],
        question="Can an independent peer appraise this synthetic callback result?",
        expected_artifact="Independent, explicitly limited appraisal",
        cause_ref=ref(first_artifact), task_ref=fixture["proposal_refs"]["a-fast"],
        evidence_refs=[ref(first_artifact)],
        payload={"question": "Appraise the synthetic callback result",
                 "artifact_ref": ref(first_artifact)}, expected_latency="long",
    )
    if request["status"] != "accepted":
        raise RuntimeError("Peer question was rejected")
    mailbox.mark_delivered("stage3-delivered", mission_id=MISSION,
                           message_id="stage3-question-message")
    mailbox.acknowledge("stage3-ack", mission_id=MISSION,
                        message_id="stage3-question-message",
                        recipient_id=actors[2]["id"])
    # Keep the unanswered question above intact. A separate exchange records a
    # partial critique, not requester acceptance or a scientific report.
    dissent = board.dissent(
        "stage3-dissent", mission_id=MISSION, dissent_id="stage3-limited-critique",
        proposal_ref=deferred["proposal_ref"], actor_id=actors[2]["id"],
        critique="The local callback demonstrates execution, but the synthetic input contains no study passage to appraise.",
        evidence_refs=[input_ref],
    )
    partial_request = mailbox.send_request(
        "stage3-partial-question", mission_id=MISSION,
        request_id="stage3-partial-review", message_id="stage3-partial-message",
        requester_id=actors[1]["id"], recipient_id=actors[2]["id"],
        question="Which part of the synthetic input can be appraised now?",
        expected_artifact="A scoped critique with remaining evidence needs",
        cause_ref=deferred["proposal_ref"], task_ref=deferred["proposal_ref"],
        evidence_refs=[input_ref], payload={"synthetic": True},
    )
    if dissent["status"] != "accepted" or partial_request["status"] != "accepted":
        raise RuntimeError("Partial review fixture could not save its exact context")
    mailbox.mark_delivered("stage3-partial-delivered", mission_id=MISSION,
                           message_id="stage3-partial-message")
    mailbox.acknowledge("stage3-partial-ack", mission_id=MISSION,
                        message_id="stage3-partial-message", recipient_id=actors[2]["id"])
    partial_answer = mailbox.record_answer(
        "stage3-partial-answer", mission_id=MISSION, request_id="stage3-partial-review",
        answer_id="stage3-partial-answer", responder_id=actors[2]["id"],
        artifact_ref=dissent["dissent_ref"], partial=True,
    )
    if partial_answer["status"] != "accepted":
        raise RuntimeError("Partial review fixture answer was rejected")
    projection = _projection(store, actors, configurations, input_ref, fixture,
                             deferred["proposal_ref"], request["request_ref"])
    saved = save_snapshot(
        store, "stage3-save-snapshot", projection["snapshot"],
        source_provenance=[], attempt_provenance=projection["attempt_provenance"],
        detail_refs=projection["detail_refs"], accepted_outputs=[],
        timing_basis="observed_local",
    )
    result = load_snapshot(store, saved["id"])
    if result["artifacts"] or result["mission"]["reportArtifactId"] is not None:
        raise RuntimeError("An offline callback must not become a scientific report")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=None,
                        help="Optional new directory name under .local/stage3")
    args = parser.parse_args()
    name = args.name or f"demo-{uuid.uuid4().hex[:12]}"
    if not name.startswith("demo-") or Path(name).name != name:
        parser.error("Name must be a single demo-* directory name")
    destination = ROOT / ".local" / "stage3" / name
    snapshot = build_fixture(ROOT, destination / "state")
    destination.mkdir(parents=True, exist_ok=True)
    export = destination / "snapshot.json"
    with export.open("x") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"proof": "offline_local_callbacks", "provider_calls": 0,
                      "state_dir": str(destination / "state"),
                      "snapshot_id": snapshot["snapshotId"], "snapshot_json": str(export)}))


if __name__ == "__main__":
    main()
