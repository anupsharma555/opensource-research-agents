"""Compare whether an agent can recover earlier evidence after its context changes.

Without --allow-live, print the test plan without creating run state. An enabled
test uses synthetic evidence, the selected GPT-OSS credential profile and a
shared call allowance in a fresh workspace, saving requests, results and
evaluation records. Stop when the model departs from the requested test steps,
a provider reports an error or quota limit, or the call allowance is exhausted.
This script does not start Atlas, resume existing runs, change credential holds
or assess the scientific correctness of the answer.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ora.benchmarks.context_recovery import digest, evaluate_recovery, load_case  # noqa: E402
from ora.contracts.models import LiveStartRequest  # noqa: E402
from ora.coordination.board import ref  # noqa: E402
from ora.credentials import GroqCredentialProfiles  # noqa: E402
from ora.evidence import read_source_window  # noqa: E402
from ora.mission import MissionService  # noqa: E402
from ora.mission.service import _key  # noqa: E402
from ora.models import GroqSelectedAdapter, ModelSession  # noqa: E402
from ora.models.groq import DEFAULT_REASONING_EFFORT, MODEL, URL  # noqa: E402
from ora.state import StateStore  # noqa: E402
from ora.state.store import canonical  # noqa: E402


class QualificationStop(RuntimeError):
    """A bounded test must stop for inspection before another external call."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_json(path: Path, value: object) -> None:
    """Create a private receipt, never overwrite earlier evidence."""
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as out:
        json.dump(value, out, indent=2)
        out.write("\n")


class BatchBudget:
    """Cross-process serial test allowance; every attempted HTTP call is charged.

    A latched stop cannot be reset by another case. Creating a different ledger
    requires a separately authorized batch; this helper never creates one itself.
    """

    def __init__(self, path: Path, max_calls: int, profile: str, *, transport=None):
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("A positive authorized cross-batch call limit is required")
        self.path, self.max_calls, self.profile = Path(path), max_calls, profile
        self.inner_transport = transport or urllib.request.urlopen
        self.request_guard = None
        self.lock = None
        self.data = None

    def __enter__(self):
        self.lock = os.fdopen(os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600), "w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                self.data = json.loads(self.path.read_text())
                if (self.data.get("max_calls"), self.data.get("profile")) != (
                    self.max_calls, self.profile,
                ):
                    raise QualificationStop("Batch profile or authorized allowance changed")
                if self.data.get("stop_reason"):
                    raise QualificationStop("Batch already stopped; inspect its saved reason")
            else:
                self.data = {"schema": "ora.guided_live_budget.v1", "max_calls": self.max_calls,
                             "profile": self.profile, "calls": 0, "attempts": [],
                             "stop_reason": None, "created_at": now()}
                save_json(self.path, self.data)
            return self
        except BaseException:
            self.lock.close()
            raise

    def __exit__(self, *_args):
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()

    @property
    def calls(self):
        return self.data["calls"]

    def _save(self):
        temporary = self.path.with_name(self.path.name + ".pending")
        save_json(temporary, self.data)
        os.replace(temporary, self.path)

    def stop(self, reason: str):
        if not self.data["stop_reason"]:
            self.data["stop_reason"] = reason
            self.data["stopped_at"] = now()
            self._save()

    def transport(self, request, *, timeout):
        packet = json.loads(request.data)
        if request.full_url != URL or packet.get("model") != MODEL or packet.get("reasoning_effort") != DEFAULT_REASONING_EFFORT or packet.get("max_completion_tokens") != 8192:
            self.stop("Selected model, endpoint, reasoning effort or 8192-token output setting changed")
            raise QualificationStop(self.data["stop_reason"])
        if self.data["stop_reason"] or self.calls >= self.max_calls:
            self.stop("Authorized cross-batch call allowance exhausted")
            raise QualificationStop(self.data["stop_reason"])
        if self.request_guard is not None:
            try:
                self.request_guard(packet)
            except QualificationStop as exc:
                self.stop(str(exc))
                raise
        self.data["calls"] += 1
        self.data["attempts"].append({"index": self.calls, "started_at": now(),
                                     "request_sha256": digest(packet)})
        self._save()  # Reserve before HTTP; uncertain calls still consume the allowance.
        try:
            return self.inner_transport(request, timeout=timeout)
        except BaseException:
            self.stop("Provider transport failed; no automatic retry in this batch")
            raise


