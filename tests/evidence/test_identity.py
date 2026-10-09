"""Identifier normalization is conservative and independent of source titles."""

import pytest

from ora.evidence.identity import IdentityError, normalize_identifier


@pytest.mark.parametrize(
    ("kind", "variant", "expected"),
    [
        ("doi", "https://doi.org/10.1136/BMJ.H5627", "10.1136/bmj.h5627"),
        ("doi", "doi:10.1136/bmj.h5627", "10.1136/bmj.h5627"),
        ("pmid", "https://pubmed.ncbi.nlm.nih.gov/26559241/", "26559241"),
        ("pmcid", "https://pmc.ncbi.nlm.nih.gov/articles/PMC4641883/", "PMC4641883"),
        ("arxiv", "https://arxiv.org/abs/2401.12345v2", "2401.12345"),
        ("arxiv", "arXiv:hep-th/9901001v3", "hep-th/9901001"),
        ("registry", "https://www.isrctn.com/ISRCTN91947481", "ISRCTN91947481"),
        ("registry", "https://clinicaltrials.gov/study/NCT12345678", "NCT12345678"),
        ("dataset_doi", "https://doi.org/10.5061/DRYAD.TEST", "10.5061/dryad.test"),
    ],
)
def test_normalizes_known_resolver_variants(kind, variant, expected):
    assert normalize_identifier(kind, variant) == expected


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("doi", "A similar title"),
        ("doi", "https://example.org/10.1136/bmj.h5627"),
        ("pmid", "26559241a"),
        ("registry", "ISRCTN9194748"),
        ("arxiv", "2401.123"),
    ],
)
def test_rejects_unverified_or_malformed_identity(kind, value):
    with pytest.raises(IdentityError):
        normalize_identifier(kind, value)
