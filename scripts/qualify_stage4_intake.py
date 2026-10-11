"""Explicit bounded real-source qualification for ANU-414; writes only to .local.

Run with ``.venv/bin/python scripts/qualify_stage4_intake.py --live``. This is
deterministic extraction over public article APIs, not a model or mission run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from ora.evidence import EvidenceLibrary
from ora.evidence.intake import IntakeService, extract_blocks, fetch_public_source
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".local" / "stage4"
DOI_MAIN = "10.2196/14754"
DOI_PDF = "10.1371/journal.pone.0028099"
HOSTS = {"www.ebi.ac.uk", "pmc.ncbi.nlm.nih.gov", "journals.plos.org", "clinicaltrials.gov"}
CORE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urlencode(
    {"query": f"DOI:{DOI_MAIN}", "format": "json", "resultType": "core"}
)
XML_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC6887812/fullTextXML"
HTML_URL = "https://pmc.ncbi.nlm.nih.gov/articles/PMC6887812/"
PDF_PAGE = "https://journals.plos.org/plosone/article?id=" + DOI_PDF
PDF_URL = "https://journals.plos.org/plosone/article/file?" + urlencode(
    {"id": DOI_PDF, "type": "printable"}
)
REGISTRY_URL = "https://clinicaltrials.gov/api/v2/studies/NCT03244878"


def ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Actually call public article endpoints")
    parser.add_argument("--table-review", type=Path, default=PRIVATE / "table-check" / "table-review.json")
    args = parser.parse_args()
    if not args.live:
        parser.error("Live public retrieval is explicit: pass --live")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = PRIVATE / "qualifications" / stamp
    raw = root / "raw"
    raw.mkdir(parents=True)
    store = StateStore(root / "state")
    library = EvidenceLibrary(store)
    intake = IntakeService(library)
    requests = []
    cases = []

    def fetch(label: str, url: str, *, retain_payload: bool = True) -> dict:
        started = time.monotonic()
        response = fetch_public_source(url, allowed_hosts=HOSTS)
        payload = response["content"]
        if retain_payload:
            (raw / f"{label}.bin").write_bytes(payload)
        requests.append({
            "label": label, "url": url, "status": response["status"],
            "final_url": response["final_url"], "content_type": response["content_type"],
            "retrieved_at": response["retrieved_at"], "bytes": len(payload),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "sha256": sha(payload), "raw_path": f"raw/{label}.bin" if retain_payload else None,
        })
        return response

    core = fetch("europe-pmc-core", CORE_URL)
    metadata = json.loads(core["content"])["resultList"]["result"]
    if len(metadata) != 1 or metadata[0].get("doi", "").lower() != DOI_MAIN:
        raise RuntimeError("Core metadata did not identify the expected article")
    article = metadata[0]
    if article.get("pmcid") != "PMC6887812" or article.get("isOpenAccess") != "Y":
        raise RuntimeError("Core metadata lacks the expected open article identity")
    xml = fetch("europe-pmc-xml", XML_URL)
    xml_root = ET.fromstring(xml["content"])
    licenses = [" ".join(" ".join(node.itertext()).split()) for node in xml_root.iter()
                if node.tag.rsplit("}", 1)[-1] == "license"]
    if not any("creativecommons.org/licenses/by/4.0/" in value for value in licenses):
        raise RuntimeError("Expected source-copy reuse notice is absent")
    policy_main = f"Open article CC BY 4.0 notice in exact Europe PMC XML {sha(xml['content'])}"
    main_pub = library.register_publication(
        "live-publication-main", identifiers={"doi": DOI_MAIN, "pmcid": "PMC6887812"},
        title=article["title"], verification="Exact Europe PMC core DOI and PMCID fields",
    )["publication_ref"]

    abstract = article["abstractText"].encode("utf-8")
    chosen = "intervention group (n=181) had significantly lower depression symptom severity"
    abstract_saved = intake.inspect_response(
        "live-abstract", main_pub, content=abstract, content_type="application/abstract+text",
        source_url=CORE_URL, retrieved_at=core["retrieved_at"],
        source_version=f"core-abstract-sha256:{sha(core['content'])}", rights_status="permitted",
        rights_basis=policy_main + "; same published article, core abstract field",
        declared_surface="abstract", selected_texts=[chosen],
        identity_basis=f"Exact Europe PMC core DOI={DOI_MAIN}, PMCID=PMC6887812, response SHA-256={sha(core['content'])}",
    )
    if abstract_saved["classification"] != "abstract" or not abstract_saved["passage_refs"]:
        raise RuntimeError("Real abstract did not yield a usable exact passage")
    cases.append({"level": "very_simple", "case": "published_abstract", "source": "live",
                  "expected": "reported comparison and direction only", "observed": abstract_saved,
                  "missing_method": "allocation concealment not established by chosen span"})

    xml_blocks, _ = extract_blocks(xml["content"], "application/xml")
    results = next(block for block in xml_blocks if "section=Results" in block.locator and "n=181" in block.text)
    selected_xml = results.text[:190]
    xml_saved = intake.inspect_response(
        "live-xml", main_pub, content=xml["content"], content_type="application/xml",
        source_url=XML_URL, retrieved_at=xml["retrieved_at"],
        source_version=f"xml-sha256:{sha(xml['content'])}", rights_status="permitted",
        rights_basis=policy_main, declared_surface="full_text", expected_doi=DOI_MAIN,
        selected_texts=[selected_xml], identity_basis="XML article-id DOI and Europe PMC core identity match",
    )
    if xml_saved["classification"] != "full_text" or not xml_saved["passage_refs"]:
        raise RuntimeError("Real XML did not yield a verified deeper passage")
    cases.append({"level": "simple", "case": "open_xml_results", "source": "live",
                  "expected": "section and result numbers preserved", "observed": xml_saved})

    html = fetch("pmc-html", HTML_URL)
    html_saved = intake.inspect_response(
        "live-html", main_pub, content=html["content"], content_type="text/html",
        source_url=HTML_URL, retrieved_at=html["retrieved_at"],
        source_version=f"html-sha256:{sha(html['content'])}", rights_status="permitted",
        rights_basis=policy_main + "; alternate rendering of same CC BY article",
        declared_surface="full_text", expected_doi=DOI_MAIN,
        identity_basis="PMC article URL, title and Europe PMC core PMCID match",
    )
    cases.append({"level": "simple", "case": "alternate_html", "source": "live",
                  "expected": "same DOI and article text; no automatic selected claim", "observed": html_saved})

    page = fetch("plos-license-page", PDF_PAGE)
    if b"Creative Commons Attribution License" not in page["content"]:
        raise RuntimeError("Publisher source-use notice missing")
    pdf = fetch("plos-pdf", PDF_URL)
    if not pdf["content"].startswith(b"%PDF"):
        raise RuntimeError("PDF endpoint returned a non-PDF response")
    pdf_pub = library.register_publication(
        "live-publication-pdf", identifiers={"doi": DOI_PDF},
        title="Internet-Based CBT for Depression with and without Telephone Tracking in a National Helpline",
        verification="Publisher PDF first page and article DOI",
    )["publication_ref"]
    table_review = json.loads(args.table_review.read_text()) if args.table_review.exists() else None
    if table_review and table_review.get("pdf_sha256") != sha(pdf["content"]):
        raise RuntimeError("PDF changed since the rendered-original review")
    if table_review:
        rendered = root / "table-page-5.png"
        subprocess.run(["pdftoppm", "-f", "5", "-l", "5", "-png", "-r", "150", "-singlefile",
                        str(raw / "plos-pdf.bin"), str(rendered.with_suffix(""))], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if sha(rendered.read_bytes()) != table_review["rendered_original_sha256"]:
            raise RuntimeError("Rendered table page differs from the visually reviewed original")
    pdf_blocks, _ = extract_blocks(pdf["content"], "application/pdf")
    table_page = next(block.text for block in pdf_blocks if block.locator.startswith("page=5"))
    selected_table = [
        next(line for line in table_page.splitlines() if needle in line)
        for needle in ("Web only (n = 38)", "Sex (female), n (%)", "Not all participants completed all questions.",
                       "*Education (n = 150)")
    ]
    pdf_saved = intake.inspect_response(
        "live-pdf", pdf_pub, content=pdf["content"], content_type="application/pdf",
        source_url=PDF_URL, retrieved_at=pdf["retrieved_at"],
        source_version=f"pdf-sha256:{sha(pdf['content'])}", rights_status="permitted",
        rights_basis=f"Publisher article CC Attribution notice at {PDF_PAGE}; page sha256 {sha(page['content'])}",
        declared_surface="full_text", expected_doi=DOI_PDF,
        selected_texts=selected_table, table_check=table_review,
        identity_basis="PLOS publisher page DOI and PDF first-page identity match",
    )
    if pdf_saved["classification"] != "full_text" or not pdf_saved["passage_refs"]:
        raise RuntimeError("Real PDF table passage was not retained")
    cases.append({"level": "intermediate", "case": "rendered_pdf_table", "source": "live",
                  "expected": "Table 1 page 5 row/header/denominator/footnote checked", "observed": pdf_saved,
                  "rendered_review": bool(table_review)})
    pdf_wrong = intake.inspect_response(
        "live-pdf-wrong-identity", main_pub, content=pdf["content"], content_type="application/pdf",
        source_url=PDF_URL, retrieved_at=pdf["retrieved_at"],
        source_version=f"pdf-sha256:{sha(pdf['content'])}", rights_status="permitted",
        rights_basis="Publisher CC Attribution article; intentionally paired with a different DOI",
        declared_surface="full_text", expected_doi=DOI_MAIN,
        identity_basis="PLOS publisher PDF identity, intentionally mismatched to target",
    )
    if pdf_wrong["classification"] != "wrong_article" or pdf_wrong["copy_ref"] is not None:
        raise RuntimeError("Actual alternate article was not rejected as wrong-article evidence")
    cases.append({"level": "intermediate", "case": "wrong_article_pdf", "source": "live_bytes_mispaired",
                  "expected": "wrong article, no target copy", "observed": pdf_wrong})

    # Named missing field opens two separate research paths. The registry ID
    # appears in this article's XML; independent PDF concerns another study.
    registry_ids = [" ".join(" ".join(n.itertext()).split()) for n in xml_root.iter()
                    if n.tag.rsplit("}", 1)[-1] == "ext-link" and "NCT03244878" in " ".join(n.itertext())]
    if not registry_ids and b"NCT03244878" not in xml["content"]:
        raise RuntimeError("Article XML did not establish expected registry ID")
    registry_response = fetch("clinicaltrials-registry", REGISTRY_URL, retain_payload=False)
    registry_data = json.loads(registry_response["content"])
    registry_identity = registry_data["protocolSection"]["identificationModule"]
    if registry_identity["nctId"] != "NCT03244878":
        raise RuntimeError("Registry response has a different study ID")
    family = library.register_study_family(
        "live-family", registry_ids=["NCT03244878"], verification="Registry ID in exact article XML",
    )["family_ref"]
    registry_pub = library.register_publication(
        "live-registry-publication", identifiers={},
        title=registry_identity["briefTitle"], kind="registry_report",
        verification=f"Registry ID in article XML and live ClinicalTrials API SHA-256 {sha(registry_response['content'])}",
    )["publication_ref"]
    library.link_publication_to_family("live-main-family", main_pub, family,
                                      relation="reports_study", basis="Trial registration in article XML")
    library.link_publication_to_family("live-registry-family", registry_pub, family,
                                      relation="registry_for_study", basis="Same exact NCT ID in article XML and live registry API")
    need = intake.open_information_need(
        "live-need-allocation", main_pub, field="allocation concealment details",
        question="Does the same-study registry or manuscript report concealment?",
        missing_from=abstract_saved["copy_ref"], same_study_refs=[registry_pub],
        independent_context_refs=[pdf_pub],
    )
    cases.append({"level": "advanced", "case": "two_path_missing_field", "source": "live_retrieval_plus_saved_records",
                  "expected": "same-study registry candidate and independent context kept distinct",
                  "observed_need_ref": need, "qualifier": "Candidates not asserted to answer missing field"})
    damaged = intake.inspect_response(
        "live-xml-truncated-injection", main_pub, content=xml["content"][:600],
        content_type="application/xml", source_url=XML_URL,
        retrieved_at=xml["retrieved_at"], source_version="controlled-truncation-v1",
        rights_status="permitted", rights_basis=policy_main, declared_surface="full_text",
        expected_doi=DOI_MAIN, truncated=True,
    )
    if damaged["passage_refs"] or damaged["classification"] not in {"partial", "empty", "parser_failure"}:
        raise RuntimeError("Truncated XML was incorrectly promoted to a usable passage")
    cases.append({"level": "advanced", "case": "truncated_xml", "source": "simulated_failure_on_live_bytes",
                  "expected": "no claim-ready passage; prior abstract remains", "observed": damaged})
    partial_abstract = intake.inspect_response(
        "live-abstract-partial", main_pub,
        content=abstract[:abstract.find(chosen.encode()) + len(chosen) + 1],
        content_type="application/abstract+text", source_url=CORE_URL,
        retrieved_at=core["retrieved_at"], source_version="controlled-partial-core-abstract-v1",
        rights_status="permitted", rights_basis=policy_main,
        declared_surface="abstract", identity_basis="Exact Europe PMC core DOI and PMCID",
        selected_texts=[chosen], truncated=True, missing_fields=["subgroup outcome"],
    )
    if (partial_abstract["classification"] != "partial" or not partial_abstract["passage_refs"]
            or not partial_abstract["information_need_refs"]):
        raise RuntimeError("Intact span in partial abstract did not remain usable")
    if intake.source_for_model(partial_abstract["passage_refs"][0])["text"] != chosen:
        raise RuntimeError("Intact partial-abstract span did not replay")
    cases.append({"level": "advanced", "case": "partial_abstract_with_span", "source": "controlled_partial_on_live_bytes",
                  "expected": "exact reported span usable at passage scope; subgroup outcome open",
                  "observed": partial_abstract})
    login = intake.inspect_response(
        "injected-login", main_pub, content=b"<html><p>Sign in to access this article</p></html>",
        content_type="text/html", source_url=XML_URL, retrieved_at=xml["retrieved_at"],
        source_version="injected-login-v1", rights_status="unknown",
        rights_basis="Synthetic access-page failure", declared_surface="full_text", expected_doi=DOI_MAIN,
    )
    if login["classification"] != "access_page" or login["copy_ref"] is not None:
        raise RuntimeError("Access-page failure was not isolated")
    cases.append({"level": "advanced", "case": "injected_login", "source": "simulated_failure",
                  "expected": "access page, no passage", "observed": login})

    # Long real XML, alternate real HTML, a corrupted branch and a store reopen
    # exercise usable partial progress. No model/report citation is asserted.
    reopened = IntakeService(EvidenceLibrary(StateStore(root / "state")))
    resolved = reopened.source_for_model(xml_saved["passage_refs"][0])
    if resolved["text"] != selected_xml:
        raise RuntimeError("Exact XML passage failed to replay after store reopen")
    cases.append({"level": "complex", "case": "mixed_format_replay", "source": "live_plus_replay",
                  "expected": "exact XML passage survives retry/reopen alongside independent PDF and failed branch",
                  "observed_passage_ref": resolved["passage_ref"],
                  "observed_copy_ref": resolved["copy_ref"], "html_classification": html_saved["classification"]})
    if store.get(**abstract_saved["copy_ref"])["body"]["content_sha256"] != sha(abstract):
        raise RuntimeError("Usable abstract was lost during mixed-format work")
    cases.append({"level": "complex", "case": "independent_branch_survival", "source": "saved_real_data_replay",
                  "expected": "abstract and XML remain exact after wrong article, access and parser failure",
                  "observed_copy_ref": abstract_saved["copy_ref"],
                  "observed_quality_ref": abstract_saved["quality_ref"]})

    receipt = {
        "schema": "ora.stage4.intake_qualification.v1", "run_at": stamp,
        "proof_class": "live retrieval with deterministic processing; controlled failure; saved replay",
        "question": "What can a digital CBT article's published abstract and deeper open copy actually support?",
        "requests": requests, "cases": cases, "model_calls": 0, "model_tokens": 0,
        "cost": "not billed by these public article APIs", "state_path": "state/",
        "scope": "Intake/extraction only; no live mission, selected-model output or report citation",
    }
    (root / "receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    print(root / "receipt.json")
    print("cases", len(cases), "public requests", len(requests), "model calls", 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