def model_receipts(host, run_id: str) -> list[dict]:
    mission_id = host._run(run_id)["body"]["mission_id"]
    return sorted([row for row in host.store.records("model_response_receipt")
                   if row["body"].get("mission_id") == mission_id], key=lambda row: row["created_at"])


def advance_one(host, run_id: str, budget: BatchBudget, expected_tool: dict | None,
                *, timeout_seconds: float = 180, expect_rejection: bool = False) -> dict:
    """Allow one authentic response and its expected local tool, checking before execution."""
    before = len(model_receipts(host, run_id))
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        rows = model_receipts(host, run_id)
        if len(rows) > before and rows[-1]["body"].get("status") not in {"complete", "tool_requested"}:
            raise QualificationStop("Provider returned a partial, quota, error or other non-success response")
        if budget.data["stop_reason"]:
            raise QualificationStop(budget.data["stop_reason"])
        control = host._control(run_id, "agent-synthesis")
        phase = control["body"]["phase"]
        if phase == "ready_tool":
            call = control["body"]["tool_call"]
            if expected_tool is None or call["name"] != expected_tool["name"] or call["arguments"] != expected_tool["arguments"]:
                raise QualificationStop("Actual model tool choice differs from the declared guided protocol")
        if len(rows) > before:
            if phase == "ready_model":
                if expected_tool is None:
                    raise QualificationStop("Expected direct final answer but model continued tool work")
                results = [item for item in host.store.records("context_tool_result")
                           if item["body"].get("response_ref") == ref(rows[-1])]
                if len(results) != 1:
                    raise QualificationStop("Expected one exact saved local reader result")
                exchange = json.loads(host.store.read_blob(results[0]["body"]["blob"]["sha256"]))
                result = json.loads(exchange["content"])
                if expect_rejection:
                    if result.get("status") != "rejected":
                        raise QualificationStop("Revoked source read did not produce the expected rejection")
                elif not result.get("window_ref") or not result.get("passage_refs"):
                    raise QualificationStop("Local reader rejected or failed before the next model call")
                return rows[-1]
            if phase == "done":
                if expected_tool is not None:
                    raise QualificationStop("Model finished before the next staged evidence event")
                return rows[-1]
        if phase in {"waiting", "retry_at"}:
            raise QualificationStop("Host entered a wait; inspect saved state before another test")
        host._drive_run(host._run(run_id))
        time.sleep(.01)
    raise QualificationStop("Observation deadline reached; close host and inspect pending work")


def plan(request_id: str, condition: str, profile: str, max_calls: int) -> dict:
    return {"schema": "ora.guided_recovery_plan.v1", "request_id": request_id,
            "run_id": _key("run-", request_id), "condition": condition,
            "credential_profile": profile, "model": MODEL,
            "reasoning_effort": DEFAULT_REASONING_EFFORT,
            "max_batch_calls": max_calls, "planned_case_calls": 4,
            "protocol": "guided_reader_mechanism_comparison_not_spontaneous_discovery",
            "no_atlas_server": True, "no_credential_hold_changes": True,
            "case_sha256": digest(load_case(ROOT)),
            "imposed_context_allowance": ({"tokens": 8000, "target_fraction": .70,
                                            "basis": "qualification_imposed_limit",
                                            "actual_provider_observation": False}
                                           if condition == "compacted_recovery" else None)}


def _reader(copy: dict, count: int, *, mode="replace") -> dict:
    return {"name": "read_source_window", "arguments": {
        "source_id": copy["id"], "source_version": copy["version"],
        "block_offset": 0, "char_offset": 0, "char_limit": count, "selection_mode": mode,
    }}


