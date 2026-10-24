"""Describe the research tools an agent can discover or request during a mission.

Define each tool's name, purpose and accepted arguments, and group tools for
ordinary research, invitations and source recovery. The mission service uses
these definitions to build the tool descriptions saved with each model request;
it separately checks permissions and executes the selected actions.
"""

from __future__ import annotations

from dataclasses import replace

from ora.agents.capabilities import COMMON_SCHEMAS, Capability, content_hash
from ora.contracts.models import (
    CancelSearchInput,
    FetchSourceAssetInput,
    FetchSourceInput,
    InspectFigureInput,
    LegacySavePlanInput,
    LegacySearchWebInput,
    ListSourceAssetsInput,
    ModifyPlanInput,
    ReadDocumentOCRInput,
    ReadPlanInput,
    ReadResearchActivityInput,
    ReadSearchResultsInput,
    RenderSourceInput,
    RetainEvidenceInput,
    SavePlanInput,
    SearchEvidenceLibraryInput,
    SearchWebInput,
)
from ora.retrieval.catalog_tools import CATALOG_TOOL_DESCRIPTIONS, CATALOG_TOOL_SCHEMAS
from ora.retrieval.discovery import available_routes

from .calculation import CALCULATION_TOOL_DESCRIPTION, CALCULATION_TOOL_SCHEMA
from .observation_tools import OBSERVATION_TOOL_DESCRIPTIONS, OBSERVATION_TOOL_SCHEMAS

CORE_RESEARCH_TOOLS = frozenset(
    {
        "discover_capabilities",
        "request_capability",
        "search_scholarly",
        "inspect_hit",
        "publish_result",
        "board_read",
        "peer_request",
        "review_peer_answer",
        "read_context_page",
        "read_context_record",
    }
)
# These defaults stay granted and requestable when their schemas cannot fit.
# Explicit peer selections and active skill tools take precedence over deferral.
DEFERABLE_CORE_TOOLS = frozenset({
    "search_scholarly", "read_context_record", "peer_request", "review_peer_answer", "board_read",
})
CORE_INVITATION_TOOLS = frozenset(
    {"discover_capabilities", "request_capability", "respond_invitation", "read_context_page"}
)
SOURCE_RECOVERY_TOOLS = frozenset({"read_source_inventory", "select_working_sources"})
UNHANDLED_COMMON_TOOLS = frozenset({"claim_work", "peer_invite", "subscribe"})
TOOL_PURPOSES = {
    "read_research_activity": "See peer research questions, searches, progress and gaps before repeating work",
    "retain_evidence": "Curate checked permitted evidence for reuse in later missions",
    "search_evidence_library": "Search retained evidence metadata or select one entry for checked reading",
    "list_source_assets": "Discover source-linked figures from exact retained HTML/XML",
    "fetch_source_asset": "Fetch one discovered public figure image by exact asset reference",
    "read_document_ocr": "Local OCR of one scanned page/image; unverified text",
    "inspect_figure": "Vision-opt-in figure question; unverified interpretation",
    "render_source": "Optional Browserless render after ordinary extraction",
    "search_web": "Exa-first web search: quick for one targeted lookup; research for multiple complementary queries. Same-task identical searches reuse saved results unless refresh=true.",
    "read_search_results": "Page saved operation results; no new search",
    "cancel_search": "Cancel pending retrieval; keep completed results",
    "browser_search": "Groq browsing for linked-page exploration; native provider tool, not a local function",
    "read_browser_search_result": "Page saved browser discovery text/links",
    "calculate": "Arithmetic on supplied values; no source validation",
    "search_saved_passages": "Saved-source search: find text in retained permitted sources; no web request",
    "read_skill_resource": "Page active skill guidance/examples/helper source",
    "activate_skill": "Load optional guidance and selected tools",
    "release_skill": "Release optional guidance and its tools",
    "release_capability": "Release one selected tool",
    "read_tool_result": "Page saved result under current permissions",
    "compact_conversation": "Archive exchanges with a continuation summary",
    "read_conversation_history": "Page archived exchanges under current permissions",
    "board_read": "List open proposals, optional work and registered peers when available",
    "claim_work": "Low-level host claim; executor owns claims",
    "discover_capabilities": "List tools, purposes and access",
    "evidence_lookup": "Find current copy's passage IDs and URL",
    "peer_invite": "Low-level invite; use invite_peer",
    "peer_request": "Ask a registered peer",
    "decline_peer_request": "Decline one directed request addressed to this peer",
    "request_capability": "Select permitted tool for next turn",
    "subscribe": "Low-level host subscription",
    "defer_work": "Defer exact optional proposal",
    "fetch_source": "Fetch and inspect saved public hit",
    "inspect_hit": "Inspect retained saved-hit text",
    "invite_peer": "Invite role to exact open work",
    "list_optional_work": "List optional tasks/versions",
    "propose_scope_completion": "Propose requested-scope completion",
    "propose_work": "Create optional research branch",
    "publish_result": "Save report and finish peer task",
    "read_context_page": "List saved refs and previews",
    "read_context_record": "Read saved record text",
    "read_note": "Read own task note by ID",
    "read_source_window": "Read exact source window/citation spans",
    "read_source_inventory": "List saved copies and access during recovery",
    "select_working_sources": "Select copies for next model turn",
    "respond_invitation": "Accept, decline or counter invitation",
    "review_peer_answer": "Review answer to own request",
    "revise_work": "Revise exact current proposal",
    "search_notes": "Find own task notes",
    "search_scholarly": "Scholarly databases or exact DOI/PMID",
    "search_bheb_corpus": "Optional private behavioral-health corpus search; score ranks leads, not evidence quality",
    "inspect_bheb_hit": "Check one exact private corpus hit for attributable, permitted source text",
    "write_note": "Save or revise own task note",
    "save_plan": "Create first optional task plan",
    "modify_plan": "Edit existing plan fields/steps",
    "read_plan": "Read exact plan revision/stale refs",
    "list_venues": "List dated venue leads",
    "propose_venue": "Save additional venue lead",
    "navigate_venue": "Open venue and list public links",
    "page_venue_navigation": "Page saved links; no new fetch",
    "inspect_navigation_document": "Inspect retained navigation HTML",
    "estimate_latency": "Record rough task duration",
    "wait_for_dependencies": "Wait for peer answers/prerequisites",
}
ROUTE_ALIASES = {
    "pubmed": "pubmed.search",
    "crossref": "crossref.search",
    "europe_pmc": "europe_pmc.search",
    "exa": "exa.search",
}
MAX_MISSION_SOURCE_WINDOW_CHARS = 16000
ACTION_PURPOSE_TOOL_IDS = frozenset({"peer_request", "request_capability"})
ACTION_PURPOSE_FIELD = {
    "type": ["string", "null"], "maxLength": 160,
    "description": "Public intended benefit; omit or use null if unavailable. Do not claim results.",
}
BOARD_READ_V1_DESCRIPTION = (
    "List open proposal refs, optional work and current registered peer IDs when available, "
    "without report or source text. No arguments; topic is unused."
)


