"""Provide the small skills that research agents can use with preserved evidence.

Callers can load versioned skill instructions and prepare candidate claim links or
study appraisals through this package. The helpers do not fetch sources or certify
that an agent's scientific judgment is correct.
"""

from .appraisal import prepare_result_appraisal
from .evidence_links import prepare_claim_link
from .registry import (
    RegistryError,
    SkillAttempt,
    SkillUnavailable,
    load_registry,
)

__all__ = [
    "RegistryError",
    "SkillAttempt",
    "SkillUnavailable",
    "load_registry",
    "prepare_claim_link",
    "prepare_result_appraisal",
]
