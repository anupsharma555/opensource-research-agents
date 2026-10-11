"""Keep offline tests independent of the developer's private credential controls."""

from pathlib import Path

import pytest

from ora.credentials import GroqCredentialProfiles


@pytest.fixture(autouse=True)
def isolate_repository_credential_profiles(monkeypatch, tmp_path):
    """Use empty fixture settings only for the real checkout's private mapping.

    Tests that explicitly create a mapping under their own temporary root still
    exercise the actual parser and enabled/domain behavior.
    """
    repository = Path(__file__).resolve().parents[1]
    original = GroqCredentialProfiles.__init__
    fixture_root = tmp_path / "private-profile-fixture"

    def initialize(instance, root):
        config_root = fixture_root if Path(root).resolve() == repository else root
        original(instance, config_root)

    monkeypatch.setattr(GroqCredentialProfiles, "__init__", initialize)
