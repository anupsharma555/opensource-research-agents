"""Give callers one import point for the synthetic research benchmark.

It exposes ways to load a question and its sources, hand a reduced packet to a
tested worker, and check saved run records. Answer references are loaded separately
for evaluators, and these checks do not grade scientific quality.
"""

from .catalog import (
    BenchmarkError,
    build_manifest,
    canonical_sha256,
    export_worker_packet,
    load_event_script,
    load_reference,
    load_scenario,
    make_worker_packet,
    validate_catalog,
    validate_worker_packet,
)
from .results import result_applicability, validate_run_record

__all__ = [
    "BenchmarkError",
    "build_manifest",
    "canonical_sha256",
    "export_worker_packet",
    "load_event_script",
    "load_reference",
    "load_scenario",
    "make_worker_packet",
    "result_applicability",
    "validate_catalog",
    "validate_worker_packet",
    "validate_run_record",
]
