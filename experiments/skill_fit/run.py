"""Prepare synthetic skill-fit cases offline; make bounded model calls only with --live.

The runner compares two instruction variants per skill and keeps reference answers
out of model-visible input. A completed request still needs independent scoring.
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
OUT = ROOT / "experiments" / "skill_fit"
MODEL = "openai/gpt-oss-120b"
URL = "https://api.groq.com/openai/v1/chat/completions"
CAP = 4096
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
        "Use the preserved synthetic sources to link a bounded answer to the adult "
        "Condition Y question. State A1's six-week counts and interval, address A2, "
        "and distinguish evidence support from uncertainty. For each factual claim give "
        "source_id, source_version and exact span_id. Identify any requested detail "
        "the packet cannot support. Do not use tools or outside knowledge."
    ),
    "study-appraisal": (
        "Independently appraise only A1's six-week response comparison from its "
        "synthetic published abstract. Name a design-appropriate appraisal method "
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
        {"role": "user", "content": TASKS[skill_id] + "\n\nSynthetic source packet:\n" + json.dumps(packet, ensure_ascii=False, sort_keys=True)},
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


def run_one(case_id: str) -> dict[str, object]:
    if case_id not in CASES:
        raise ValueError("Unknown blind case")
    if (OUT / f"{case_id}.attempt.json").exists() or (OUT / f"{case_id}.json").exists():
        raise RuntimeError("This case has already been attempted; no automatic repeat")
    p = plan()
    request = request_for(case_id)
    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    key = _key()
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / f"{case_id}.attempt.json").open("x", encoding="utf-8") as handle:
        json.dump({"blind_id": case_id, "request_sha256": digest(request), "status": "attempt_reserved"}, handle)
        handle.write("\n")
    http = urllib.request.Request(
        URL,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
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
        "reasoning_returned": bool(message.get("reasoning")),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{case_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {k: v for k, v in result.items() if k != "final_content"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Make exactly one live call")
    parser.add_argument("--case", choices=CASES)
    args = parser.parse_args()
    if args.live:
        if not args.case:
            parser.error("--live requires --case")
        print(json.dumps(run_one(args.case), indent=2))
    else:
        print(json.dumps(plan(), indent=2))


if __name__ == "__main__":
    main()
