"""Read effective deferred tool names from full or compact model notices."""

from __future__ import annotations

import json


def catalog_tool_names(catalog: dict) -> set[str]:
    """Read exact IDs from either pinned role-catalog format."""
    if catalog.get("schema") == "ora.role_capability_catalog.ids.v1":
        names = catalog["tool_ids"]
        assert names == sorted(set(names))
        return set(names)
    assert catalog.get("schema") in {None, "ora.role_capability_catalog.purposes.v2"}
    assert set(catalog) == ({"skills", "tools"} if "schema" not in catalog
                            else {"schema", "skills", "tools"})
    assert all(isinstance(purpose, str) and purpose.strip()
               for purpose in catalog["tools"].values())
    return set(catalog["tools"])


def request_first_names(packet: dict, notice: dict) -> set[str]:
    """Assert the serialized catalog, attached schemas and notice agree."""
    attached = {item["function"]["name"] for item in packet.get("tools", [])
                if item.get("type") == "function"}
    assert set(notice["call_now_local_functions"]) == attached
    if "request_first_local_functions" in notice:
        return set(notice["request_first_local_functions"])
    marker = "Role capability catalog: "
    instructions = packet["messages"][0]["content"]
    assert instructions.count(marker) == 1
    catalog, _ = json.JSONDecoder().raw_decode(instructions.split(marker, 1)[1])
    names = catalog_tool_names(catalog)
    assert "request_first_rule" in notice
    assert "request_first_native_tools" in notice["request_first_rule"]
    return (
        names
        - attached
        - set(notice["not_ready_local_functions"])
        - set(notice["request_first_native_tools"])
        - set(notice.get("unavailable_now", []))
        - {"browser_search"}
    )


def saved_discovery_result(host, control: dict, first: dict) -> dict:
    """Reconstruct one bounded model-visible discovery page for test assertions."""
    assert first["schema"] == "ora.saved_tool_result_window.v1"
    chunks = [first["text"]]
    request = first["next_request"]
    while request is not None:
        assert request["tool"] == "read_tool_result"
        page = host.context.read_tool_result(
            mission_id=control["body"]["mission_id"],
            actor_id=control["body"]["actor_id"],
            task_id=control["body"]["task_id"],
            **{key: request[key] for key in (
                "result_id", "offset", "limit", "projection_sha256",
            )},
        )
        chunks.append(page["text"])
        request = page["next_request"]
    return json.loads("".join(chunks))
