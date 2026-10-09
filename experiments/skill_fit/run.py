"""Prepare four matched questions for comparing two agent-skill instruction styles.

The default run prints an offline plan with exact prompt hashes and a cost bound.
An explicit ``--live --case`` invocation makes one selected-model request or a
peer-feedback revision and saves private evidence. A separate reviewer judges
source support and usefulness; this helper is not the production agent loop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ora.benchmarks import make_worker_packet
from ora.skills import load_registry

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".local" / "skill-fit"
MODEL = "openai/gpt-oss-120b"
URL = "https://api.groq.com/openai/v1/chat/completions"
USER_AGENT = "OpenSourceResearchAgents/0.1 skill-fit"
CAP = 8192
MESSAGE_OVERHEAD_TOKENS = 1024
INPUT_USD_PER_M = 0.15
OUTPUT_USD_PER_M = 0.60
CASES = {
    "X1": ("evidence-linking", "concise"),
    "X2": ("evidence-linking", "rich"),
    "X3": ("study-appraisal", "concise"),
    "X4": ("study-appraisal", "rich"),
}
TASKS = {
    "evidence-linking": (
        "Use the preserved sources to link a bounded answer to the adult "
        "depression question. State A1's six-week counts and interval, address A2, "
        "and distinguish evidence support from uncertainty. For each factual claim give "
        "source_id, source_version and exact span_id. Identify any requested detail "
        "the packet cannot support. Do not use tools or outside knowledge."
    ),
    "study-appraisal": (
        "Independently appraise only A1's six-week response comparison from its "
        "study abstract (simulated data). Name a design-appropriate appraisal method "
        "and version if known. Separate reporting completeness, risk of bias, "
        "applicability and precision. Cite exact A1 span_ids for reported facts; "
        "say which methods cannot be determined from this abstract. Do not use "
        "A2-A4, tools, or outside study facts."
    ),
}


def digest(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def request_for(case_id: str) -> dict[str, object]:
    skill_id, variant = CASES[case_id]
    registry = load_registry(ROOT, {"source_read_by_id", "study_appraisal"})
    packet = make_worker_packet(ROOT / "benchmarks" / "v1", "syn-abstract-disagreement", "single_role", "controlled_equal_input")
    if skill_id == "study-appraisal":
        packet["sources"] = [s for s in packet["sources"] if s["source_id"] == "A1"]
        packet.pop("worker_packet_sha256")  # The task view is a subset, not a benchmark export.
    messages = [
        {"role": "system", "content": registry.render_instructions(skill_id, variant)},
        {"role": "user", "content": TASKS[skill_id] + "\n\nSource packet (simulated data):\n" + json.dumps(packet, ensure_ascii=False, sort_keys=True)},
    ]
    return {
        "model": MODEL,
        "messages": messages,
        "max_completion_tokens": CAP,
        "reasoning_effort": "high",
        "include_reasoning": False,
        "stream": False,
    }


def upper_cost_usd(requests: list[dict[str, object]]) -> float:
    # UTF-8 byte count is a deliberately loose upper bound on input tokens.
    input_bytes = sum(len(json.dumps(r["messages"], ensure_ascii=False).encode("utf-8")) for r in requests)
    input_upper = input_bytes + len(requests) * MESSAGE_OVERHEAD_TOKENS
    return input_upper * INPUT_USD_PER_M / 1_000_000 + len(requests) * CAP * OUTPUT_USD_PER_M / 1_000_000


def revision_request(case_id: str, prior: dict[str, object], feedback: str) -> dict[str, object]:
    """Continue one completed case with source-checkable peer feedback and the same model."""
    request = request_for(case_id)
    if (prior.get("schema") != "ora.skill_fit.result.v1"
            or prior.get("phase", "initial") != "initial"
            or prior.get("blind_id") != case_id or prior.get("model_requested") != MODEL
            or prior.get("model_returned") != MODEL
            or prior.get("request_sha256") != digest(request)):
        raise ValueError("Prior answer does not match this case, model and request")
    content = prior.get("final_content")
    if prior.get("finish_reason") != "stop" or not isinstance(content, str) or not content.strip():
        raise ValueError("Peer revision requires a completed, nonempty prior answer")
    if "final_content_sha256" in prior and prior["final_content_sha256"] != digest(content):
        raise ValueError("Prior answer does not match its saved content hash")
    if not feedback.strip():
        raise ValueError("Peer feedback is empty")
    request["messages"].extend([
        {"role": "assistant", "content": content},
        {"role": "user", "content": (
            "Check this peer feedback against the supplied sources, then revise your answer. "
            "Preserve supported findings and correct only affected claims. Distinguish any "
            "new method guidance from the study evidence.\n\n" + feedback
        )},
    ])
    return request


def plan() -> dict[str, object]:
    requests = [request_for(case_id) for case_id in CASES]
    estimate = upper_cost_usd(requests)
    if estimate >= 1.0:
        raise RuntimeError("Four-call cost upper bound exceeds $1")
    return {
        "schema": "ora.skill_fit.plan.v1",
        "synthetic": True,
        "model": MODEL,
        "endpoint": URL,
        "reasoning_effort": "high",
        "include_reasoning": False,
        "completion_cap_each": CAP,
        "initial_call_count": len(requests),
        "pricing_usd_per_million": {"input": INPUT_USD_PER_M, "output": OUTPUT_USD_PER_M},
        "input_estimate_method": "UTF-8 message bytes plus 1024 wrapper tokens per call as conservative token upper bound",
        "worst_case_usd": round(estimate, 6),
        "cases": [
            {"blind_id": case_id, "request_sha256": digest(request), "prompt_sha256": digest(request["messages"]),
             "prompt_utf8_bytes": len(json.dumps(request["messages"], ensure_ascii=False).encode("utf-8"))}
            for case_id, request in zip(CASES, requests)
        ],
    }


def _key() -> str:
    value = os.environ.get("GROQ_API_KEY", "")
    if value:
        return value
    env_path = ROOT / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("GROQ_API_KEY="):
            return line.partition("=")[2].strip().strip('"').strip("'")
    raise RuntimeError("GROQ_API_KEY is unavailable")


def run_one(case_id: str, *, prior: dict[str, object] | None = None,
            feedback: str | None = None, output_dir: Path | None = None) -> dict[str, object]:
    if case_id not in CASES:
        raise ValueError("Unknown blind case")
    folder = (output_dir or OUT).resolve()
    # Research responses stay local even when an explicit destination is supplied.
    if folder.is_relative_to(ROOT) and not folder.is_relative_to(ROOT / ".local"):
        raise ValueError("Save raw results below .local or outside the repository")
    if (folder / f"{case_id}.attempt.json").exists() or (folder / f"{case_id}.json").exists():
        raise RuntimeError("This case has already been attempted; no automatic repeat")
    p = plan()
    if (prior is None) != (feedback is None):
        raise ValueError("Peer revision needs both the prior result and feedback")
    request = request_for(case_id) if prior is None else revision_request(case_id, prior, feedback)
    if upper_cost_usd([request]) >= 1.0:
        raise RuntimeError("One-call estimate exceeds $1")
    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    key = _key()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{case_id}.attempt.json").open("x", encoding="utf-8") as handle:
        json.dump({"blind_id": case_id, "request_sha256": digest(request), "status": "attempt_reserved"}, handle)
        handle.write("\n")
    http = urllib.request.Request(
        URL,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": USER_AGENT},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(http, timeout=120) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Groq HTTP {exc.code}; stopped without retry") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Groq transport failure ({type(exc).__name__}); stopped without retry") from None
    elapsed = round(time.monotonic() - started, 3)
    choice = payload["choices"][0]
    message = choice["message"]
    usage = payload.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    result = {
        "schema": "ora.skill_fit.result.v1",
        "blind_id": case_id,
        "phase": "peer_revision" if prior is not None else "initial",
        "parent_result_sha256": digest(prior) if prior is not None else None,
        "parent_content_hash_status": ("not_applicable" if prior is None else
                                       "matched" if "final_content_sha256" in prior else "not_recorded"),
        "feedback_sha256": digest(feedback) if feedback is not None else None,
        "synthetic": True,
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "model_requested": MODEL,
        "model_returned": payload.get("model"),
        "request_sha256": digest(request),
        "prompt_sha256": digest(request["messages"]),
        "plan_sha256": digest(p),
        "elapsed_seconds": elapsed,
        "finish_reason": choice.get("finish_reason"),
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": usage.get("total_tokens")},
        "estimated_usd_at_documented_rates": round(
            ((prompt_tokens or 0) * INPUT_USD_PER_M + (completion_tokens or 0) * OUTPUT_USD_PER_M) / 1_000_000, 8
        ),
        "final_content": message.get("content"),
        "final_content_sha256": digest(message.get("content")),
        "reasoning_returned": bool(message.get("reasoning")),
    }
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{case_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {k: v for k, v in result.items() if k != "final_content"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Make exactly one live call")
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--prior-result", type=Path)
    parser.add_argument("--feedback-file", type=Path)
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    if bool(args.prior_result) != bool(args.feedback_file):
        parser.error("Peer revision needs --prior-result and --feedback-file")
    prior = json.loads(args.prior_result.read_text()) if args.prior_result else None
    feedback = args.feedback_file.read_text() if args.feedback_file else None
    if args.live:
        if not args.case:
            parser.error("--live requires --case")
        print(json.dumps(run_one(args.case, prior=prior, feedback=feedback, output_dir=args.output_dir), indent=2))
    elif prior is not None:
        if not args.case:
            parser.error("Peer revision preview requires --case")
        request = revision_request(args.case, prior, feedback)
        print(json.dumps({"case": args.case, "request_sha256": digest(request),
                          "estimated_max_usd": upper_cost_usd([request]), "provider_called": False}))
    else:
        print(json.dumps(plan(), indent=2))


if __name__ == "__main__":
    main()