def _event(host, run_id: str, text: str, action: dict | None = None):
    feedback = {"guided_evaluation_event": text}
    if action:
        feedback["next_requested_action"] = action
    host._update_control(host._control(run_id, "agent-synthesis"), feedback=feedback)


def run_case(folder: Path, selected: dict, budget: BatchBudget, *, transport_mode="live") -> dict:
    """Run one fresh case; transport_mode is offline_test only in injected unit tests."""
    if selected["credential_profile"] != budget.profile or selected["max_batch_calls"] != budget.max_calls:
        raise QualificationStop("Case selection differs from the authorized batch profile/allowance")
    if folder.exists():
        raise QualificationStop("Case output must be a fresh directory; existing state is never resumed")
    if not GroqCredentialProfiles(ROOT).is_run_enabled(selected["credential_profile"], selected["run_id"]):
        raise QualificationStop("Selected profile/run is held; runner will not change the hold")
    if budget.max_calls - budget.calls < selected["planned_case_calls"]:
        raise QualificationStop("Remaining batch allowance cannot fit the declared four-call case")
    folder.mkdir(mode=0o700)
    save_json(folder / "plan.json", selected)
    source_paths = [*sorted((ROOT / "src/ora").rglob("*.py")), Path(__file__),
                    ROOT / "benchmarks/context_recovery/case.json",
                    ROOT / "benchmarks/context_recovery/withheld/reference.json"]
    source_manifest = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in source_paths}
    save_json(folder / "source-at-start.json", {"recorded_at": now(), "sha256": source_manifest,
                                              "proof": "checkout bytes at driver startup"})
    state = folder / "state"
    store = StateStore(state)
    session = ModelSession(store, ROOT, adapter=GroqSelectedAdapter(ROOT, transport=budget.transport))
    host = MissionService(ROOT, state, model_session=session, execution_mode=transport_mode,
                          start_supervisor=False)
    case = load_case(ROOT)
    condition = selected["condition"]
    rows = []
    revoked = None
    try:
        run = host.start(LiveStartRequest(
            request_id=selected["request_id"], credential_profile=selected["credential_profile"],
            question=case["question"] + " This is a guided staged evaluation. Follow each host_feedback evaluation event exactly, call only its requested read_source_window, and wait for the final event before answering. Use no discovery or peer tools.",
            evidence_text=case["early"] + case["pressure_unit"] * case["pressure_repetitions"],
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        source = control["body"]["source_refs"][0]
        initial = read_source_window(host.evidence, "evaluation-early", source,
                                     block_offset=0, char_offset=0, char_limit=len(case["early"]))
        early = initial["passage_refs"][0]
        host._update_control(control, selected_passage_refs={source["id"]: [early]},
                             read_window_refs=[initial["window_ref"]])
        action = _reader(source, 16000)
        _event(host, run.run_id, "Opening evidence is supplied. Read the appendix using this exact reader request; do not answer yet.", action)
        def first_guard(packet):
            text = canonical(packet)
            if case["early"] not in text or case["late"] in text:
                raise QualificationStop("Initial input does not match the staged evidence condition")
        budget.request_guard = first_guard
        rows.append(advance_one(host, run.run_id, budget, action))
        control = host._control(run.run_id, "agent-synthesis")
        if condition == "compacted_recovery":
            host._update_control(control, input_budget={"observed_limit_tokens": 8000,
                                                       "target_fraction": .70,
                                                       "basis": "qualification_imposed_limit",
                                                       "actual_provider_observation": False})
        elif condition == "revoked_source":
            use = host.store.resolve("source_use.copy", f"{source['id']}:{source['version']}")
            revoked = host.store.transact("evaluation-revoke", {}, lambda tx: ref(tx.put(
                "source_use", use["id"], {**use["body"], "allowed_uses": [], "policy_status": "revoked"},
                expected_version=use["version"],
            )), mission_id=control["body"]["mission_id"])
        action = _reader(source, len(case["early"]))
        _event(host, run.run_id, "Revisit the opening passage using this exact reader request. Do not answer until the later event arrives. If access was revoked, observe the rejection and do not reproduce restricted text.", action)
        def pressure_guard(packet):
            text = canonical(packet)
            present = case["early"] in text
            if present != (condition == "unchanged_context") or case["late"] in text:
                raise QualificationStop("Pressure input does not match its declared comparison condition")
        budget.request_guard = pressure_guard
        rows.append(advance_one(host, run.run_id, budget, action,
                                expect_rejection=condition == "revoked_source"))
        saved_run = host._run(run.run_id)
        late_source = host._supplied_source({
            **saved_run, "id": run.run_id + "-late-event",
            "body": {**saved_run["body"], "profile": {
                **saved_run["body"]["profile"], "evidenceText": case["late"],
            }},
        })
        window = read_source_window(host.evidence, "evaluation-late", late_source,
                                    block_offset=0, char_offset=0, char_limit=len(case["late"]))
        late = window["passage_refs"][0]
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, source_refs=[source, late_source], selected_passage_refs={
            **control["body"].get("selected_passage_refs", {}), late_source["id"]: [late],
        })
        action = _reader(late_source, len(case["late"]), mode="append")
        _event(host, run.run_id, "A new source is now available. Read its exact checked passage using the supplied request; final synthesis follows next.", action)
        budget.request_guard = None
        rows.append(advance_one(host, run.run_id, budget, action))
        _event(host, run.run_id, "Now provide your direct final answer to the research question. Compare permitted evidence, state limitations, and cite exact passage record IDs in brackets. Do not call a tool.")
        rows.append(advance_one(host, run.run_id, budget, None))
        report = host.store.get(**host._control(run.run_id, "agent-synthesis")["body"]["report_ref"])
        selection = {"case_sha256": digest(case), "run_id": run.run_id, "condition": condition,
                     "early_intent_ref": rows[0]["body"]["intent_ref"],
                     "pressure_intent_ref": rows[1]["body"]["intent_ref"],
                     "recovery_intent_ref": rows[2]["body"]["intent_ref"],
                     "final_response_ref": ref(rows[3]), "report_ref": ref(report),
                     "early_passage_ref": early, "late_passage_ref": late,
                     "revoked_source_use_ref": revoked}
        save_json(folder / "selection.json", selection)
        score = evaluate_recovery(host.store, ROOT, selection)
        save_json(folder / "receipt.json", score)
        if not score["mechanical_pass"]:
            raise QualificationStop("Saved-run mechanical checks failed; inspect the receipt before further calls")
        return score
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, QualificationStop) else type(exc).__name__
        budget.stop(reason)
        save_json(folder / "stopped.json", {"at": now(), "reason": reason,
                                           "batch_calls": budget.calls, "state_preserved": True})
        raise
    finally:
        budget.request_guard = None
        host.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition", choices=load_case(ROOT)["conditions"], required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--credential-profile", choices=("primary", "backup"), required=True)
    parser.add_argument("--max-calls", type=int, required=True, help="Authorized total for the whole serial batch")
    parser.add_argument("--batch-ledger", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-live", action="store_true")
    args = parser.parse_args()
    selected = plan(args.request_id, args.condition, args.credential_profile, args.max_calls)
    if not args.allow_live:
        print(json.dumps(selected, indent=2))
        return 0
    output = args.output_dir.resolve()
    ledger = args.batch_ledger.resolve()
    if not output.is_relative_to(ROOT / ".local") or not ledger.is_relative_to(ROOT / ".local"):
        parser.error("Keep this isolated comparison and its shared ledger under ignored .local")
    with BatchBudget(ledger, args.max_calls, args.credential_profile) as budget:
        result = run_case(output, selected, budget)
    print(json.dumps({"receipt": str(output / "receipt.json"),
                      "mechanical_pass": result["mechanical_pass"],
                      "semantic_review": result["semantic_review"], "qualified": result["qualified"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
