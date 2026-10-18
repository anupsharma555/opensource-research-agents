"""Only checked exact passages from one permitted copy reach a model packet."""

import hashlib

import pytest

import ora.evidence.intake as intake_module
from ora.evidence import EvidenceError, EvidenceLibrary, IntakeService, checked_model_text
from ora.state import StateStore


def _mock_pdf_text(monkeypatch, text):
    geometry = (b'<html xmlns="http://www.w3.org/1999/xhtml">'
                b'<page width="612" height="792"/></html>')

    def run(_content, *options):
        if options == ("-layout",):
            return text
        if options == ("-bbox-layout",):
            return geometry
        raise AssertionError(f"Unexpected PDF extraction options: {options}")

    monkeypatch.setattr("ora.evidence.pdf_content._run", run)


def _library(tmp_path):
    store = StateStore(tmp_path / "state")
    library = EvidenceLibrary(store)
    publication = library.register_publication(
        "publication", identifiers={"doi": "10.1234/model-text"},
        title="Synthetic article", verification="Synthetic exact DOI",
    )["publication_ref"]
    return library, publication


def _copy(library, publication, suffix, content, mime, surface, selections,
          *, truncated=False, missing_fields=None):
    return IntakeService(library).inspect_response(
        f"inspect-{suffix}", publication, content=content, content_type=mime,
        source_url=f"https://example.org/{suffix}", retrieved_at="2026-10-09T12:00:00Z",
        source_version=f"synthetic-{suffix}-v1", rights_status="permitted",
        rights_basis="Synthetic project-owned article", declared_surface=surface,
        expected_doi="10.1234/model-text", identity_basis="Synthetic exact DOI",
        selected_texts=selections, truncated=truncated, missing_fields=missing_fields,
    )


@pytest.mark.parametrize("mime,content,surface,selection,expected", [
    ("text/plain", b"DOI: 10.1234/model-text. Symptoms fell by -3 points.",
     "abstract", "-3 points", "abstract"),
    ("application/xml", b"<article><article-id pub-id-type='doi'>10.1234/model-text"
     b"</article-id><sec><title>Results</title><p>Symptoms fell by -3 points.</p>"
     b"</sec></article>", "full_text", "-3 points", "passage"),
    ("application/pdf", b"%PDF-1.4\nsynthetic bytes", "full_text", "-3 points", "passage"),
])
def test_checked_text_packet_keeps_original_copy_and_selected_passage(
    tmp_path, monkeypatch, mime, content, surface, selection, expected
):
    if mime == "application/pdf":
        _mock_pdf_text(monkeypatch,
                       b"DOI: 10.1234/model-text.\nResults\nSymptoms fell by -3 points.\f")
    library, publication = _library(tmp_path)
    saved = _copy(library, publication, "source", content, mime, surface, [selection])
    assert saved["classification"] == expected
    packet = checked_model_text(library, saved["copy_ref"], saved["passage_refs"])
    assert packet["copy_ref"] == saved["copy_ref"]
    assert packet["content_sha256"] == hashlib.sha256(content).hexdigest()
    assert packet["passage_refs"] == saved["passage_refs"]
    assert packet["source_use_refs"] == [saved["source_use_ref"]]
    assert packet["passages"][0]["text"] == selection
    assert selection in packet["text"] and packet["passages"][0]["locator"] in packet["text"]
    assert packet["text_sha256"] == hashlib.sha256(packet["text"].encode()).hexdigest()
    assert packet["classification"] == expected


def test_partial_packet_retains_omissions_need_and_review_flags(tmp_path):
    library, publication = _library(tmp_path)
    saved = _copy(
        library, publication, "partial",
        b"DOI: 10.1234/model-text. Results: symptoms fell by -3 points. More unavailable.",
        "text/plain", "full_text", ["unreported subgroup", "-3 points"],
        truncated=True, missing_fields=["subgroup outcome"],
    )
    packet = checked_model_text(library, saved["copy_ref"], saved["passage_refs"])
    assert packet["classification"] == "partial" and packet["truncated"] is True
    assert packet["selection_omissions"][0]["index"] == 1
    assert packet["information_needs"][0]["field"] == "subgroup outcome"
    assert packet["passages"][0]["text"] == "-3 points"

    def dispute(tx):
        return {"flags": tx.invalidate(tx.get(**saved["copy_ref"]), "Synthetic correction pending")}

    library.store.transact("flag-copy", {"copy": saved["copy_ref"]}, dispute)
    flagged = checked_model_text(library, saved["copy_ref"], saved["passage_refs"])
    assert flagged["needs_review"]["source_use"]
    assert flagged["passages"][0]["needs_review"]
    assert "-3 points" in flagged["text"]