def mission_board_read_capability(item: Capability) -> Capability:
    """Offer argument-free board reads on new mission calls; keep common v1 intact."""
    if item.tool_id != "board_read":
        return item
    if item.version != "1" or item.schema != COMMON_SCHEMAS["board_read"]:
        raise ValueError("Mission board read needs the exact common v1 tool schema")
    return replace(item, version="2", schema={
        "type": "object", "properties": {}, "required": [],
        "additionalProperties": False,
    })


ACTION_PURPOSE_FIELD_V3 = {
    "type": ["string", "null"], "maxLength": 512,
    "description": (
        "Brief public intended benefit, ideally one or two sentences; omit or use null "
        "if unavailable. Do not claim results."
    ),
}

def mission_action_purpose_capability(item: Capability) -> Capability:
    """Add a bounded optional action purpose only to new mission call manifests."""
    if item.tool_id not in ACTION_PURPOSE_TOOL_IDS:
        return item
    if item.version != "1" or item.schema != COMMON_SCHEMAS[item.tool_id]:
        raise ValueError("Mission action purpose needs the exact common v1 tool schema")
    return replace(item, version="2", schema={
        **item.schema,
        "properties": {**item.schema["properties"], "purpose": dict(ACTION_PURPOSE_FIELD)},
    })


def mission_action_purpose_v3_capability(item: Capability) -> Capability:
    """Widen only new-run common action purposes; keep the v2 contract intact."""
    if item.tool_id not in ACTION_PURPOSE_TOOL_IDS:
        return item
    expected_v2 = {
        **COMMON_SCHEMAS[item.tool_id],
        "properties": {
            **COMMON_SCHEMAS[item.tool_id]["properties"],
            "purpose": ACTION_PURPOSE_FIELD,
        },
    }
    if item.version != "2" or item.schema != expected_v2:
        # A historical no-purpose catalog can still be constructed for saved calls.
        return item
    return replace(item, version="3", schema={
        **item.schema,
        "properties": {**item.schema["properties"], "purpose": dict(ACTION_PURPOSE_FIELD_V3)},
    })


