"""Fresh PDF order improvements leave historical source coordinates intact."""

import shutil

import pytest

from ora.evidence.intake import (
    PARSER_VERSION,
    PDF_PARSER_VERSION,
    extract_blocks,
    extract_copy_blocks,
)
from ora.evidence.library import EvidenceError
from tests.evidence.test_source_format_walkthroughs import _pdf

pytestmark = pytest.mark.skipif(not shutil.which("pdftotext"), reason="Poppler unavailable")


def test_historical_pdf_mapping_replays_layout_while_fresh_copy_reorders_columns():
    content = _pdf([[
        (38, 690, "LEFT FIRST: demand fell by 18 percent."),
        (320, 690, "RIGHT FIRST: winter was an exception."),
        (38, 665, "LEFT SECOND: the gain lasted all year."),
        (320, 665, "RIGHT SECOND: weekends were excluded."),
    ]])
    historical = {"mime_type": "application/pdf", "mapping_version":
                  f"{PARSER_VERSION}:poppler-pdftotext-layout:partial_xml=False"}
    old, old_version = extract_copy_blocks(content, historical)
    fresh, current_version = extract_blocks(content, "application/pdf")
    assert old_version == "poppler-pdftotext-layout"
    assert current_version == PDF_PARSER_VERSION
    assert old[0].text.index("RIGHT FIRST") < old[0].text.index("LEFT SECOND")
    assert fresh[0].text.index("LEFT SECOND") < fresh[0].text.index("RIGHT FIRST")
    assert old[0].locator == "page=1; layout-text"
    assert fresh[0].locator == "page=1; reading-order-text"
    assert extract_copy_blocks(content, historical) == (old, old_version)
    assert extract_copy_blocks(content, {"mime_type": "application/pdf"}) == (old, old_version)
    retained_new = {**historical, "mapping_version":
                    f"{PARSER_VERSION}:{current_version}:partial_xml=False"}
    assert extract_copy_blocks(content, retained_new) == (fresh, current_version)


def test_unknown_retained_pdf_mapping_is_not_silently_reinterpreted():
    with pytest.raises(EvidenceError, match="Unsupported retained PDF"):
        extract_copy_blocks(b"unneeded", {
            "mime_type": "application/pdf",
            "mapping_version": f"{PARSER_VERSION}:unavailable-future-reader:partial_xml=False",
        })
