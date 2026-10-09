"""Keep nested model options intact across the shared command data format."""

import pytest
from pydantic import ValidationError

from ora.contracts.models import ModelSelection


def test_model_settings_round_trip_nested_json_without_changing_scalar_callers():
    settings = {"temperature": 0.25, "stop": ["END"], "response_format": {"type": "json_object"}, "optional": None}
    digest = ModelSelection.calculate_hash("fixture", "selected-model", "high", settings)
    selection = ModelSelection(provider="fixture", model="selected-model", reasoning_effort="high", settings=settings, configuration_hash=digest)
    restored = ModelSelection.model_validate_json(selection.model_dump_json(by_alias=True))
    assert restored.settings == settings
    assert restored.configuration_hash == digest
    with pytest.raises(ValidationError):
        ModelSelection(provider="fixture", model="selected-model", reasoning_effort="high", settings={"nested": [float("nan")]}, configuration_hash=digest)