def tool_contract_matches(current: dict, saved: dict) -> bool:
    """Match pinned contracts, including narrowly declared additive legacy forms."""
    def without_descriptions(schema: dict) -> dict:
        result = {key: value for key, value in schema.items() if key != "description"}
        if isinstance(result.get("properties"), dict):
            result["properties"] = {
                name: without_descriptions(shape)
                for name, shape in result["properties"].items()
            }
        for key in ("items", "additionalProperties"):
            if isinstance(result.get(key), dict):
                result[key] = without_descriptions(result[key])
        for key in ("allOf", "anyOf", "oneOf"):
            if isinstance(result.get(key), list):
                result[key] = [without_descriptions(shape) for shape in result[key]]
        return result

    def contract(descriptor: dict) -> dict:
        if (not isinstance(descriptor, dict)
                or not isinstance(descriptor.get("description", ""), str)
                or not isinstance(descriptor.get("schema"), dict)
                or content_hash(descriptor["schema"]) != descriptor.get("schema_sha256")):
            raise ValueError("Invalid tool descriptor")
        return {
            **{key: value for key, value in descriptor.items()
               if key not in {"description", "schema", "schema_sha256"}},
            "schema": without_descriptions(descriptor["schema"]),
        }

    try:
        current_contract, saved_contract = contract(current), contract(saved)
        if current_contract == saved_contract:
            return True
        if current_contract.get("tool_id") in ACTION_PURPOSE_TOOL_IDS:
            tool_id = current_contract["tool_id"]
            version = current_contract.get("version")
            purpose_schema = without_descriptions(
                ACTION_PURPOSE_FIELD_V3 if version == "3" else ACTION_PURPOSE_FIELD
            )
            expected_current = {
                **COMMON_SCHEMAS[tool_id],
                "properties": {
                    **COMMON_SCHEMAS[tool_id]["properties"], "purpose": purpose_schema,
                },
            }
            return bool(
                version in {"2", "3"}
                and current_contract["schema"] == expected_current
                and saved_contract in (
                    [{**current_contract, "version": "1", "schema": COMMON_SCHEMAS[tool_id]}]
                    + ([{**current_contract, "version": "2", "schema": {
                        **COMMON_SCHEMAS[tool_id],
                        "properties": {
                            **COMMON_SCHEMAS[tool_id]["properties"],
                            "purpose": without_descriptions(ACTION_PURPOSE_FIELD),
                        },
                    }}] if version == "3" else [])
                )
            )
        if current_contract.get("tool_id") == "activate_skill":
            expected_current = {
                **LEGACY_ACTIVATE_SKILL_SCHEMA,
                "properties": {
                    **LEGACY_ACTIVATE_SKILL_SCHEMA["properties"],
                    "purpose": without_descriptions(ACTION_PURPOSE_FIELD),
                },
            }
            return bool(
                current_contract.get("version") == "2"
                and current_contract["schema"] == expected_current
                and saved_contract == {
                    **current_contract,
                    "version": "1",
                    "schema": LEGACY_ACTIVATE_SKILL_SCHEMA,
                }
            )
        if current_contract.get("tool_id") != "search_web":
            return False
        # Saved search_web calls before optional refresh was introduced keep
        # their exact old argument schema. The current handler defaults it to
        # false; all other descriptor fields and schema fields must still match.
        schema = current_contract["schema"]
        if (schema.get("properties", {}).get("refresh") != {
                "title": "Refresh", "type": "boolean",
            } or "refresh" in schema.get("required", [])):
            return False
        legacy_schema = {
            **schema,
            "properties": {key: value for key, value in schema["properties"].items()
                           if key != "refresh"},
        }
        return saved_contract == {**current_contract, "schema": legacy_schema}
    except (ValueError, TypeError, AttributeError):
        return False


def _schema(fields: dict[str, dict], required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": fields,
        "required": required,
        "additionalProperties": False,
    }


LEGACY_ACTIVATE_SKILL_SCHEMA = _schema(
    {"skill_id": {"type": "string", "minLength": 1}}, ["skill_id"],
)


def _plan_schema(model) -> dict:
    """Inline canonical plan definitions for the existing bounded tool validator."""
    schema = model.model_json_schema(by_alias=False)
    definitions = schema.get("$defs", {})

    def inline(value):
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return inline(definitions[value["$ref"].removeprefix("#/$defs/")])
        return {key: inline(item) for key, item in value.items()
                if key not in {"$defs", "default", "pattern"}}

    return inline(schema)


LEGACY_SAVE_PLAN_SCHEMA = _plan_schema(LegacySavePlanInput)
PARALLEL_TOOLS = frozenset({"read_research_activity", "retain_evidence", "search_evidence_library"})
PARALLEL_FETCH_SOURCE_SCHEMA = _plan_schema(FetchSourceInput)
PARALLEL_SEARCH_WEB_SCHEMA = _plan_schema(SearchWebInput)
PARALLEL_SEARCH_PURPOSE = "Exa-first web search: quick for one targeted lookup; research for multiple complementary queries. Check peer activity; identical requests share work within this mission."
LEGACY_SAVE_PLAN_DESCRIPTION = (
    "Save or revise this peer's optional plan. Use expected_version 0 to create "
    "or the current revision to replace the complete plan. No work is scheduled."
)


