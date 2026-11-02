"""Create files describing the allowed fields and values in research records.

These JSON Schemas and test examples come from the Python definitions in
``models.py`` so other languages can check the same shapes. With ``--check``, this
file reports outdated generated files without changing them.
"""

import argparse
import difflib
import json
from pathlib import Path

from .examples import generate_examples
from .models import (
    AdjustPValuesInput,
    AnalyzeContingencyInput,
    AnalyzePairedValuesInput,
    AnalyzeValuesInput,
    CancelSearchInput,
    Command,
    CompareSummaryStatisticsInput,
    ConvertUnitsInput,
    ConvertValuesUnitsInput,
    CorrelateValuesInput,
    DescribeValuesInput,
    Event,
    EvidencePacket,
    FetchSourceAssetInput,
    FetchSourceInput,
    InspectFigureInput,
    JobRequest,
    ListSourceAssetsInput,
    LiveBranchCancelReceipt,
    LiveBranchCancelRequest,
    LiveFollowupRequest,
    LiveReceipt,
    LiveRunCancelReceipt,
    LiveRunCancelRequest,
    LiveStartRequest,
    LiveState,
    LiveUserInputReceipt,
    LiveUserInputRequest,
    ModifyPlanInput,
    OperationResult,
    Query,
    ReadDocumentOCRInput,
    ReadResearchActivityInput,
    ReadSearchResultsInput,
    Receipt,
    RenderSourceInput,
    ResearchSnapshot,
    RetainEvidenceInput,
    RetrievalEnvelope,
    RuntimePlan,
    SavePlanInput,
    SearchEvidenceLibraryInput,
    SearchOperationResult,
    SearchWebInput,
    Subscription,
)

MODELS = {
    "adjust_pvalues": AdjustPValuesInput,
    "analyze_contingency": AnalyzeContingencyInput,
    "analyze_paired_values": AnalyzePairedValuesInput,
    "compare_summary_statistics": CompareSummaryStatisticsInput,
    "convert_values_units": ConvertValuesUnitsInput,
    "correlate_values": CorrelateValuesInput,
    "describe_values": DescribeValuesInput,
    "analyze_values": AnalyzeValuesInput,
    "convert_units": ConvertUnitsInput,
    "read_research_activity": ReadResearchActivityInput,
    "retain_evidence": RetainEvidenceInput,
    "search_evidence_library": SearchEvidenceLibraryInput,
    "fetch_source": FetchSourceInput,
    "list_source_assets": ListSourceAssetsInput,
    "fetch_source_asset": FetchSourceAssetInput,
    "render_source": RenderSourceInput,
    "read_document_ocr": ReadDocumentOCRInput,
    "inspect_figure": InspectFigureInput,
    "search_web": SearchWebInput,
    "read_search_results": ReadSearchResultsInput,
    "cancel_search": CancelSearchInput,
    "search_operation": SearchOperationResult,
    "research": ResearchSnapshot,
    "runtime_plan": RuntimePlan,
    "save_plan": SavePlanInput,
    "modify_plan": ModifyPlanInput,
    "command": Command,
    "query": Query,
    "event": Event,
    "receipt": Receipt,
    "evidence": EvidencePacket,
    "job": JobRequest,
    "operation": OperationResult,
    "subscription": Subscription,
    "live_start": LiveStartRequest,
    "live_followup": LiveFollowupRequest,
    "live_branch_cancel": LiveBranchCancelRequest,
    "live_branch_cancel_receipt": LiveBranchCancelReceipt,
    "live_run_cancel": LiveRunCancelRequest,
    "live_run_cancel_receipt": LiveRunCancelReceipt,
    "live_receipt": LiveReceipt,
    "live_state": LiveState,
    "live_user_input": LiveUserInputRequest,
    "live_user_input_receipt": LiveUserInputReceipt,
    "retrieval": RetrievalEnvelope,
}


def generate(root: Path, check: bool = False) -> bool:
    output = root / "contracts" / "generated"
    valid = True
    for name, model in MODELS.items():
        schema = model.model_json_schema(by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        expected = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        path = output / f"{name}.schema.json"
        if check:
            actual = path.read_text() if path.exists() else ""
            if actual != expected:
                valid = False
                print(f"Stale generated schema: {path.relative_to(root)}; run scripts/dev generate")
                print(
                    "".join(
                        difflib.unified_diff(actual.splitlines(True), expected.splitlines(True))
                    )
                )
        else:
            output.mkdir(parents=True, exist_ok=True)
            path.write_text(expected)
    return generate_examples(root, check) and valid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if generate(Path.cwd(), args.check) else 1)


if __name__ == "__main__":
    main()
