"""Source-free context keeps one exact saved-reading and navigation card."""

from __future__ import annotations

import json

from tests.mission.test_context_metadata_compaction import _source_free_case


def test_one_card_keeps_saved_coverage_and_exact_uncovered_cursor(tmp_path):
    store, context, task, copy_ref, window, refs, _result, call, _inventory, history = (
        _source_free_case(tmp_path)
    )
    pack = context.assemble(
        call, task, control_ref=refs["control"], history=history,
    )
    index = json.loads(pack.messages[-1]["content"])
    assert "retained_reading_progress" not in index
    assert len(index["source_free_reading_navigation"]) == 1
    card = index["source_free_reading_navigation"][0]
    assert card["copy_ref"] == copy_ref
    assert card["selected_passage_count"] == 2
    assert card["saved_coverage"]["status"] == "incomplete_extracted_text"
    assert card["saved_coverage"]["original_source_partial"] is False
    assert card["saved_coverage"]["covered_chars"] == 2400
    assert card["saved_coverage"]["first_uncovered_cursor"] == {
        "block_offset": 0, "char_offset": 2400,
    }
    assert card["uncovered_chars"] == card["saved_coverage"]["total_chars"] - 2400
    assert card["latest_window_ref"] == window["window_ref"]
    assert card["latest_window_next_cursor"] == window["next_cursor"]
    assert card["fetch_first_uncovered"] == {
        "tool": "read_source_window", "source_id": copy_ref["id"],
        "source_version": copy_ref["version"],
        "block_offset": 0, "char_offset": 2400, "char_limit": 1900,
    }
    assert card["source_text_in_this_call"] is False
    assert card["past_model_delivery"] == "unverified"
    assert pack.source_refs == ()
    assert store.get(**copy_ref) is not None
