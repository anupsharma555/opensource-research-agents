"""Guard the local stack comparison's safety and transport parity probes."""

from scripts.evaluate_stack_candidates import BYTE_LIMIT, evaluate_fts, evaluate_http


def test_fts_probe_preserves_current_version_permission_and_literal_query():
    result = evaluate_fts(filler_count=25, repetitions=2)
    cases = {case["probe"]: case for case in result["cases"]}

    assert result["fts5_compiled"]
    assert cases["phrase"]["current_guarded"] == [
        "phrase-adjacent", "phrase-separated"
    ]
    assert cases["phrase"]["fts_phrase_guarded"] == ["phrase-adjacent"]
    assert cases["negation"]["fts_phrase_guarded"] == ["negated"]
    assert cases["expanded_abbreviation"]["current_guarded"] == ["ptsd-expanded"]
    assert cases["expanded_abbreviation"]["fts_guarded"] == ["ptsd-expanded"]
    assert cases["numeric_unit"]["fts_phrase_guarded"] == ["dose-decimal"]
    assert cases["superseded_version"]["fts_raw"] == [["versioned", 1]]
    assert cases["superseded_version"]["fts_guarded"] == []
    assert cases["revoked_permission"]["fts_guarded"] == ["cytokine-allowed"]
    assert cases["fts_syntax_literal"]["fts_guarded"] == []


def test_httpx_adapter_probe_preserves_provider_response_classification():
    result = evaluate_http(repetitions=3)
    cases = {case["probe"]: case for case in result["cases"]}

    for name in (
        "success", "redirect", "non_2xx", "missing", "post", "byte_bound",
        "compressed_raw",
    ):
        assert cases[name]["urllib"] == cases[name]["httpx"]
    assert cases["redirect"]["httpx"]["status"] == 200
    assert cases["non_2xx"]["httpx"]["status"] == 503
    assert cases["byte_bound"]["httpx"]["body_bytes"] == BYTE_LIMIT
    assert cases["unknown_response"]["httpx"]["kind"] == "unknown_response"
    assert cases["unknown_response"]["urllib"]["kind"] == "unknown_response"
    assert result["urllib_repeated"]["connections"] == 3
    assert result["httpx_repeated"]["connections"] == 1
