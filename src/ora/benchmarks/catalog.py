"""Load fixed synthetic research questions with exact source passages.

This file checks packet versions, content hashes, quoted spans, and future events,
then builds a worker input that excludes the answer reference. A tested agent must
receive only that exported input; this code does not secure the repository files.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA_MANIFEST = "ora.benchmark.manifest.v1"
SCHEMA_SCENARIO = "ora.benchmark.scenario.v1"
SCHEMA_REFERENCE = "ora.benchmark.reference.v1"
SCHEMA_WORKER = "ora.benchmark.worker.v1"
SCHEMA_PROFILES = "ora.benchmark.profiles.v1"
SCHEMA_BASELINES = "ora.benchmark.baselines.v1"
SCHEMA_RUBRIC = "ora.benchmark.rubric.v1"
PACKET_SET_VERSION = "1.0.4"
DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "benchmarks" / "v1"
PROFILE_IDS = frozenset(
    {"provider_only", "retrieval_only", "single_role", "peer_exchange", "complete_workflow"}
)
CONDITION_IDS = frozenset({"controlled_equal_input", "native_workflow"})
ARM_IDS = frozenset({"ora_pipeline", "same_model_single_agent", "frontier_chatgpt_or_codex"})


class BenchmarkError(ValueError):
    """A packet is malformed, changed without a new manifest, or unsafe to export."""


def canonical_sha256(value: Any) -> str:
    """Hash JSON content independently of indentation or object-key order."""
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"Cannot read JSON packet {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"{path.name} must contain a JSON object")
    return value


def _inside(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise BenchmarkError("Manifest path must be a nonempty relative string")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise BenchmarkError(f"Manifest path escapes packet root: {relative}")
    return path


def _require(value: dict[str, Any], keys: set[str], where: str) -> None:
    missing = keys - value.keys()
    if missing:
        raise BenchmarkError(f"{where} is missing {', '.join(sorted(missing))}")


def _manifest(root: Path) -> dict[str, Any]:
    manifest = _read_json(root / "manifest.json")
    if manifest.get("schema") != SCHEMA_MANIFEST:
        raise BenchmarkError("Unsupported benchmark manifest schema")
    if manifest.get("packet_set_version") != PACKET_SET_VERSION:
        raise BenchmarkError("Unsupported benchmark packet-set version")
    _require(manifest, {"scenarios", "profiles_sha256", "baselines_sha256", "rubric_sha256"}, "manifest")
    if not isinstance(manifest["scenarios"], list):
        raise BenchmarkError("manifest.scenarios must be a list")
    return manifest


def _entry(root: Path, scenario_id: str) -> dict[str, Any]:
    entries = [x for x in _manifest(root)["scenarios"] if x.get("scenario_id") == scenario_id]
    if len(entries) != 1:
        raise BenchmarkError(f"Expected one manifest entry for {scenario_id}")
    return entries[0]


def _checked_file(root: Path, relative: str, digest: str) -> dict[str, Any]:
    value = _read_json(_inside(root, relative))
    if canonical_sha256(value) != digest:
        raise BenchmarkError(f"Frozen packet changed without a new hash/version: {relative}")
    return value


def _check_scenario(scenario: dict[str, Any]) -> None:
    if scenario.get("schema") != SCHEMA_SCENARIO or scenario.get("synthetic") is not True:
        raise BenchmarkError("Scenario must be marked as a synthetic v1 scenario")
    _require(
        scenario,
        {"scenario_id", "scenario_version", "family", "review_type", "task", "sources", "initial_source_ids", "scripted_events"},
        "scenario",
    )
    task = scenario["task"]
    if not isinstance(task, dict):
        raise BenchmarkError("scenario.task must be an object")
    _require(task, {"question", "decision", "audience", "latency_class"}, "task")
    if task["latency_class"] not in {"minimal", "medium", "long", "very_long"}:
        raise BenchmarkError("Invalid descriptive latency class")
    if not all(isinstance(task[k], str) and task[k] for k in ("question", "decision", "audience")):
        raise BenchmarkError("Task question, decision, and audience must be nonempty")
    sources = scenario["sources"]
    if not isinstance(sources, list) or not sources:
        raise BenchmarkError("Scenario needs source snapshots")
    source_ids: set[str] = set()
    span_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise BenchmarkError("Each source must be an object")
        _require(source, {"source_id", "source_version", "surface", "access_state", "rights", "body", "body_sha256", "spans", "synthetic"}, "source")
        sid = source["source_id"]
        if not isinstance(sid, str) or not sid or sid in source_ids:
            raise BenchmarkError(f"Duplicate or invalid source ID: {sid}")
        source_ids.add(sid)
        if source["synthetic"] is not True or source["rights"] != "synthetic_project_owned" or not isinstance(source["body"], str):
            raise BenchmarkError(f"Source {sid} must contain synthetic text")
        if hashlib.sha256(source["body"].encode("utf-8")).hexdigest() != source["body_sha256"]:
            raise BenchmarkError(f"Source text changed without updating its hash: {sid}")
        if not isinstance(source["spans"], list):
            raise BenchmarkError(f"Source spans must be a list: {sid}")
        for span in source["spans"]:
            if not isinstance(span, dict):
                raise BenchmarkError(f"Source span must be an object: {sid}")
            _require(span, {"span_id", "start", "end", "quote", "locator"}, "span")
            span_id = span["span_id"]
            if not isinstance(span_id, str) or not span_id or span_id in span_ids:
                raise BenchmarkError(f"Duplicate or invalid span ID: {span_id}")
            span_ids.add(span_id)
            start, end = span["start"], span["end"]
            if not isinstance(start, int) or not isinstance(end, int) or not (0 <= start < end <= len(source["body"])):
                raise BenchmarkError(f"Invalid text offsets for {span_id}")
            if source["body"][start:end] != span["quote"] or not span["locator"]:
                raise BenchmarkError(f"Passage locator does not match exact source text: {span_id}")
    initial = scenario["initial_source_ids"]
    if not isinstance(initial, list) or len(initial) != len(set(initial)) or not set(initial) <= source_ids:
        raise BenchmarkError("Initial source IDs must be unique known sources")
    events = scenario["scripted_events"]
    if not isinstance(events, list):
        raise BenchmarkError("Scripted events must be a list")
    event_ids: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            raise BenchmarkError("Scripted event must be an object")
        _require(event, {"event_id", "step", "kind", "actor", "target", "payload"}, "scripted event")
        eid = event["event_id"]
        if not isinstance(eid, str) or not eid or eid in event_ids:
            raise BenchmarkError(f"Duplicate or invalid event ID: {eid}")
        event_ids.add(eid)
        if not isinstance(event["step"], int) or event["step"] < 0 or not isinstance(event["payload"], dict):
            raise BenchmarkError(f"Invalid scripted event: {eid}")
        for key in ("source_ids", "retrieved_source_ids"):
            if key in event["payload"] and not set(event["payload"][key]) <= source_ids:
                raise BenchmarkError(f"Event {eid} names an unknown source")
        if "source_id" in event["payload"] and event["payload"]["source_id"] not in source_ids:
            raise BenchmarkError(f"Event {eid} names an unknown source")


def _check_reference(reference: dict[str, Any], scenario: dict[str, Any]) -> None:
    if reference.get("schema") != SCHEMA_REFERENCE or reference.get("synthetic") is not True:
        raise BenchmarkError("Reference must be marked as a synthetic v1 reference")
    if (reference.get("scenario_id"), reference.get("scenario_version")) != (
        scenario["scenario_id"], scenario["scenario_version"]
    ):
        raise BenchmarkError("Reference identity/version does not match scenario")
    _require(reference, {"withheld_marker", "acceptable_claims", "contrary_span_ids", "severe_error_examples", "irrelevant_span_ids", "expected_partial_outcome", "unresolved_reference_gaps"}, "reference")
    if reference["withheld_marker"] in json.dumps(scenario):
        raise BenchmarkError("Withheld marker leaked into public scenario")
    span_ids = {span["span_id"] for source in scenario["sources"] for span in source["spans"]}
    claim_ids: set[str] = set()
    for claim in reference["acceptable_claims"]:
        _require(claim, {"claim_id", "text", "support_span_ids", "qualification"}, "reference claim")
        if claim["claim_id"] in claim_ids or not set(claim["support_span_ids"]) <= span_ids:
            raise BenchmarkError("Duplicate claim or unknown supporting span in reference")
        claim_ids.add(claim["claim_id"])
    for key in ("contrary_span_ids", "irrelevant_span_ids"):
        if not set(reference[key]) <= span_ids:
            raise BenchmarkError(f"Unknown source span in {key}")


def load_scenario(root: Path | str, scenario_id: str) -> dict[str, Any]:
    """Load a public harness scenario, including future events but no answer key."""
    root = Path(root)
    entry = _entry(root, scenario_id)
    scenario = _checked_file(root, entry["scenario_path"], entry["scenario_sha256"])
    _check_scenario(scenario)
    if (scenario["scenario_id"], scenario["scenario_version"]) != (scenario_id, entry["scenario_version"]):
        raise BenchmarkError("Manifest and scenario identity/version disagree")
    return scenario


def load_reference(root: Path | str, scenario_id: str) -> dict[str, Any]:
    """Evaluator-only loader. Never call from a tested agent process."""
    root = Path(root)
    entry = _entry(root, scenario_id)
    reference = _checked_file(root, entry["reference_path"], entry["reference_sha256"])
    _check_reference(reference, load_scenario(root, scenario_id))
    return reference


def load_event_script(root: Path | str, scenario_id: str) -> list[dict[str, Any]]:
    """Public to the fixture driver; deliver one event at a time to tested agents."""
    return copy.deepcopy(load_scenario(root, scenario_id)["scripted_events"])


def validate_catalog(root: Path | str = DEFAULT_ROOT) -> dict[str, Any]:
    """Check every pinned file, source span, event, and withheld reference link."""
    root = Path(root)
    manifest = _manifest(root)
    for name, schema in (("profiles", SCHEMA_PROFILES), ("baselines", SCHEMA_BASELINES), ("rubric", SCHEMA_RUBRIC)):
        value = _checked_file(root, f"{name}.json", manifest[f"{name}_sha256"])
        if value.get("schema") != schema:
            raise BenchmarkError(f"Unsupported {name} schema")
    profiles = _read_json(root / "profiles.json")
    baselines = _read_json(root / "baselines.json")
    if {x["profile_id"] for x in profiles["profiles"]} != PROFILE_IDS:
        raise BenchmarkError("Profile IDs do not match the v1 contract")
    if {x["condition_id"] for x in baselines["conditions"]} != CONDITION_IDS:
        raise BenchmarkError("Baseline condition IDs do not match the v1 contract")
    if {x["arm_id"] for x in baselines["comparison_arms"]} != ARM_IDS:
        raise BenchmarkError("Comparison arms do not match the v1 contract")
    ids = [x.get("scenario_id") for x in manifest["scenarios"]]
    if len(ids) != len(set(ids)) or not ids:
        raise BenchmarkError("Manifest needs unique scenarios")
    for scenario_id in ids:
        scenario = load_scenario(root, scenario_id)
        reference = load_reference(root, scenario_id)
        if reference["withheld_marker"] in json.dumps(make_worker_packet(root, scenario_id, "single_role", "controlled_equal_input")):
            raise BenchmarkError("Reference marker leaked into worker packet")
        if not scenario["sources"]:
            raise BenchmarkError("Empty scenario source opportunity")
    return {"packet_set_version": manifest["packet_set_version"], "scenario_count": len(ids), "profile_count": len(PROFILE_IDS), "condition_count": len(CONDITION_IDS)}


def make_worker_packet(root: Path | str, scenario_id: str, profile_id: str, condition_id: str) -> dict[str, Any]:
    """Return only task and initially available sources, never future events or references."""
    if profile_id not in PROFILE_IDS or condition_id not in CONDITION_IDS:
        raise BenchmarkError("Unknown profile or comparison condition")
    root = Path(root)
    scenario = load_scenario(root, scenario_id)
    entry = _entry(root, scenario_id)
    initial = set(scenario["initial_source_ids"])
    sources = (
        [copy.deepcopy(x) for x in scenario["sources"] if x["source_id"] in initial]
        if condition_id == "controlled_equal_input"
        else []
    )
    packet = {
        "schema": SCHEMA_WORKER,
        "scenario_id": scenario_id,
        "scenario_version": scenario["scenario_version"],
        "scenario_sha256": entry["scenario_sha256"],
        "profile_id": profile_id,
        "condition_id": condition_id,
        "synthetic": True,
        "task": copy.deepcopy(scenario["task"]),
        "sources": sources,
        "source_access": "fixed_initial_snapshots" if sources else "native_authorized_workflow",
    }
    packet["worker_packet_sha256"] = canonical_sha256(packet)
    return packet


def validate_worker_packet(root: Path | str, packet: dict[str, Any]) -> None:
    """Reject altered or augmented exported input, including leaked reference fields."""
    if not isinstance(packet, dict) or packet.get("schema") != SCHEMA_WORKER:
        raise BenchmarkError("Worker packet must use the v1 schema")
    expected = make_worker_packet(
        root, packet.get("scenario_id"), packet.get("profile_id"), packet.get("condition_id")
    )
    if packet != expected:
        raise BenchmarkError("Worker packet differs from the frozen, key-free export")


def export_worker_packet(root: Path | str, scenario_id: str, profile_id: str, condition_id: str, destination: Path | str) -> Path:
    """Write one worker packet without overwrite; mount only this output for an agent."""
    packet = make_worker_packet(root, scenario_id, profile_id, condition_id)
    destination = Path(destination)
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(packet, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    return destination


def build_manifest(root: Path | str = DEFAULT_ROOT) -> dict[str, Any]:
    """Produce a new manifest after an intentional packet version update.

    This does not rewrite existing manifests. Review the version change before
    saving the returned value; never refresh hashes to make a failing check pass.
    """
    root = Path(root)
    scenarios = []
    for path in sorted((root / "scenarios").glob("*.json")):
        scenario = _read_json(path)
        _check_scenario(scenario)
        reference_path = root / "withheld" / f"{scenario['scenario_id']}.reference.json"
        reference = _read_json(reference_path)
        _check_reference(reference, scenario)
        scenarios.append({
            "scenario_id": scenario["scenario_id"],
            "scenario_version": scenario["scenario_version"],
            "scenario_path": str(path.relative_to(root)),
            "scenario_sha256": canonical_sha256(scenario),
            "reference_path": str(reference_path.relative_to(root)),
            "reference_sha256": canonical_sha256(reference),
        })
    return {
        "schema": SCHEMA_MANIFEST,
        "packet_set_version": PACKET_SET_VERSION,
        "scenarios": scenarios,
        "profiles_sha256": canonical_sha256(_read_json(root / "profiles.json")),
        "baselines_sha256": canonical_sha256(_read_json(root / "baselines.json")),
        "rubric_sha256": canonical_sha256(_read_json(root / "rubric.json")),
    }