TOOL_SCHEMAS = {
    "read_research_activity": _plan_schema(ReadResearchActivityInput),
    "retain_evidence": _plan_schema(RetainEvidenceInput),
    "search_evidence_library": _plan_schema(SearchEvidenceLibraryInput),
    "list_source_assets": _plan_schema(ListSourceAssetsInput),
    "fetch_source_asset": _plan_schema(FetchSourceAssetInput),
    "read_document_ocr": _plan_schema(ReadDocumentOCRInput),
    "inspect_figure": _plan_schema(InspectFigureInput),
    "render_source": _plan_schema(RenderSourceInput),
    "search_web": _plan_schema(LegacySearchWebInput),
    "read_search_results": _plan_schema(ReadSearchResultsInput),
    "cancel_search": _plan_schema(CancelSearchInput),
    "save_plan": _plan_schema(SavePlanInput),
    "modify_plan": _plan_schema(ModifyPlanInput),
    "read_plan": _plan_schema(ReadPlanInput),
    "browser_search": _schema({}, []),
    "read_browser_search_result": _schema(
        {"response_id": {"type": "string", "minLength": 1},
         "offset": {"type": "integer", "minimum": 0},
         "limit": {"type": "integer", "minimum": 1, "maximum": 1800},
         "content_sha256": {"type": "string"}}, ["response_id"],
    ),
    "calculate": CALCULATION_TOOL_SCHEMA,
    "search_saved_passages": _schema(
        {"query": {"type": "string", "minLength": 1, "maxLength": 160},
         "cursor": {"type": "integer", "minimum": 0},
         "limit": {"type": "integer", "minimum": 1, "maximum": 10},
         "projection_sha256": {"type": "string"}}, ["query"],
    ),
    "read_skill_resource": _schema(
        {"skill_id": {"type": "string", "minLength": 1},
         "resource": {"type": "string", "enum": ["rich_instructions", "example", "helper_source"]},
         "offset": {"type": "integer", "minimum": 0},
         "limit": {"type": "integer", "minimum": 1, "maximum": 1800},
         "content_sha256": {"type": "string"}}, ["skill_id", "resource"],
    ),
    "compact_conversation": _schema(
        {"summary": {"type": "string", "minLength": 1, "maxLength": 8000}}, ["summary"]
    ),
    "read_conversation_history": _schema(
        {"archive_id": {"type": "string", "minLength": 1},
         "cursor": {"type": "integer", "minimum": 0},
         "limit": {"type": "integer", "minimum": 1, "maximum": 1800},
         "projection_sha256": {"type": "string"}},
        ["archive_id", "cursor", "limit", "projection_sha256"],
    ),
    "activate_skill": {
        **LEGACY_ACTIVATE_SKILL_SCHEMA,
        "properties": {
            **LEGACY_ACTIVATE_SKILL_SCHEMA["properties"],
            "purpose": dict(ACTION_PURPOSE_FIELD),
        },
    },
    "release_skill": _schema({"skill_id": {"type": "string", "minLength": 1}}, ["skill_id"]),
    "release_capability": _schema({"tool_id": {"type": "string", "minLength": 1}}, ["tool_id"]),
    "read_tool_result": _schema(
        {"result_id": {"type": "string"},
         "offset": {"type": "integer", "minimum": 0},
         "limit": {"type": "integer", "minimum": 1, "maximum": 1800},
         "projection_sha256": {"type": "string"}},
        ["result_id", "offset", "limit", "projection_sha256"],
    ),
    "search_scholarly": _schema(
        {
            "route": {
                "type": "string",
                "enum": sorted((set(available_routes()) - {"tavily.search", "searxng.search"}) | set(ROUTE_ALIASES)),
                "description": "Choose an exact route; pubmed and crossref aliases select search only.",
            },
            "query": {"type": "string", "description": "Required nonempty terms for search routes; optional for an exact selected_id or selected_url fetch."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "offset": {"type": "integer", "minimum": 0,
                       "description": "PubMed/Crossref pages only; never use on Europe PMC."},
            "cursor": {"type": "string", "description": "Europe PMC only: '*' or an exact returned cursor."},
            "selected_id": {"type": ["string", "integer", "null"],
                            "description": "Exact PMID or DOI; omit or use null when absent. Integer is accepted for pubmed.pmid and pubmed.abstract."},
            "selected_url": {"type": ["string", "null"]},
            "from_date": {"type": "string", "description": "Search routes only; YYYY-MM-DD."},
            "until_date": {"type": "string", "description": "Search routes only; YYYY-MM-DD."},
            "journal": {"type": "string", "description": "Europe PMC search only."},
            "publication_type": {"type": "string", "description": "Crossref search type only."},
            "exa_mode": {"type": "string", "enum": ["instant", "fast", "auto"],
                         "description": "Exa Search only."},
            "max_characters": {"type": "integer", "minimum": 1, "maximum": 8388608,
                               "description": "Exa Contents text limit only."},
            "max_age_hours": {"type": "integer", "minimum": -1, "maximum": 720,
                              "description": "Exa Contents cache age only."},
            "selection_reason": {"type": "string", "maxLength": 500,
                                 "description": "Optional Exa Contents selection rationale."},
            "resume_esearch_ref": {
                "type": ["object", "null"], "additionalProperties": False,
                "description": "PubMed search only: exact returned saved ID-list reference to retry its summary fetch without a new search.",
                "properties": {"kind": {"type": "string", "enum": ["discovery_esearch_leg"]},
                               "id": {"type": "string"},
                               "version": {"type": "integer", "minimum": 1}},
                "required": ["kind", "id", "version"],
            },
        },
        ["route", "limit"],
    ),
    "search_bheb_corpus": _schema(
        {"query": {"type": "string", "minLength": 1, "maxLength": 1000},
         "refresh": {"type": "boolean", "description": "Request a new retrieval instead of reusing an identical saved query. Different query text or filters already create separate searches."},
         "limit": {"type": "integer", "minimum": 1, "maximum": 10},
         "filters": {"type": "object", "additionalProperties": False,
                     "properties": {
                         **{key: {"type": "string", "minLength": 1, "maxLength": 120}
                            for key in ("access_posture", "condition", "evidence_axis",
                                        "evidence_role", "source_type")},
                         "publication_year": {"type": "integer", "minimum": 1900,
                                              "maximum": 2100},
                     }}},
        ["query"],
    ),
    "inspect_bheb_hit": _schema(
        {"hit_id": {"type": "string", "minLength": 1},
         "search_operation_id": {"type": "string", "minLength": 1}},
        ["hit_id", "search_operation_id"],
    ),
    "inspect_hit": _schema({"hit_id": {"type": "string"}}, ["hit_id"]),
    "fetch_source": _schema({"hit_id": {"type": "string"}}, ["hit_id"]),
    "read_source_window": _schema(
        {
            "source_id": {"type": "string", "description": "Exact current source-copy ID from supplied evidence, inspection, inventory or next_request; not a report ID."},
            "source_version": {"type": "integer", "minimum": 1,
                               "description": "Exact version paired with source_id in the returned reference; never infer a version."},
            "block_offset": {"type": "integer", "minimum": 0},
            "char_offset": {"type": "integer", "minimum": 0},
            "char_limit": {"type": "integer", "minimum": 1,
                           "description": f"Requested characters; one block returns at most {MAX_MISSION_SOURCE_WINDOW_CHARS} with an exact continuation. A saved window enters the next model input only when that complete request fits."},
            "selection_mode": {"type": "string", "enum": ["append", "replace"]},
        },
        ["source_id", "source_version", "block_offset", "char_offset", "char_limit"],
    ),
    "read_source_inventory": _schema(
        {
            "inventory_id": {"type": "string"},
            "inventory_version": {"type": "integer"},
            "cursor": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        ["inventory_id", "inventory_version", "cursor", "limit"],
    ),
    "select_working_sources": _schema(
        {
            "source_refs": {
                "type": "array",
                "items": _schema(
                    {
                        "source_id": {"type": "string"},
                        "source_version": {"type": "integer"},
                    },
                    ["source_id", "source_version"],
                ),
            },
            "expected_version": {"type": "integer"},
        },
        ["source_refs", "expected_version"],
    ),
    "write_note": _schema(
        {
            "note_id": {"type": "string"},
            "category": {"type": "string"},
            "text": {"type": "string"},
            "expected_version": {"type": "integer"},
        },
        ["note_id", "category", "text", "expected_version"],
    ),
    "read_note": _schema({"note_id": {"type": "string"}}, ["note_id"]),
    "search_notes": _schema({"query": {"type": "string"}}, ["query"]),
    "publish_result": _schema(
        {
            "title": {"type": "string"},
            "text": {"type": "string"},
            "citations": {
                "type": "array",
                "description": "Exact source-copy/span pairs from inspected passages. Peer-report and note IDs are not source IDs. Use [] for a summary based only on task input or already read peer records, attributing those records in text.",
                "items": _schema(
                    {
                        "source_id": {"type": "string", "description":
                                      "Exact inspected source-copy ID, not a report, note, URL or publication ID."},
                        "span_id": {"type": "string", "description":
                                    "Exact span ID returned with an inspected passage; never invent a label such as full."},
                    },
                    ["source_id", "span_id"],
                ),
            },
            "limitations": {"type": "array", "items": {"type": "string"}},
            "answer_request_id": {"type": ["string", "null"]},
            "scope_completion": {**_schema(
                {
                    "reason": {"type": "string"},
                    "decisions": {
                        "type": "array",
                        "items": _schema(
                            {
                                "control_id": {"type": "string"},
                                "control_version": {"type": "integer"},
                                "disposition": {"type": "string", "enum": ["continue", "defer"]},
                            },
                            ["control_id", "control_version", "disposition"],
                        ),
                    },
                },
                ["reason", "decisions"],
            ), "type": ["object", "null"]},
        },
        ["title", "text", "citations", "limitations"],
    ),
    "decline_peer_request": _schema(
        {
            "request_id": {"type": "string", "minLength": 1},
            "request_version": {"type": "integer", "minimum": 1},
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        },
        ["request_id", "request_version", "reason"],
    ),
    "review_peer_answer": _schema(
        {
            "request_id": {"type": "string"},
            "answer_id": {"type": "string"},
            "disposition": {"type": "string"},
            "reason": {"type": "string"},
        },
        ["request_id", "answer_id", "disposition", "reason"],
    ),
    "read_context_page": _schema(
        {
            "inclusion_id": {"type": "string"},
            "cursor": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1},
        },
        ["inclusion_id", "cursor", "limit"],
    ),
    "read_context_record": _schema(
        {
            "inclusion_id": {"type": "string", "description":
                             "Copy from read_request or older_records.inclusion_id in the context index; this is not the target record ID."},
            "kind": {"type": "string"},
            "id": {"type": "string", "description":
                   "Exact target record ref.id, separate from inclusion_id."},
            "version": {"type": "integer"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1},
        },
        ["inclusion_id", "kind", "id", "version", "offset", "limit"],
    ),
    "propose_work": _schema(
        {
            "objective": {"type": "string"},
            "expected_artifact": {"type": "string"},
            "next_action": {"type": "string"},
            "deferred": {"type": "boolean"},
        },
        ["objective", "expected_artifact", "next_action", "deferred"],
    ),
    "revise_work": _schema(
        {
            "proposal_id": {"type": "string"},
            "proposal_version": {"type": "integer"},
            "reason": {"type": "string"},
            "objective": {"type": ["string", "null"]},
            "next_action": {"type": ["string", "null"]},
        },
        ["proposal_id", "proposal_version", "reason"],
    ),
    "defer_work": _schema(
        {
            "proposal_id": {"type": "string"},
            "proposal_version": {"type": "integer"},
            "reason": {"type": "string"},
        },
        ["proposal_id", "proposal_version", "reason"],
    ),
    "invite_peer": _schema(
        {
            "proposal_id": {"type": "string"},
            "proposal_version": {"type": "integer"},
            "recipient_alias": {"type": "string"},
            "objective": {"type": "string"},
            "expected_artifact": {"type": "string"},
        },
        ["proposal_id", "proposal_version", "recipient_alias", "objective", "expected_artifact"],
    ),
    "respond_invitation": _schema(
        {
            "decision": {"type": "string", "enum": ["accepted", "declined", "countered"]},
            "counter": {"type": ["string", "null"]},
        },
        ["decision"],
    ),
    "list_optional_work": _schema({}, []),
    "propose_scope_completion": _schema(
        {
            "reason": {"type": "string"},
            "decisions": {
                "type": "array",
                "items": _schema(
                    {
                        "control_id": {"type": "string"},
                        "control_version": {"type": "integer"},
                        "disposition": {"type": "string", "enum": ["continue", "defer"]},
                    },
                    ["control_id", "control_version", "disposition"],
                ),
            },
        },
        ["reason", "decisions"],
    ),
}
TOOL_SCHEMAS.update(CATALOG_TOOL_SCHEMAS)
TOOL_SCHEMAS.update(OBSERVATION_TOOL_SCHEMAS)

TOOL_DESCRIPTIONS = {
    "read_research_activity": "Read a paged snapshot of this mission's peer questions, planned/running searches, progress and remaining gaps. Use it to complement peers' work; this starts no external retrieval. Follow next_request for omitted entries.",
    "retain_evidence": "Retain selected checked, permitted source copies with a brief relevance note for later missions. This curates evidence; it does not certify scientific conclusions. Candidate search snippets cannot be retained as checked sources.",
    "search_evidence_library": "Search the local evidence library by query for metadata only, or provide one returned entry_id to select a source for reading. Selection rechecks current permissions and returns exact reader references; it does not fetch the web or send source text automatically.",
    "list_source_assets": "List figure or table images authored in the exact retained HTML/XML source copy. Supply its source_id and source_version. Captions and links are untrusted source content; this makes no network or model call. Only a selected same-task, currently permitted copy can be used.",
    "fetch_source_asset": "Fetch one authored PNG/JPEG by exact asset_ref from list_source_assets. Returns an image document_ref and page-1 reader actions. Request a deferred reader with request_capability first if its schema is not attached. No arbitrary URL; current parent and image use are rechecked.",
    "read_document_ocr": "Recover printed text from one saved PDF page or PNG/JPEG. Supply document_ref from fetch_source or fetch_source_asset and page_number (1 for an image). Optional region isolates known coordinates; layout selects page, block or sparse labels. Preserve next_request when paging. OCR is unverified transcription, not a checked citation or figure interpretation; no vision call occurs.",
    "inspect_figure": "Ask your own specific visual question about one saved PDF page or PNG/JPEG image. Supply exact document_ref from fetch_source or fetch_source_asset, one-based page_number (1 for an image), and question. Requires this run's vision opt-in; returns an unverified interpretation and usage.",
    "render_source": "Explicit Browserless extraction of a selected public page after fetch_source has saved ordinary HTML. Supply its exact ordinary_document_ref and the missing-content reason. Preserves a separate rendered copy and existing source permissions; no login, unblock, or general discovery. At most one render runs in the shared pool. Read the retained copy before citing.",
    "search_web": "Find web leads, not evidence. quick: one Exa fast query; research: up to ten complementary Exa auto queries. Include information_need; prefer saved sources/URLs/IDs. Tavily or SearXNG may fill gaps; quick uses configured Tavily fallback after known empty/failed retrieval. Identical searches reuse unless refresh=true.",
    "read_search_results": "Page saved operation_id candidates by cursor; no new provider request. wait=true pauses for results. Fetch returned hit IDs before citing.",
    "cancel_search": "Stop pending retrieval in this peer task's operation_id. Completed and late known results remain readable; in-flight external calls cannot be recalled or blindly replayed.",
    "save_plan": "Create this peer's first optional task plan. Provide the complete initial plan; "
    "expected_version must be 0. If a plan exists, use modify_plan. A plan records intent, "
    "not dispatched work or task completion. The skill offers an optional planning template.",
    "modify_plan": "Modify an existing plan using its exact expected_version and a revision_reason. "
    "Supply only changed fields. Top-level lists replace those fields ([] clears an optional list); "
    "update_steps edits existing stable IDs, add_steps adds new IDs, and remove_step_ids removes IDs. "
    "Untouched fields and steps are retained. The entire resulting plan must have valid, acyclic "
    "dependencies. This cannot create a plan or schedule work. Use the last returned revision, "
    "or read_plan when needed; reread after a stale-version rejection.",
    "read_plan": "Read this peer's saved plan, or an exact historical version, with "
    "current task state and stale-reference markers. No arguments reads the current "
    "plan. The note text contains its structured JSON when current source use permits. "
    "A plan is a continuation aid, not accepted evidence or permission to execute work.",
    "board_read": "List open proposal refs, optional work and current registered peer IDs when available, without report or source text. "
    "No arguments.",
    "discover_capabilities": "List this role's tools, purposes and current access. Does not enable tools.",
    "request_capability": "Select an exact request-first local tool_id absent from this request's attached schemas for next turn; call attached tools directly. Does not execute; browser_search is native.",
    "peer_request": "Ask a registered peer by exact instance ID or unique alias/name. "
    "Include brief context and saved refs. Invite absent roles first.",
    "decline_peer_request": "Decline a directed request addressed to you when you cannot answer it. "
    "Use the exact request ID and version from incoming_request, and give a brief reason. "
    "The requester receives your stated reason and chooses how to continue; this does not finish your task.",
    "evidence_lookup": "Look up one inspected source_id among this task's selected source "
    "copies. Returns saved passage IDs and the source URL, without passage text. "
    "Use read_source_window to read the source; report IDs are not source IDs.",
    "browser_search": "Groq-hosted general web discovery on the selected GPT-OSS model. Request this capability to make native browser search available for one non-streaming model turn. Returned text, URLs and citation markers are discovery leads, not inspected source evidence; fetch returned hit IDs before using source passages. This uses the same selected Groq account and its allowance.",
    "read_browser_search_result": "Read this peer's saved browser-enabled response by response_id. Search text is unverified discovery material; only explicit returned URLs become hit IDs for fetch_source. Follow next_request to page exact content without another search. Opaque citation markers are not resolved URLs or inspected passages.",
    "calculate": CALCULATION_TOOL_DESCRIPTION,
    "search_saved_passages": "Find 1-8 word terms in this task's permitted retained text. Snippets are navigation, not citation spans; follow next_request to read_source_window. No match does not establish original-source absence.",
    "read_skill_resource": "Read a bounded page of an active role-granted skill's registered rich_instructions, synthetic example or helper_source. Never executes the helper or reads arbitrary paths. Start at offset 0; follow next_request with its content hash. Activation and package pins are rechecked on every read.",
    "compact_conversation": "Write a concise continuation summary of the current task, open questions, contrary evidence and exact source IDs. This is your own working summary, not verified evidence. The host archives completed visible assistant/tool exchanges without deleting their records. Use read_conversation_history with the returned archive_id to recover exact exchange text.",
    "read_conversation_history": "Read saved assistant/tool exchanges for this actor's task using the archive_id returned by compact_conversation. Start with cursor 0 and empty projection_sha256; follow next_request for later pages. Current source permissions are checked again.",
    "activate_skill": "Load exact role-granted skill_id guidance and tools next turn until released; no execution or new permission.",
    "release_skill": "Unload exact skill_id next turn. Core and separately selected tools, and saved work, remain.",
    "release_capability": "Release exact selected tool_id next turn; core/active-skill tools and saved work remain.",
    "read_tool_result": "Page this task's saved result_id without rerunning it. Follow next_request; empty projection_sha256 starts at offset zero under current permissions.",
    "search_scholarly": "Choose route/limit. Search uses query; exact PMID/DOI uses selected_id; exa.contents uses selected_url. pubmed.abstract returns available text, pubmed.pmid metadata. Europe PMC pages by cursor; PubMed/Crossref by offset. Inspect before citing.",
    "search_bheb_corpus": "Search the configured private BHEB corpus when relevant to behavioral health, digital interventions, product comparisons, or research/reporting guidance. Coverage is selective; inspect the corpus summary. Query by meaning; filter only on populated attributes. Identical queries reuse saved results; changed queries or filters search separately. Use refresh=true for fresh retrieval. Scores rank leads, not evidence quality. Use inspect_bheb_hit to check source text. No generation call.",
    "inspect_bheb_hit": "Inspect an exact hit_id and search_operation_id returned by search_bheb_corpus in this task. A vector match is not a citation; checked source passages are returned only if exact source bytes, identity and current model-use permission can be verified. Otherwise treat it as a lead and seek original source text.",
    "inspect_hit": "Inspect retained text by exact hit_id, not source ID, PMID or DOI. Does not fetch missing text. For metadata-only hits use source_text_options or fetch_source. Returns checked passages only when permitted.",
    "fetch_source": "Fetch public HTML/PDF/XML/text by hit_id, not URL. Read selectable text with read_source_window; OCR scans; inspect_figure handles authorized visual questions. Download alone grants no source use.",
    "read_source_window": "Read selectable text from a permitted source copy, including text PDFs; PNG/JPEG images have no text windows. For image labels use read_document_ocr; for a visual question use opt-in inspect_figure. Request either reader with request_capability first if its schema is not attached. Follow next_request for more text.",
    "read_source_inventory": "Page exact source-copy IDs, versions and use status without source text. "
    "Use inventory_id/version and cursor/limit from a source-free recovery turn. "
    "This lists saved copies; it does not search the web or read their text.",
    "select_working_sources": "Choose exact saved source copies for the next model turn; "
    "use source_refs from the inventory and its current expected_version. An empty list is "
    "allowed. Returns the saved selection; source-use permissions are unchanged.",
    "publish_result": "Save report and finish peer task. Cite claims with inspected source/span pairs; attribute peer input and include limits. Other work may continue.",
    "review_peer_answer": "Review an answer to your request using request_id, answer_id, "
    "disposition answered or partly_answered, and reason. Saves your review, not a report.",
    "read_context_page": "List omitted saved record refs and previews by inclusion_id. "
    "Use a positive item limit and follow next_cursor. For full text, use a supplied "
    "read_request with read_context_record.",
    "read_context_record": "Read authorized report, message, board or note text in bounded "
    "windows. Use a supplied read_request or inclusion_id with exact kind/id/version. "
    "Follow next_request; original sources use checked source readers.",
    "write_note": "Save this actor's task note using note_id and text, with category "
    "note, hypothesis, contradiction, next_action or checkpoint. Use "
    "expected_version 0 for a new note or its exact current version to revise it. "
    "Returns note_ref; a working note is not an accepted report or inspected source.",
    "read_note": "Read this actor's task note by saved note_id. Returns its current "
    "permitted text. No context inclusion_id is needed; use read_context_record for "
    "other saved records identified by a context read_request.",
    "search_notes": "Find this actor's saved notes for the current task using query text. "
    "Returns matching working notes; this does not search publications or other peers' notes.",
    "propose_work": "Create an optional branch with objective, expected_artifact, "
    "next_action and deferred choice. Returns a saved proposal; work can run alongside "
    "requested scope, or remain deferred when selected.",
    "revise_work": "Revise a current proposal, including your task, by proposal_id/version "
    "and reason. Supply objective or next_action changes as needed. Returns the new "
    "proposal version; earlier records remain saved.",
    "defer_work": "Defer an optional branch using its current proposal_id/version and "
    "reason. Saves that branch's deferral; it does not complete the mission.",
    "invite_peer": "Offer a configured role by recipient_alias an open proposal_id/version "
    "with objective and expected_artifact. Returns an invitation for that peer to accept, "
    "decline or counter; use peer_request to question an already registered peer.",
    "respond_invitation": "Decide the invitation assigned to your current task using "
    "decision: accepted, declined or countered, with counter text when countering. "
    "The host supplies the invitation identity and records the outcome.",
    "list_optional_work": "List optional tasks and exact control versions for this run "
    "when choosing which to continue or defer. No arguments. Use board_read for open "
    "proposals as well as optional work.",
    "propose_scope_completion": "Propose completion of requested scope with a reason. "
    "For new missions, omitted optional work continues; give exact control versions only "
    "for explicit continue/defer decisions. Older saved participation policies require "
    "decisions for every open optional control.",
}
TOOL_DESCRIPTIONS.update(CATALOG_TOOL_DESCRIPTIONS)
TOOL_DESCRIPTIONS.update(OBSERVATION_TOOL_DESCRIPTIONS)