def test_cross_copy_and_forged_passage_are_rejected(tmp_path):
    library, publication = _library(tmp_path)
    first = _copy(library, publication, "one", b"DOI: 10.1234/model-text. Result A was -3.",
                  "text/plain", "abstract", ["-3"])
    second = _copy(library, publication, "two", b"DOI: 10.1234/model-text. Result B was +4.",
                   "text/plain", "abstract", ["+4"])
    with pytest.raises(EvidenceError, match="different source copy"):
        checked_model_text(library, first["copy_ref"],
                           first["passage_refs"] + second["passage_refs"])

    original = library.store.get(**first["passage_refs"][0])["body"]
    forged_ref = library.store.transact(
        "forge-span", {"test": "mismatched span"},
        lambda tx: {
            "ref": {key: record[key] for key in ("kind", "id", "version")}
            if (record := tx.put("source_passage", "forged", {**original, "text": "+99"})) else None
        },
    )["ref"]
    with pytest.raises(EvidenceError, match="does not replay"):
        checked_model_text(library, first["copy_ref"], [forged_ref])


@pytest.mark.parametrize("mime,content,surface", [
    ("text/plain", b"DOI: 10.1234/model-text. Symptoms fell by -3 points.", "abstract"),
    ("application/xml", b"<article><article-id pub-id-type='doi'>10.1234/model-text"
     b"</article-id><sec><title>Results</title><p>Symptoms fell by -3 points.</p>"
     b"</sec></article>", "full_text"),
    ("application/pdf", b"%PDF-1.4\nsynthetic bytes", "full_text"),
])
def test_each_format_rejects_a_saved_passage_that_differs_from_its_source(
    tmp_path, monkeypatch, mime, content, surface
):
    if mime == "application/pdf":
        _mock_pdf_text(monkeypatch,
                       b"DOI: 10.1234/model-text.\nResults\nSymptoms fell by -3 points.\f")
    library, publication = _library(tmp_path)
    saved = _copy(library, publication, "tamper", content, mime, surface, ["-3 points"])
    original = library.store.get(**saved["passage_refs"][0])["body"]

    def forge(tx):
        record = tx.put("source_passage", "changed-selected-text", {**original, "text": "+3 points"})
        return {"ref": {key: record[key] for key in ("kind", "id", "version")}}

    false_ref = library.store.transact("forge-selected", {"format": mime}, forge)["ref"]
    with pytest.raises(EvidenceError, match="does not replay"):
        checked_model_text(library, saved["copy_ref"], [false_ref])


def test_current_source_use_revocation_blocks_packet(tmp_path):
    library, publication = _library(tmp_path)
    saved = _copy(library, publication, "revoked",
                  b"DOI: 10.1234/model-text. Symptoms fell by -3 points.",
                  "text/plain", "abstract", ["-3 points"])
    checked_model_text(library, saved["copy_ref"], saved["passage_refs"])
    use_ref = saved["source_use_ref"]

    def revoke(tx):
        current = tx.get(**use_ref)
        body = {**current["body"], "allowed_uses": ["discovery"],
                "policy_status": "revoked"}
        tx.put("source_use", use_ref["id"], body, expected_version=use_ref["version"])
        return {"revoked": True}

    library.store.transact("revoke-use", {"source_use_ref": use_ref}, revoke)
    with pytest.raises(EvidenceError, match="does not grant model input"):
        checked_model_text(library, saved["copy_ref"], saved["passage_refs"])


def test_multipassage_pdf_packet_extracts_its_copy_once(tmp_path, monkeypatch):
    _mock_pdf_text(monkeypatch,
                   b"DOI: 10.1234/model-text.\nResults\nGroup A n=40; score -3 points; P<.01.\f")
    library, publication = _library(tmp_path)
    saved = _copy(library, publication, "pdf-multiple", b"%PDF-1.4\nsynthetic bytes",
                  "application/pdf", "full_text", ["Group A n=40", "-3 points", "P<.01"])
    original = intake_module.extract_blocks
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(intake_module, "extract_blocks", counted)
    packet = checked_model_text(library, saved["copy_ref"], saved["passage_refs"])
    assert calls == 1
    assert len(packet["passages"]) == 3


def test_policy_revoked_during_packet_build_rejects_final_packet(tmp_path, monkeypatch):
    library, publication = _library(tmp_path)
    saved = _copy(library, publication, "mid-build",
                  b"DOI: 10.1234/model-text. Results: n=40; score -3 points.",
                  "text/plain", "abstract", ["n=40", "-3 points"])
    original = IntakeService.source_for_model
    calls = 0

    def revoke_after_first(self, passage_ref, *, _prepared=None):
        nonlocal calls
        result = original(self, passage_ref, _prepared=_prepared)
        calls += 1
        if calls == 1:
            use_ref = saved["source_use_ref"]
            def revoke(tx):
                current = tx.get(**use_ref)
                tx.put("source_use", use_ref["id"],
                       {**current["body"], "allowed_uses": ["discovery"],
                        "policy_status": "revoked"}, expected_version=use_ref["version"])
                return {"revoked": True}
            library.store.transact("mid-build-revocation", {"copy": saved["copy_ref"]}, revoke)
        return result

    monkeypatch.setattr(IntakeService, "source_for_model", revoke_after_first)
    with pytest.raises(EvidenceError, match="does not grant model input"):
        checked_model_text(library, saved["copy_ref"], saved["passage_refs"])
    assert calls == 2
