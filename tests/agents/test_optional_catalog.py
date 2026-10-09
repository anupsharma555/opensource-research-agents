"""Keep shared tools usable when an optional descriptor cannot be registered."""

import pytest

from ora.agents import COMMON_TOOL_IDS, Capability, CapabilityCatalog, CapabilityError
from ora.agents.capabilities import capability_view


def test_unsupported_optional_schema_is_visible_without_blocking_common_tools():
    optional = Capability("optional_uri", "1", {
        "type": "object", "properties": {"url": {"type": "string", "format": "uri"}},
        "additionalProperties": False,
    }, "optional:uri", optional=True)
    catalog = CapabilityCatalog.with_common([optional])
    grants = {f"common:{name}" for name in COMMON_TOOL_IDS} | {"optional:uri"}
    permitted = catalog.permitted(grants)
    assert COMMON_TOOL_IDS.issubset(permitted)
    assert "optional_uri" not in permitted
    view = capability_view({"id": "call", "version": 1, "body": {
        "permitted_tools": permitted, "grants": sorted(grants), "exposed_tools": sorted(COMMON_TOOL_IDS),
    }}, catalog)
    assert any(item["tool_id"] == "optional_uri" and item["reason"] for item in view["unavailable_optional"])
    assert len(view["available"]) == len(COMMON_TOOL_IDS)
    with pytest.raises(CapabilityError):
        CapabilityCatalog.with_common([Capability("required_uri", "1", optional.schema, "required:uri")])
