import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ora.contracts.generate import MODELS, generate

ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / "contracts/examples.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_python_boundary(case):
    if case["accepted"]:
        MODELS[case["schema"]].model_validate(case["value"], strict=True)
    else:
        with pytest.raises(ValidationError):
            MODELS[case["schema"]].model_validate(case["value"], strict=True)


def test_generated_artifacts_are_current():
    assert generate(ROOT, check=True), "Run scripts/dev generate after changing canonical models."
