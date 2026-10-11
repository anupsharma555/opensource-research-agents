"""Check exact direct-answer links without promoting guessed or unselected citations."""

import pytest

from ora.coordination.board import ref
from ora.mission.direct_citations import resolve_direct_citations, unresolved_source_markers
from ora.state import StateStore


def records(tmp_path):
    store = StateStore(tmp_path)

    def save(tx):
        copy = tx.put("source_copy", "source-example", {"title": "Example source"})
        passage = tx.put("source_passage", "source-example:window-one", {
            "copy_ref": ref(copy), "span_id": "span-one",
            "inspection_state": "checked", "text": "Example passage.",
        })
        intent = tx.put("model_request_intent", "example-intent", {
            "source_decisions": [{"copy_ref": ref(copy), "passage_refs": [ref(passage)],
                                  "use": "permitted", "allowed_for_model": True}],
        })
        return {"copy": copy, "passage": passage, "intent": intent}

    saved = store.transact("setup", {}, save)
    return store, saved["copy"], saved["passage"], saved["intent"]


def test_exact_markers_preserve_links_and_producing_input_provenance(tmp_path):
    store, copy, passage, intent = records(tmp_path)
    text = "A finding 【source-example:window-one】. Repeated [source-example::span-one]."
    citations, provenance = resolve_direct_citations(store, text, intent, [ref(copy)])
    assert citations == [{"sourceId": copy["id"], "spanId": "span-one"}]
    assert provenance == [{
        "source_id": copy["id"], "span_id": "span-one", "copy_ref": ref(copy),
        "passage_ref": ref(passage), "producing_intent_ref": ref(intent),
        "input_status": "exact_passage_in_producing_call",
        "link_origin": "explicit_direct_response_marker",
    }]


def test_plain_ids_guesses_substrings_and_other_links_do_not_become_citations(tmp_path):
    store, copy, _passage, intent = records(tmp_path)
    text = (
        "source-example:window-one [source-example::span-other] "
        "【prefix-source-example:window-one】 [source-example:window-one-extra] "
        "[source-example:window-one](https://example.invalid/other)"
    )
    assert resolve_direct_citations(store, text, intent, [ref(copy)]) == ([], [])


def test_unselected_or_now_ineligible_passages_are_not_linked(tmp_path):
    store, copy, _passage, intent = records(tmp_path)
    text = "A finding 【source-example:window-one】."
    assert resolve_direct_citations(store, text, intent, []) == ([], [])
    no_passages = {**intent, "body": {"source_decisions": []}}
    assert resolve_direct_citations(store, text, no_passages, [ref(copy)]) == ([], [])


def test_ambiguous_span_alias_is_not_guessed_across_versions(tmp_path):
    store, copy, passage, intent = records(tmp_path)

    def revise(tx):
        newer = tx.put("source_copy", copy["id"], {"title": "Revised source"}, expected_version=1)
        newer_passage = tx.put("source_passage", passage["id"], {
            **passage["body"], "copy_ref": ref(newer), "text": "Revised passage.",
        }, expected_version=1)
        return {"copy": newer, "passage": newer_passage}

    saved = store.transact("revise", {}, revise)
    newer, newer_passage = saved["copy"], saved["passage"]
    both = {**intent, "body": {"source_decisions": [
        *intent["body"]["source_decisions"],
        {"copy_ref": ref(newer), "passage_refs": [ref(newer_passage)],
         "use": "permitted", "allowed_for_model": True},
    ]}}
    assert resolve_direct_citations(
        store, "【source-example:window-one】 [source-example::span-one]",
        both, [ref(copy), ref(newer)],
    ) == ([], [])


@pytest.mark.parametrize("invalid", ["unchecked", "different_copy"])
def test_input_metadata_cannot_promote_an_unchecked_or_mismatched_passage(tmp_path, invalid):
    store, copy, passage, intent = records(tmp_path)

    def revise(tx):
        body = {**passage["body"]}
        if invalid == "unchecked":
            body["inspection_state"] = "unreviewed"
        else:
            body["copy_ref"] = {**ref(copy), "id": "another-source"}
        return tx.put("source_passage", passage["id"], body, expected_version=1)

    changed = store.transact("invalid-passage", {}, revise)
    selected = {**intent, "body": {"source_decisions": [{
        "copy_ref": ref(copy), "passage_refs": [ref(changed)],
        "use": "permitted", "allowed_for_model": True,
    }]}}
    assert resolve_direct_citations(
        store, "【source-example:window-one】", selected, [ref(copy)],
    ) == ([], [])


def test_metadata_only_input_cannot_be_described_as_a_text_citation(tmp_path):
    store, copy, _passage, intent = records(tmp_path)
    metadata = {**intent, "body": {"source_decisions": [{
        **intent["body"]["source_decisions"][0],
        "use": "unknown", "allowed_for_model": False,
    }]}}
    assert resolve_direct_citations(
        store, "【source-example:window-one】", metadata, [ref(copy)],
    ) == ([], [])


def test_mixed_known_and_unknown_source_markers_are_distinguished(tmp_path):
    store, copy, _passage, intent = records(tmp_path)
    text = "Known 【source-example:window-one】, unknown [source-other:span-two], [ordinary label]."
    citations, provenance = resolve_direct_citations(store, text, intent, [ref(copy)])
    assert len(citations) == 1
    assert unresolved_source_markers(text, provenance) == 1
