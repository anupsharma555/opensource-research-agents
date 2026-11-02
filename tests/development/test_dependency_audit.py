"""Advisory results must not confuse unavailable scanners with clear checks."""

from scripts.dependency_audit import outcome


def test_errors_and_skipped_packages_are_incomplete():
    assert outcome("javascript", 1, '{"error":{"code":"ENOTFOUND"}}')["status"] == "incomplete"
    assert outcome("rust", 1, "connection failed")["status"] == "incomplete"
    assert outcome("python", 0, '{"dependencies":[{"name":"synthetic","skip_reason":"unknown"}]}')["status"] == "incomplete"


def test_completed_scans_distinguish_advisories_from_clear_results():
    assert outcome("python", 1, '{"dependencies":[{"vulns":[{"id":"synthetic"}]}]}') == {"status": "findings", "advisories": 1}
    assert outcome("rust", 0, '{"vulnerabilities":{"count":0}}') == {"status": "clear", "advisories": 0}
    assert outcome("javascript", 1, '{"metadata":{"vulnerabilities":{"total":2}}}') == {"status": "findings", "advisories": 2}


def test_rust_warnings_are_reported_even_when_the_scanner_exits_zero():
    result = outcome("rust", 0, '{"vulnerabilities":{"count":0},"warnings":{"unsound":[{}]}}')
    assert result == {"status": "warnings", "advisories": 0, "warnings": {"unsound": 1}}
