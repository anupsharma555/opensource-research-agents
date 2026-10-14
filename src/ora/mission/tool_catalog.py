"""Describe the research tools an agent can discover or request during a mission.

Define each tool's name, purpose and accepted arguments, and group tools for
ordinary research, invitations and source recovery. The mission service uses
these definitions to build the tool descriptions saved with each model request;
it separately checks permissions and executes the selected actions.
"""

from __future__ import annotations

from ora.agents.capabilities import content_hash
from ora.contracts.models import ReadPlanInput, SavePlanInput
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
CORE_INVITATION_TOOLS = frozenset(
    {"discover_capabilities", "request_capability", "respond_invitation", "read_context_page"}
)
SOURCE_RECOVERY_TOOLS = frozenset({"read_source_inventory", "select_working_sources"})
UNHANDLED_COMMON_TOOLS = frozenset({"claim_work", "peer_invite", "subscribe"})
TOOL_PURPOSES = {
    "browser_search": "Select Groq-native web discovery next turn; it is not a local function",
    "read_browser_search_result": "Page this task's saved web discovery text and links",
    "calculate": "Calculate arithmetic from supplied values; no source validation",
    "search_saved_passages": "Find text within this task's permitted saved sources",
    "read_skill_resource": "Read an active skill's instructions, examples or helper source",
    "activate_skill": "Load optional workflow guidance and its permitted tools",
    "release_skill": "Release optional skill guidance and its tool selections",
    "release_capability": "Release one explicit tool selection",
    "read_tool_result": "Page a saved tool result using current permissions",
    "compact_conversation": "Save this peer's concise task continuation summary and archive completed visible assistant/tool exchanges",
    "read_conversation_history": "Page this peer's saved assistant/tool exchanges with current source permissions",
    "board_read": "List open board proposals and optional work",
    "claim_work": "Low-level host claim; executor manages mission claims",
    "discover_capabilities": "List tools, purposes and current access status",
    "evidence_lookup": "List passage IDs and URL for one current source copy",
    "peer_invite": "Low-level invite; use invite_peer in missions",
    "peer_request": "Ask a registered peer a focused question",
    "request_capability": "Select one permitted optional tool for the next turn",
    "subscribe": "Low-level subscription; host delivers mission updates",
    "defer_work": "Defer an exact optional proposal",
    "fetch_source": "Fetch and inspect one saved hit's public URL",
    "inspect_hit": "Inspect retained text from one saved retrieval hit",
    "invite_peer": "Invite a configured role to exact open work",
    "list_optional_work": "List optional tasks and their current versions",
    "propose_scope_completion": "Propose completion of the requested scope",
    "propose_work": "Create an optional research branch",
    "publish_result": "Save a report and finish this peer's task",
    "read_context_page": "List saved record references and short previews",
    "read_context_record": "Read saved report, message or note text",
    "read_note": "Read this peer's task note by note ID",
    "read_source_window": "Read source text and return exact citation spans",
    "read_source_inventory": "List saved source copies and access status during recovery",
    "select_working_sources": "Choose source copies for the next model turn",
    "respond_invitation": "Accept, decline or counter your saved invitation",
    "review_peer_answer": "Record your review of an answer to your request",
    "revise_work": "Revise an exact current work proposal",
    "search_notes": "Search this peer's task notes",
    "search_scholarly": "Search scholarly records or retrieve selected content",
    "write_note": "Save or revise this peer's task note",
    "save_plan": "Save or revise this peer's optional task plan without scheduling work",
    "read_plan": "Read this peer's exact plan revision and changed reference heads",
    "list_venues": "List dated research-venue discovery leads",
    "propose_venue": "Save an additional research-venue lead",
    "navigate_venue": "Open one saved venue and list public links",
    "page_venue_navigation": "Page saved venue links without another fetch",
    "inspect_navigation_document": "Check retained HTML from this task's saved navigation for source reading",
    "estimate_latency": "Record or revise a rough task duration expectation",
    "wait_for_dependencies": "Wait for specific peer answers or prerequisite tasks",
}
ROUTE_ALIASES = {
    "pubmed": "pubmed.search",
    "crossref": "crossref.search",
    "europe_pmc": "europe_pmc.search",
    "exa": "exa.search",
}
MAX_MISSION_SOURCE_WINDOW_CHARS = 16000


