"""One explicit natural search task on the real host, with no prescribed tools.

The default prints the case. --live scopes the selected profile to a private
runtime copy. Accounting-only observation requires two explicit opt-in flags.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUEST_ID = "doe-cool-roof-natural-20261015-v1"
QUESTION = ("Find official U.S. Department of Energy guidance on cool roofs. "
            "In at most 150 words, explain how a cool roof works and one climate-related "
            "tradeoff, with a supporting source link.")
PROFILE = Path(".local/groq-credential-profiles.json")


def _atomic_json(path: Path, value: dict) -> None:
    # Bootstrap without importing the mutable checkout's ora modules. Live
    # runtime imports happen only after the exact private snapshot is ready.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(name, path)


def freeze(root: Path, folder: Path, run_id: str, credential_profile: str = "primary") -> dict:
    """Snapshot the full normal roles/skills/code; no reduced capability menu."""
    runtime = folder / "runtime"
    hashes = {}
    for directory in ("src/ora", "roles", "runtime_skills"):
        for source in sorted((root / directory).rglob("*")):
            if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc":
                continue
            relative = source.relative_to(root)
            target = runtime / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            hashes[str(relative)] = hashlib.sha256(target.read_bytes()).hexdigest()
    original = (root / PROFILE).read_bytes() if (root / PROFILE).exists() else None
    policy = json.loads(original) if original else {"version": 1, "profiles": {}}
    if credential_profile not in {"primary", "backup"}:
        raise ValueError("Select Primary or Backup explicitly")
    for name in ("primary", "backup"):
        policy["profiles"].setdefault(name, {}).update(
            enabled=name == credential_profile,
            allowed_run_ids=[run_id] if name == credential_profile else [],
        )
    _atomic_json(runtime / PROFILE, policy)
    if (root / ".env").exists():
        (runtime / ".env").symlink_to(root / ".env")
    manifest = {"files": hashes, "original_profile_sha256": hashlib.sha256(original).hexdigest() if original else None,
                "runtime_profile": policy, "original_profile_changed": False,
                "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    _atomic_json(folder / "source-manifest.json", manifest)
    return manifest


def _state_digest(folder: Path) -> str:
    """Fingerprint a closed parent's files without opening its state store."""
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(folder)).encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def run_case(root: Path, folder: Path, *, execution_mode="live", deadline_seconds=600,
             model_limit=8, token_limit=55_000, retrieval_limit=4,
             accounting_only=False, until_complete=False, start_request: dict | None = None,
             followup_state: Path | None = None, followup_request: dict | None = None) -> dict:
    if start_request is not None and followup_request is not None:
        raise ValueError("Choose a start or follow-up request")
    if accounting_only is not True and any(value is None for value in (
            model_limit, token_limit, retrieval_limit)):
        raise ValueError("Unlimited aggregate accounting needs explicit authorization")
    if accounting_only != (until_complete is True) or (
            accounting_only and any(value is not None for value in (
                model_limit, token_limit, retrieval_limit))):
        raise ValueError("Accounting-only mode requires no aggregate limits and until-complete observation")
    if until_complete and deadline_seconds is not None:
        raise ValueError("Until-complete observation cannot have a deadline")
    if (followup_state is None) != (followup_request is None):
        raise ValueError("Follow-up state and request metadata must be supplied together")
    followup = followup_request is not None
    request_data = None
    parent_digest = None
    if followup:
        allowed = {"request_id", "parent_run_id", "parent_snapshot_id", "target_id",
                   "target_version", "question", "scope", "agent_ids", "required_agent_ids",
                   "credential_profile", "allow_vision"}
        if not isinstance(followup_request, dict) or set(followup_request) - allowed:
            raise ValueError("Follow-up metadata contains unsupported fields")
        profile = followup_request.get("credential_profile", "primary")
        if profile not in {"primary", "backup"}:
            raise ValueError("Select Primary or Backup explicitly")
        request_data = {**followup_request, "credential_profile": profile,
                        "allow_vision": followup_request.get("allow_vision", False)}
        followup_state = Path(followup_state).resolve()
        if not (followup_state.is_dir() and (followup_state / "state.sqlite3").is_file()):
            raise ValueError("Follow-up needs an existing closed parent state directory")
        parent_digest = _state_digest(followup_state)
    elif start_request is not None:
        allowed = {"request_id", "question", "scope", "evidence_text", "agent_ids",
                   "required_agent_ids", "credential_profile", "allow_vision"}
        if not isinstance(start_request, dict) or set(start_request) - allowed:
            raise ValueError("Start metadata contains unsupported fields")
        if start_request.get("credential_profile") not in {"primary", "backup"}:
            raise ValueError("Start request must select Primary or Backup")
        request_data = {**start_request, "allow_vision": start_request.get("allow_vision", False)}
    allow_vision = request_data.get("allow_vision", False) if request_data else False
    if type(allow_vision) is not bool:
        raise ValueError("Vision authorization must be an explicit boolean")
    if accounting_only and request_data is None:
        raise ValueError("Accounting-only observation requires an explicit start or follow-up request")
    folder = folder.resolve()
    private = (root / ".local/search-validation").resolve()
    if not folder.is_relative_to(private) or folder == private or folder.exists():
        raise ValueError("Use one fresh private search-validation case directory; no implicit resume")
    if followup and folder.is_relative_to(followup_state):
        raise ValueError("Follow-up output must not be inside the parent state")
    folder.mkdir(parents=True, mode=0o700)
    request_id = request_data["request_id"] if request_data else REQUEST_ID
    run_id = "run-" + hashlib.sha256(json.dumps((request_id,), separators=(",", ":")).encode()).hexdigest()[:32]
    credential_profile = request_data["credential_profile"] if request_data else "primary"
    manifest = freeze(root, folder, run_id, credential_profile)
    if followup:
        shutil.copytree(followup_state, folder / "state")
    runtime = folder / "runtime"
    if execution_mode == "live" and any(name == "ora" or name.startswith("ora.") for name in sys.modules):
        raise ValueError("Launch live execution in a fresh process before importing ora")
    sys.path.insert(0, str(runtime / "src"))
    from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
    from ora.mission import MissionService
    from ora.observability.config import runtime_export_config
    from ora.observability.evaluation import record_evaluation_observation
    from ora.retrieval.agent_validation_budget import AgentBudget, BudgetAdmissionStop, BudgetStop
    selected = {"request_id": request_id, "run_id": run_id,
                "question": request_data["question"] if request_data else QUESTION,
                "model": "openai/gpt-oss-120b", "credential_profile": credential_profile,
                "reasoning": "pending saved run configuration",
                "max_model_calls": model_limit, "max_planned_tokens": token_limit,
                "max_retrieval_attempts": retrieval_limit, "deadline_seconds": deadline_seconds,
                "accounting_only": accounting_only, "until_complete": until_complete,
                "allow_vision": allow_vision,
                "execution_mode": execution_mode, "prescribed_tool_sequence": None,
                "parent_lineage": ({key: request_data[key] for key in (
                    "parent_run_id", "parent_snapshot_id", "target_id", "target_version"
                )} if followup else None),
                "copied_parent_state_sha256": parent_digest}
    _atomic_json(folder / "case.json", selected)
    # Withheld review stays out of the submitted question and host feedback.
    _atomic_json(folder / "review-criteria.json", {
        "criteria": (["follow-up answer supported by retained permitted evidence",
                      "requested length and source link", "external retrieval allowance respected"]
                     if followup else
                     ["answer supported by inspected permitted evidence",
                      "requested scope and source links", "actual tool route and usage"]
                     if start_request else
                     ["official DOE source identity", "mechanism and climate tradeoff supported by delivered passages",
                      "at most 150 words", "source link", "actual route and checked-source use distinguished"]),
        "comparison": "none; natural choice and single-task support only",
    })
    host = None
    outcome = "observation_window_ended"
    original_http = urllib.request.AbstractHTTPHandler.do_open
    starting_threads = {thread.ident for thread in threading.enumerate()}
    closed = False
    evaluation_observation_saved = False
    started = time.time()
    controller_stop_elapsed = None
    with AgentBudget(folder / "budget.json", model_limit=model_limit, token_limit=token_limit,
                     retrieval_limit=retrieval_limit, accounting_only=accounting_only,
                     allow_vision=allow_vision) as budget:
        def observed_http(handler, http_class, request, **connection):
            if closed:
                raise BudgetAdmissionStop("case_closed")
            def perform(_handler, fullurl, data=None, timeout=None, **_kwargs):
                result = original_http(_handler, http_class, fullurl, **connection)
                if result.status >= 400:
                    raise urllib.error.HTTPError(fullurl.full_url, result.status,
                                                 result.reason, result.headers, result)
                return result
            return budget.open(perform, handler, request, timeout=getattr(request, "timeout", 120))

        urllib.request.AbstractHTTPHandler.do_open = observed_http
        try:
            host = MissionService(runtime, folder / "state", execution_mode=execution_mode,
                                  start_supervisor=False,
                                  trace_config=(runtime_export_config(runtime)
                                                if execution_mode == "live" else None))
            schedule = host._schedule_model
            def bounded_schedule(control, **kwargs):
                if budget.data.get("stop_reason"):
                    return False
                if model_limit is not None and len(budget.data["model_attempts"]) >= model_limit:
                    budget.stop("model_attempt_cap")
                return schedule(control, **kwargs)
            host._schedule_model = bounded_schedule
            if followup:
                parent = host._run(request_data["parent_run_id"])
                if parent["body"]["status"] != "completed":
                    raise ValueError("Follow-up parent run must be closed and completed")
                run = host.followup(LiveFollowupRequest.model_validate(request_data))
            else:
                run = host.start(LiveStartRequest.model_validate(
                    request_data if request_data else {
                        "request_id": REQUEST_ID, "question": QUESTION,
                        "credential_profile": "primary", "agent_ids": ["agent-synthesis"],
                        "allow_vision": False,
                    }))
            assert run.run_id == run_id
            selected["reasoning"] = host._run(run_id)["body"]["profile"]["reasoningSettings"]
            _atomic_json(folder / "case.json", selected)
            while deadline_seconds is None or time.time() - started < deadline_seconds:
                host._drive_run(host._run(run_id))
                status = host._run(run_id)["body"]["status"]
                if budget.data.get("stop_reason"):
                    outcome = budget.data["stop_reason"]
                    break
                if status == "completed":
                    outcome = "completed"
                    break
                if status in {"cancelled", "failed"}:
                    outcome = "mission_" + status
                    budget.stop(outcome)
                if accounting_only:
                    controls = host._controls(run_id)
                    if any(item["body"].get("operator_credential_hold") for item in controls):
                        outcome = "operator_credential_hold"
                        budget.stop(outcome)
                    waits = [item["body"].get("capacity_wait_until") for item in controls]
                    if any(isinstance(wake, (int, float)) and wake - time.time() >= 3600
                           for wake in waits):
                        outcome = "long_provider_wait"
                        budget.stop(outcome)
                diagnostics = [row for row in host.store.records("mission_host_diagnostic")
                               if row["body"].get("run_id") == run_id
                               and row["body"].get("category") == "local_context_preparation"]
                if diagnostics:
                    outcome = "preparation_failure"
                    budget.stop(outcome)
                time.sleep(.05)
        except BudgetStop as exc:
            outcome = exc.reason
        except Exception as exc:
            outcome = "controller_error_" + type(exc).__name__
            try:
                budget.stop(outcome)
            except BudgetStop:
                pass
        finally:
            if host is not None:
                # Block new model work, but allow already returned responses and
                # local acceptance to settle before closing this private host.
                host._schedule_model = lambda *_args, **_kwargs: False
                if outcome != "completed" and not budget.data.get("stop_reason"):
                    try:
                        budget.stop(outcome)
                    except BudgetStop:
                        pass
                until = time.time() + 3
                while time.time() < until:
                    if not any(row["state"] == "attempted" for key in ("model_attempts", "retrieval_attempts")
                               for row in budget.data[key]):
                        try:
                            host._drive_run(host._run(run_id))
                        except Exception:
                            pass
                        break
                    time.sleep(.05)
                # One stop reading feeds both the durable evaluation record and
                # the local observation. Shutdown time is measured separately.
                controller_stop_elapsed = max(0.0, time.time() - started)
                try:
                    if host.store.get("mission_run", run_id) is not None:
                        with budget._mutex:
                            ledger_snapshot = json.loads(json.dumps(budget.data))
                        record_evaluation_observation(
                            host.store, run_id, ledger_snapshot, outcome,
                            controller_stop_elapsed,
                        )
                        evaluation_observation_saved = True
                except Exception:
                    # A trace diagnostic cannot prevent host shutdown or alter
                    # the research mission's accepted status.
                    pass
                host.close()
            if controller_stop_elapsed is None:
                controller_stop_elapsed = max(0.0, time.time() - started)
            closed = True
            # Closing the async loop does not prove its synchronous callbacks
            # stopped. Keep the deny guard installed if any worker remains.
            workers = [thread for thread in threading.enumerate() if thread.ident not in starting_threads]
            join_until = time.time() + 5
            for worker in workers:
                worker.join(timeout=max(0, join_until - time.time()))
            shutdown_pending = any(worker.is_alive() for worker in workers)
            if not shutdown_pending:
                urllib.request.AbstractHTTPHandler.do_open = original_http
            policy = json.loads((runtime / PROFILE).read_text())
            for name in ("primary", "backup"):
                policy["profiles"][name].update(enabled=False, allowed_run_ids=[])
            _atomic_json(runtime / PROFILE, policy)
            original = (root / PROFILE).read_bytes() if (root / PROFILE).exists() else None
            unchanged = (hashlib.sha256(original).hexdigest() if original else None) == manifest["original_profile_sha256"]
            total_elapsed = max(0.0, time.time() - started)
            result = {"run_id": run_id, "outcome": outcome, "elapsed_seconds": total_elapsed,
                      "controller_elapsed_seconds": controller_stop_elapsed,
                      "post_controller_finalization_seconds": total_elapsed - controller_stop_elapsed,
                      "evaluation_observation_saved": evaluation_observation_saved,
                      "model_attempts": len(budget.data["model_attempts"]),
                      "retrieval_attempts": len(budget.data["retrieval_attempts"]),
                      "settled_or_reserved_tokens": budget.data["planned_tokens"],
                      "peak_planned_tokens": budget.data["peak_planned_tokens"],
                      "stop_reason": budget.data.get("stop_reason"),
                      "original_profile_unchanged": unchanged, "private_profile_disabled": True,
                      "parent_state_unchanged": (
                          _state_digest(followup_state) == parent_digest if followup else None
                      ),
                      "parent_lineage": selected["parent_lineage"],
                      "shutdown_pending": shutdown_pending,
                      "tracing": host.tracing_status() if host else {"enabled": False},
                      "loaded_code_hashes": {str(Path(module.__file__).resolve()): hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                                             for name, module in list(sys.modules.items()) if name.startswith("ora.")
                                             and getattr(module, "__file__", None) and Path(module.__file__).is_file()},
                      "mission_status": host._run(run_id)["body"]["status"] if host and host.store.get("mission_run", run_id) else None}
            _atomic_json(folder / "observation.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/search-validation/doe-cool-roof-20261015")
    parser.add_argument("--followup-state", type=Path)
    parser.add_argument("--followup-request", type=Path,
                        help="Private JSON with exact parent snapshot, target and new question")
    parser.add_argument("--start-request", type=Path,
                        help="Private JSON with a unique request ID, natural question, scope, peers and selected profile")
    parser.add_argument("--no-aggregate-limits", action="store_true",
                        help="Explicitly authorize accounting without model, token or retrieval totals")
    parser.add_argument("--until-complete", action="store_true",
                        help="Observe until mission completion or a classified stop, without an observation deadline")
    parser.add_argument("--model-limit", type=int, default=8)
    parser.add_argument("--token-limit", type=int, default=55_000)
    parser.add_argument("--retrieval-limit", type=int, default=4)
    parser.add_argument("--deadline-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    if bool(args.followup_state) != bool(args.followup_request):
        parser.error("--followup-state and --followup-request must be used together")
    if args.start_request and args.followup_request:
        parser.error("--start-request and --followup-request are mutually exclusive")
    if args.no_aggregate_limits != args.until_complete:
        parser.error("--no-aggregate-limits and --until-complete must be used together")
    if args.no_aggregate_limits:
        if any(word == flag or word.startswith(flag + "=")
               for word in (argv if argv is not None else sys.argv[1:])
               for flag in ("--model-limit", "--token-limit", "--retrieval-limit", "--deadline-seconds")):
            parser.error("Do not mix aggregate limits or a deadline with accounting-only observation")
        args.model_limit = args.token_limit = args.retrieval_limit = args.deadline_seconds = None
    followup_request = json.loads(args.followup_request.read_text()) if args.followup_request else None
    if isinstance(followup_request, dict) and "followup_request" in followup_request:
        followup_request = followup_request["followup_request"]
    start_request = json.loads(args.start_request.read_text()) if args.start_request else None
    if isinstance(start_request, dict) and "start_request" in start_request:
        start_request = start_request["start_request"]
    if args.no_aggregate_limits and not (start_request or followup_request):
        parser.error("Accounting-only observation needs an explicit start or follow-up request")
    if not args.live:
        preview = {"question": (followup_request or start_request or {"question": QUESTION})["question"],
                   "live": False, "followup": bool(followup_request),
                   "model_calls": args.model_limit, "planned_tokens": args.token_limit,
                   "retrieval_attempts": args.retrieval_limit}
        if start_request or args.no_aggregate_limits:
            preview.update(start_request=bool(start_request),
                           credential_profile=(followup_request or start_request or {}).get(
                               "credential_profile", "primary"),
                           accounting_only=args.no_aggregate_limits,
                           until_complete=args.until_complete)
        if (followup_request or start_request or {}).get("allow_vision") is True:
            preview["allow_vision"] = True
        print(json.dumps(preview, indent=2))
        return 0
    result = run_case(ROOT, args.output, model_limit=args.model_limit,
                      token_limit=args.token_limit, retrieval_limit=args.retrieval_limit,
                      deadline_seconds=args.deadline_seconds,
                      accounting_only=args.no_aggregate_limits, until_complete=args.until_complete,
                      start_request=start_request,
                      followup_state=args.followup_state, followup_request=followup_request)
    print(json.dumps({key: value for key, value in result.items() if key != "loaded_code_hashes"}, indent=2))
    return 0 if result["outcome"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
