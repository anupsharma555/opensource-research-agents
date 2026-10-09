"""Build a recorded storage workflow that both Atlas and Codex can inspect.

The fixture uses the repository's simulated clinical benchmark, real local
storage operations and reference provider adapters. It executes no model or
network call. Parent/child snapshots are read back from accepted records, with
their exact sources, agents, configurations and event history attached.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

from ora.agents import (
    COMMON_TOOL_IDS,
    AgentRegistry,
    CapabilityCatalog,
    ConfigurationRegistry,
    run_local_tool,
)
from ora.agents.capabilities import content_hash
from ora.benchmarks import load_scenario
from ora.contracts.models import EvidencePacket
from ora.evidence import EvidenceLibrary
from ora.retrieval import record_delivery

from .inspection import load_snapshot, ref, save_snapshot
from .store import StateStore

ROLE_NAMES = {
    "literature_acquisition": "Finch",
    "evidence_preparation_synthesis": "Sage",
    "independent_methods_reviewer_critic": "Mira",
    "questions_hypotheses_ranking": "Cora",
    "experimental_design": "Theo",
    "data_management": "Dara",
    "analysis": "Rowan",
    "project_collaboration_planning": "Vale",
    "shared_research_memory_completion": "Remy",
}
SAMPLE_ROLES = {
    "agent-acquisition": "literature_acquisition", "agent-synthesis": "evidence_preparation_synthesis",
    "agent-critique": "independent_methods_reviewer_critic", "agent-planning": "project_collaboration_planning",
}
ATTEMPT_INPUTS = {
    "attempt-a1": ["A1"], "attempt-a2-failed": [], "attempt-a2": ["A2"],
    "attempt-critique": ["A1", "A2", "A3"], "attempt-methods-failed": ["A4"],
    "attempt-methods-wait": ["A4"], "attempt-sibling": [],
}
SOURCE_TIMES = {"A1": "14:04", "A2": "14:06", "A3": "14:08", "A4": "14:15"}


def _copy_refs(value, agent_ids):
    if isinstance(value, str):
        return agent_ids.get(value, value)
    if isinstance(value, dict):
        return {key: _copy_refs(item, agent_ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_refs(item, agent_ids) for item in value]
    return value


def build_recorded_workflow(root: Path, state_dir: Path) -> dict:
    """Return saved parent/child projections; identical reruns reuse accepted IDs."""
    event_clock = [datetime.fromisoformat("2026-10-08T14:00:00+00:00").timestamp()]
    files = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
             for folder in ("src/ora/state", "src/ora/agents", "src/ora/retrieval", "src/ora/evidence")
             for path in sorted((root / folder).glob("*.py"))}
    build_id = "build-" + content_hash(files)
    store = StateStore(state_dir, clock=lambda: event_clock[0], record_context={
        "proof_basis": "offline_test", "timing_basis": "controlled_clock",
        "implementation_ref": f"implementation_manifest:{build_id}:1",
    })
    store.transact("record-implementation", {"files": files}, lambda tx: tx.put(
        "implementation_manifest", build_id, {"files": files, "scope": "Recorded workflow source modules; not a Git commit or complete environment"},
    ))
    mission, run = "recorded-depression-review", "recorded-data-run"
    identities = AgentRegistry(store, root)
    actors = {
        role: identities.register(f"register-{role}", mission_id=mission, run_id=run, role_id=role, display_name=name)
        for role, name in ROLE_NAMES.items()
    }
    catalog = CapabilityCatalog.with_common(
        handlers={"evidence_lookup": lambda args: {"copy_ref": ref(store.get("source_copy", args["source_id"]))}},
        implementation_refs={"evidence_lookup": "sha256:" + files["src/ora/state/demo.py"]},
    )
    configurations = ConfigurationRegistry(store, root, catalog, available_skill_capabilities={"source_read_by_id", "study_appraisal"})
    common_grants = {f"common:{tool}" for tool in COMMON_TOOL_IDS}
    configs = {}
    for role, actor in actors.items():
        choices = ({"evidence-linking": "concise"} if role == "evidence_preparation_synthesis" else
                   {"evidence-linking": "concise", "study-appraisal": "concise"} if role == "independent_methods_reviewer_critic" else {})
        configs[role] = configurations.save_configuration(
            f"configure-{role}", instance_id=actor["id"], mission_id=mission,
            task_scope="recorded-data integration", provider="groq",
            endpoint="https://api.groq.com/openai/v1/chat/completions", model_id="openai/gpt-oss-120b",
            settings={"max_completion_tokens": 8192}, reasoning_settings={"reasoning_effort": "high", "include_reasoning": False},
            instructions="Use inspected evidence and distinguish reported results from interpretation. This recorded fixture makes no model call.",
            skill_choices=choices, grants=common_grants | {f"skill:{skill}" for skill in choices}, input_refs=[],
        )
    library = EvidenceLibrary(store)
    scenario = load_scenario(root / "benchmarks/v1", "syn-abstract-disagreement")
    source_records = {}
    def retain_source(source_id):
        if source_id in source_records:
            return
        source = next(item for item in scenario["sources"] if item["source_id"] == source_id)
        retrieved_at = f"2026-10-08T{SOURCE_TIMES[source_id]}:00Z"
        event_clock[0] = datetime.fromisoformat(retrieved_at).timestamp()
        surface = "metadata" if source["surface"] == "access_notice" else "abstract"
        publication = library.register_publication(
            f"publication-{source_id}", identifiers={"doi": f"10.5555/ora.fixture.{source_id.lower()}"},
            title=f"Recorded clinical example {source_id}",
            verification="Project-owned recorded fixture identifier; not a resolver or actual publication claim",
        )["publication_ref"]
        request = {
            "mission_id": mission, "task_id": "task-root", "request_id": f"retrieve-{source_id}",
            "attempt_id": f"reference-attempt-{source_id}", "agent_instance_id": actors["literature_acquisition"]["id"],
            "provider": "reference-adapter", "route": "recorded-abstracts", "operation": "search",
            "query": "cognitive behavioural therapy depression six weeks", "filters": {},
            "requested_at": "2026-10-08T14:00:00Z", "requested_window": {"offset": 0, "limit": 1},
        }
        delivery = record_delivery(store, request, {
            "status": "ok", "count": 1, "offset": 0,
            "items": [{"id": source_id, "url": f"https://example.test/evidence/{source_id}", "title": f"Recorded clinical example {source_id}", "content": source["body"], "surface": surface, "origin": "original_source", "rights_status": "permitted", "access_status": "available", "scientific_metadata": {"authors": [{"name": "Fixture author"}], "language": "en", "venue": "Project-owned simulated benchmark"}}],
        }, delivery_id=f"delivery-{source_id}", adapter="count_offset", retrieved_at=retrieved_at)
        encounter = library.record_retrieval_hit(f"encounter-{source_id}", publication, delivery["accepted_hit_refs"][0])["encounter_ref"]
        retained = library.add_copy(
            f"copy-{source_id}", publication, encounter, content=source["body"].encode(), content_sha256=None,
            mime_type="text/plain", source_version=source["source_version"], access_surface=surface,
            retrieved_at=retrieved_at, rights_basis="Project-owned simulated-data benchmark",
            retain_bytes=True, reference=f"https://example.test/evidence/{source_id}",
            passages=[{"span_id": span["span_id"], "locator": span["locator"], "text": span["quote"], "start": span["start"], "end": span["end"]} for span in source["spans"]],
            inspection_state="checked", validation_record="fixture-text-and-offset-check-v1",
            scientific_metadata={"authors": [{"name": "Fixture author"}], "language": "en", "venue": "Project-owned simulated benchmark"},
            mapping_version="recorded-demo-v2",
        )
        source_records[source_id] = {**retained, "encounter_ref": encounter, "delivery_ref": delivery["delivery_ref"], "hit_ref": delivery["accepted_hit_refs"][0], "publication_ref": publication}

    actor_ids = {name: actors[role]["id"] for name, role in SAMPLE_ROLES.items()}
    role_by_id = {actors[role]["id"]: role for role in actors}
    samples = [json.loads((root / "ui/public" / filename).read_text()) for filename in ("sample-run-baseline.json", "sample-run.json")]
    saved_refs = []
    for index, template in enumerate(samples):
        selected = copy.deepcopy(template)
        selected["artifacts"] = [item for item in selected["artifacts"] if item["id"] == selected["mission"]["reportArtifactId"]]
        selected["claims"] = selected["claims"][-1:]
        stable_ids = {**actor_ids, "report-v1": "recorded-report", "report-v2": "recorded-report", "claim-benefit-v1": "recorded-claim", "claim-benefit-v2": "recorded-claim"}
        snapshot = _copy_refs(selected, stable_ids)
        snapshot["proofKind"] = "offline_test"
        snapshot["snapshotId"] = f"stage2-recorded-{'parent' if index == 0 else 'child'}"
        snapshot["sampleVersion"] = "stage2-recorded-v1"
        snapshot["mission"]["id"] = mission
        snapshot["inputQuestion"] = scenario["task"]["question"] if not index else "Revise the first finding using the second adult comparison and the peer critique."
        snapshot["parentSnapshotId"] = saved_refs[0]["id"] if index else None
        snapshot["mission"]["question"] = snapshot["inputQuestion"]
        if not index:
            snapshot["capturedAt"] = "2026-10-08T14:05:00Z"
            for event in snapshot["events"]:
                event["time"] = min(event["time"], "2026-10-08T14:04:45Z")
            for task in snapshot["tasks"]:
                if task.get("firstUsefulAt"):
                    task["firstUsefulAt"] = min(task["firstUsefulAt"], "2026-10-08T14:04:45Z")
        for message in snapshot["messages"]:
            if message["status"] in {"fulfilled", "acknowledged"}:
                message["fromAgentId"], message["toAgentId"] = message["toAgentId"], message["fromAgentId"]
        for attempt in snapshot["attempts"]:
            attempt["selectedModel"] = None
            attempt["returnedReasoning"] = {"availability": "absent"}
            attempt["resultSummary"] = "Recorded local integration step. No model call was made. " + (attempt.get("resultSummary") or "")
            if not index and attempt["id"] == "attempt-synthesis":
                attempt.update(status="completed", endedAt="2026-10-08T14:04:45Z", resultSummary="Recorded acceptance of a partial finding; the task still awaits complementary evidence. No model call was made.")
        if index:
            for record in [*snapshot["claims"], *snapshot["artifacts"]]:
                key = "supports" if "supports" in record else "citations"
                record[key].append({"sourceId": "A2", "spanId": "A2-interpretation"})
        for actor in snapshot["agents"]:
            actor["name"] = actors[role_by_id[actor["id"]]]["body"]["display_name"]
        source_provenance, detail_refs = [], []
        for source in snapshot["sources"]:
            retain_source(source["id"])
            stored = source_records[source["id"]]
            source["copyId"] = stored["copy_ref"]["id"]
            source["version"] = stored["copy_ref"]["version"]
            source["provider"] = "Recorded reference adapter"
            source_provenance.append({"sourceId": source["id"], "copyReference": stored["copy_ref"], "encounterReferences": [stored["encounter_ref"]], "origin": "reused" if index and source["id"] == "A1" else "retrieved", "fromSnapshotId": saved_refs[0]["id"] if index and source["id"] == "A1" else None})
            detail_refs += [stored[key] for key in ("publication_ref", "encounter_ref", "delivery_ref", "hit_ref", "copy_ref")]
            detail_refs += stored["passage_refs"]

        event_clock[0] = datetime.fromisoformat(snapshot["capturedAt"]).timestamp()

        def save_records(tx):
            refs = []
            for kind, items in (("task", snapshot["tasks"]), ("attempt", snapshot["attempts"]), ("message", snapshot["messages"]), ("claim", snapshot["claims"]), ("report", snapshot["artifacts"])):
                for item in items:
                    step_inputs = []
                    previous = tx.get(kind, item["id"])
                    body = {**item, "mission_id": mission, "proof_kind": "offline_test"}
                    if kind == "report":
                        body["producer_attempt_ref"] = ref(tx.get("attempt", "attempt-synthesis"))
                    if previous and previous["body"] == body:
                        saved = previous
                    else:
                        saved = tx.put(kind, item["id"], body, expected_version=previous["version"] if previous else 0)
                    refs.append(ref(saved))
                    if kind == "claim" and previous and saved["version"] != previous["version"]:
                        tx.link(saved, previous, "supersedes_claim")
                        step_inputs.append(ref(previous))
                        critique = tx.get("attempt", "attempt-critique")
                        if critique:
                            tx.link(saved, critique, "revised_after")
                            step_inputs.append(ref(critique))
                    if kind == "report":
                        claim = tx.get("claim", "recorded-claim")
                        tx.link(saved, claim, "depends_on")
                        step_inputs.append(ref(claim))
                        if previous and saved["version"] != previous["version"]:
                            tx.link(saved, previous, "supersedes_report")
                            step_inputs.append(ref(previous))
                    if kind in {"claim", "report"}:
                        for citation in item.get("citations", item.get("supports", [])):
                            retained = source_records[citation["sourceId"]]
                            passage = next(pointer for pointer in retained["passage_refs"] if pointer["id"].endswith(":" + citation["spanId"]))
                            tx.link(saved, passage, "cites")
                            step_inputs.append(passage)
                    if kind == "attempt":
                        source_ids = (["A1"] if not index else ["A1", "A2"]) if item["id"] == "attempt-synthesis" else ATTEMPT_INPUTS[item["id"]]
                        step_inputs += [source_records[source_id]["copy_ref"] for source_id in source_ids if source_id in source_records]
                    if previous is None or saved["version"] != previous["version"]:
                        tx.emit(mission, f"{kind}_saved", {"record": ref(saved), "input_refs": step_inputs})
            tx.emit(mission, "recorded_workflow_step", {"snapshot_id": snapshot["snapshotId"], "proof": "offline_test"})
            return {"references": refs}

        accepted = store.transact(f"record-step-{index}", {"snapshot": snapshot}, save_records, mission_id=mission)
        detail_refs += accepted["references"]
        # The read contract uses artifact version, not a sequence inferred by the UI.
        for artifact in snapshot["artifacts"]:
            pointer = next(item for item in accepted["references"] if item["kind"] == "report" and item["id"] == artifact["id"])
            artifact["version"] = pointer["version"]
        output_id = snapshot["mission"]["reportArtifactId"]
        outputs = [pointer for pointer in accepted["references"] if pointer["kind"] == "report" and pointer["id"] == output_id]
        attempt_provenance = []
        for attempt in snapshot["attempts"]:
            role = role_by_id[attempt["agentId"]]
            previous = configs[role]
            supplied_ids = (["A1"] if not index else ["A1", "A2"]) if attempt["id"] == "attempt-synthesis" else ATTEMPT_INPUTS[attempt["id"]]
            exact_inputs = [item["copyReference"] for item in source_provenance if item["sourceId"] in supplied_ids]
            helper_packet = None
            if index and attempt["id"] == "attempt-synthesis":
                first_source = source_records["A1"]
                first_copy = store.get(**first_source["copy_ref"])
                first_passage = store.get(**first_source["passage_refs"][0])
                packet_body = EvidencePacket(
                    mission_id=mission,
                    id="recorded-evidence-A1", version=1, kind="published_evidence",
                    artifact={"id": first_copy["id"], "version": first_copy["version"], "sha256": first_copy["body"]["content_sha256"]},
                    locators=[first_passage["body"]["locator"]], validation_record="fixture-text-and-offset-check-v1",
                    access_surface="abstract", summary="Project-owned fixture passage; not a live publication result.", limitations=[],
                ).model_dump(mode="json")
                helper_packet = store.transact("helper-packet", packet_body, lambda tx: tx.put("evidence_packet", packet_body["id"], packet_body), mission_id=mission)
                exact_inputs.append(ref(helper_packet))
                detail_refs.append(ref(helper_packet))
            input_refs = [{**pointer, "sha256": content_hash(store.get(**pointer)["body"])} for pointer in exact_inputs]
            config = configurations.save_configuration(
                f"inputs-{index}-{attempt['id']}", instance_id=actors[role]["id"], mission_id=mission,
                task_scope="recorded-data integration", provider="groq",
                endpoint=previous["body"]["endpoint"], model_id=previous["body"]["model_id"],
                settings=previous["body"]["settings"], reasoning_settings=previous["body"]["reasoning_settings"],
                instructions=previous["body"]["instructions"], skill_choices={name: skill["variant"] for name, skill in previous["body"]["skills"].items()},
                grants=set(previous["body"]["grants"]), input_refs=input_refs,
                previous_configuration_id=previous["id"], change_reason="Pin the exact evidence supplied to this recorded step",
            )
            # Prepared call configurations are inspectable but never labeled invoked.
            call = configurations.start_call(f"prepare-{index}-{attempt['id']}", call_id=f"recorded-call-{index}-{attempt['id']}", configuration_id=config["id"], mission_id=mission, task_id=attempt["taskId"], attempt_id=attempt["id"], loaded_skill_ids=list(config["body"]["skills"]), exposed_tool_ids=sorted(COMMON_TOOL_IDS))
            if helper_packet is not None:
                tool_receipt = run_local_tool(store, catalog, "recorded-local-lookup", mission_id=mission,
                    call_id=call["id"], manifest_version=call["version"], tool_id="evidence_lookup",
                    arguments={"source_id": source_records["A1"]["copy_ref"]["id"]})
                skill_receipt = configurations.invoke_skill("recorded-local-evidence-link", mission_id=mission,
                    call_id=call["id"], manifest_version=call["version"], skill_id="evidence-linking",
                    evidence_ref={**ref(helper_packet), "sha256": content_hash(helper_packet["body"])},
                    arguments={"claim": first_passage["body"]["text"], "locator": first_passage["body"]["locator"], "relation": "supports"})
                for receipt in (tool_receipt, skill_receipt):
                    activity = identities.record_activity("activity-" + receipt["kind"], mission_id=mission,
                        instance_id=actors[role]["id"], target_kind=receipt["kind"], target_id=receipt["id"],
                        target_version=receipt["version"], task_id=attempt["taskId"], attempt_id=attempt["id"], call_id=call["id"])
                    detail_refs += [ref(receipt), ref(activity)]
            attempt_provenance.append({"attemptId": attempt["id"], "instanceReference": ref(actors[role]), "configurationReference": ref(config), "callReferences": [ref(call)], "inputReferences": exact_inputs, "acceptedOutputReferences": outputs if attempt["id"] == "attempt-synthesis" else [], "explanation": "Saved fixture event sequence with explicitly supplied per-attempt evidence; model configuration prepared but not invoked."})
            detail_refs += [ref(actors[role]), ref(config), ref(call)]
        unique_refs = list({(item["kind"], item["id"], item["version"]): item for item in detail_refs}.values())
        saved = save_snapshot(store, f"save-snapshot-{index}", snapshot, source_provenance=source_provenance,
                              attempt_provenance=attempt_provenance, detail_refs=unique_refs, accepted_outputs=outputs,
                              parent_ref=saved_refs[0] if index else None,
                              followup_target={"kind": "report", "id": "recorded-report", "version": 1} if index else None,
                              initial_source_ids=["A1"] if index else [], timing_basis="controlled_clock")
        saved_refs.append(saved)
    return {"parent": load_snapshot(store, saved_refs[0]["id"]), "child": load_snapshot(store, saved_refs[1]["id"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--export-dir", type=Path, required=True)
    args = parser.parse_args()
    snapshots = build_recorded_workflow(Path.cwd(), args.state_dir)
    args.export_dir.mkdir(parents=True, exist_ok=True)
    for name, snapshot in snapshots.items():
        (args.export_dir / f"{name}.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"proof": "recorded_offline_integration", "provider_calls": 0, "snapshots": {name: value["snapshotId"] for name, value in snapshots.items()}}))


if __name__ == "__main__":
    main()