def tool_contract_matches(current: dict, saved: dict) -> bool:
    """Allow prose-only updates without changing a saved tool's executable contract."""
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
        return contract(current) == contract(saved)
    except (ValueError, TypeError, AttributeError):
        return False


def _schema(fields: dict[str, dict], required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": fields,
        "required": required,
        "additionalProperties": False,
    }


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


TOOL_SCHEMAS = {
    "save_plan": _plan_schema(SavePlanInput),
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
    "activate_skill": _schema({"skill_id": {"type": "string", "minLength": 1}}, ["skill_id"]),
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
                "enum": sorted(set(available_routes()) | set(ROUTE_ALIASES)),
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
    "save_plan": "Save an optional, advisory plan for this peer's current task. Use "
    "expected_version 0 to create or the current plan version to revise. Give a "
    "reason and exact evidence/work/peer references when relevant. Step dependencies "
    "describe intended reasoning order; they do not schedule work, assign peers, "
    "satisfy obligations or complete the task. Repeated revisions without new research "
    "progress return a local churn diagnostic; other research tools remain usable.",
    "read_plan": "Read this peer's saved plan, or an exact historical version, with "
    "current task state and stale-reference markers. No arguments reads the current "
    "plan. The note text contains its structured JSON when current source use permits. "
    "A plan is a continuation aid, not accepted evidence or permission to execute work.",
    "board_read": "List this mission's open proposal references and optional work. "
    "No arguments needed; topic is unused. Returns work summaries, not report/source text.",
    "discover_capabilities": "List granted tools, purposes and current access status. "
    "No arguments. Use when the current menu is insufficient; this does not enable tools.",
    "request_capability": "Select a request-first tool by exact tool_id; wait for its "
    "schema next turn before calling. Does not execute it or supply missing data. "
    "browser_search selects a native provider turn. Call attached tools directly.",
    "peer_request": "Send a focused question to another registered peer using "
    "target_instance_id or its unique alias/name. Include brief context and saved "
    "references in question. Returns a request, not an answer. Invite absent roles first.",
    "evidence_lookup": "Look up one inspected source_id among this task's selected source "
    "copies. Returns saved passage IDs and the source URL, without passage text. "
    "Use read_source_window to read the source; report IDs are not source IDs.",
    "browser_search": "Groq-hosted general web discovery on the selected GPT-OSS model. Request this capability to make native browser search available for one non-streaming model turn. Returned text, URLs and citation markers are discovery leads, not inspected source evidence; fetch returned hit IDs before using source passages. This uses the same selected Groq account and its allowance.",
    "read_browser_search_result": "Read this peer's saved browser-enabled response by response_id. Search text is unverified discovery material; only explicit returned URLs become hit IDs for fetch_source. Follow next_request to page exact content without another search. Opaque citation markers are not resolved URLs or inspected passages.",
    "calculate": CALCULATION_TOOL_DESCRIPTION,
    "search_saved_passages": "Search this task's currently permitted retained extracted text with 1-8 word terms. Results are navigation snippets, not checked citation passages. Follow a hit's next_request to read_source_window; follow the page next_request for more matches. No match does not establish absence from the original source.",
    "read_skill_resource": "Read a bounded page of an active role-granted skill's registered rich_instructions, synthetic example or helper_source. Never executes the helper or reads arbitrary paths. Start at offset 0; follow next_request with its content hash. Activation and package pins are rechecked on every read.",
    "compact_conversation": "Write a concise continuation summary of the current task, open questions, contrary evidence and exact source IDs. This is your own working summary, not verified evidence. The host archives completed visible assistant/tool exchanges without deleting their records. Use read_conversation_history with the returned archive_id to recover exact exchange text.",
    "read_conversation_history": "Read saved assistant/tool exchanges for this actor's task using the archive_id returned by compact_conversation. Start with cursor 0 and empty projection_sha256; follow next_request for later pages. Current source permissions are checked again.",
    "activate_skill": "Load optional workflow guidance using the exact role-granted skill_id. Its instructions and permitted tools become available next turn until released. It does not grant permissions or execute those tools.",
    "release_skill": "Unload an optional skill by its exact skill_id next turn. Core rules and separately requested tools remain; saved research is retained.",
    "release_capability": "Release an explicit selection by tool_id next turn. Core or active-skill tools remain exposed; this does not cancel work or delete results.",
    "read_tool_result": "Read a bounded saved tool result by result_id for this actor and task, without executing the tool again. Follow next_request; use an empty projection_sha256 only at offset zero to start a fresh current-permission projection.",
    "search_scholarly": "Choose route and limit: search routes use query; pubmed.pmid "
    "(metadata), pubmed.abstract (abstract text) and crossref.doi use selected_id. "
    "Numeric PMID IDs are accepted. exa.contents uses selected_url. Route IDs are "
    "arguments, never function names. Europe PMC uses cursor; PubMed/Crossref use offset. "
    "Use supported filters and returned next_request. Returns metadata/text and hit IDs "
    "as the route permits; inspect hits before citing passages.",
    "inspect_hit": "Inspect a saved retrieval hit by exact hit_id, not a source-copy ID, PMID or DOI. "
    "Checks already-retained content; it does not fetch missing text from the web. "
    "Returns a checked passage reference only when source use permits it.",
    "fetch_source": "Fetch and inspect the public URL identified by a saved hit_id on an "
    "enabled host; do not pass a URL as hit_id. Returns source/inspection results. "
    "A successful fetch alone does not grant source-use permission.",
    "read_source_window": "Read one bounded exact block window of an inspected permitted source copy. "
    "Use the exact source_id and source_version from a supplied source packet, source inventory "
    "or inspection result; a retained source copy does not need inspect_hit first. "
    "Returns source text and exact citation spans, not report or note text. "
    "Choose a positive char_limit for the "
    f"needed text, clamped to {MAX_MISSION_SOURCE_WINDOW_CHARS} characters per block. next_request continues the exact copy. "
    "A saved window enters the next model input only if its complete request fits. New passages "
    "append to next-call selection by default, so adjacent windows can be read together. "
    "Set selection_mode=replace to focus the next model input on this window; earlier "
    "source-window text is then absent from that input, though its saved references remain "
    "available. For a full reread from offset 0, replace avoids retaining the earlier "
    "prefix twice. Use notes or append if joint reading is needed.",
    "read_source_inventory": "Page exact source-copy IDs, versions and use status without source text. "
    "Use inventory_id/version and cursor/limit from a source-free recovery turn. "
    "This lists saved copies; it does not search the web or read their text.",
    "select_working_sources": "Choose exact saved source copies for the next model turn; "
    "use source_refs from the inventory and its current expected_version. An empty list is "
    "allowed. Returns the saved selection; source-use permissions are unchanged.",
    "publish_result": "Save this peer's report and finish its task. Cite original-source "
    "claims with exact inspected source/span pairs; attribute peer records in text. "
    "Include limitations. Other mission work may continue.",
    "review_peer_answer": "Review an answer to your request using request_id, answer_id, "
    "disposition answered or partly_answered, and reason. Saves your review, not a report.",
    "read_context_page": "List saved record references and short previews omitted from a prior "
    "model input. Read full report text with read_context_record; an existing read_request "
    "can be used directly. Use the inclusion_id shown in that input and a positive item "
    "limit. The returned page may be smaller; follow next_cursor to continue listing.",
    "read_context_record": "Read the text of one authorized saved report, message, board "
    "record or working note in bounded windows. Use its read_request arguments when supplied. "
    "inclusion_id identifies the context index; kind/id/version identify the record. "
    "Returns permitted text or its current access status. Follow next_request for more "
    "text; original sources use checked source readers.",
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
