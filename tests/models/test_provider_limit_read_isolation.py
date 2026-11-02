"""Cached limit reads preserve the bytes of a stopped source state."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3

import pytest

from ora.models import provider_limits
from ora.models.provider_limits import ProviderLimitCache
from tests.models.test_provider_limits import _receipt


def _tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _cold_stopped_copy(tmp_path, source, cache):
    """A coherent synthetic stopped DB whose next read would create WAL sidecars."""
    stopped = tmp_path / "stopped-source"
    shutil.copytree(source.root, stopped)
    wal = stopped / "state.sqlite3-wal"
    if wal.exists():
        assert wal.stat().st_size == 0
        wal.unlink()
    shm = stopped / "state.sqlite3-shm"
    if shm.exists():
        shm.unlink()
    with sqlite3.connect(cache.path) as db:
        for table in ("observations", "input_observations"):
            db.execute(
                f"UPDATE {table} SET source_state_path=? WHERE source_state_path=?",
                (str(stopped.resolve()), str(source.root)),
            )
    assert not wal.exists() and not shm.exists()
    return stopped


@pytest.mark.parametrize("input_limit", [None, 7000])
def test_latest_revalidates_cold_stopped_source_without_touching_it(tmp_path, input_limit):
    kwargs = ({"input_limit": input_limit, "input_requested": 7030}
              if input_limit is not None else {})
    source, receipt, scope = _receipt(tmp_path / "source", **kwargs)
    cache = ProviderLimitCache(tmp_path / "cache")
    assert cache.record(source, receipt)["observed_limit_tokens"] == 8000
    stopped = _cold_stopped_copy(tmp_path, source, cache)
    before = _tree_digest(stopped)
    hint = cache.latest(scope)
    assert hint is not None
    assert hint["source_state_path"] == str(stopped.resolve())
    assert hint["receipt_ref"] == receipt
    assert hint["observed_limit_tokens"] == 8000
    if input_limit is not None:
        assert hint["observed_input_limit_tokens"] == input_limit
        assert hint["input_limit_source_state_path"] == str(stopped.resolve())
    assert _tree_digest(stopped) == before
    assert not (stopped / "state.sqlite3-wal").exists()
    assert not (stopped / "state.sqlite3-shm").exists()


@pytest.mark.parametrize("tamper", ["missing_blob", "changed_blob"])
def test_latest_still_rejects_missing_or_tampered_request_blob(tmp_path, tamper):
    source, receipt, scope = _receipt(tmp_path / "source")
    cache = ProviderLimitCache(tmp_path / "cache")
    assert cache.record(source, receipt) is not None
    stopped = _cold_stopped_copy(tmp_path, source, cache)
    intent = source.get("model_request_intent", "intent-one")
    blob = stopped / "objects" / intent["body"]["blob"]["sha256"]
    if tamper == "missing_blob":
        blob.unlink()
    else:
        blob.write_bytes(b"changed synthetic request")
    before = _tree_digest(stopped)
    assert cache.latest(scope) is None
    assert _tree_digest(stopped) == before


def test_new_success_promotes_old_input_hint_without_opening_stopped_source(tmp_path):
    old, old_ref, scope = _receipt(
        tmp_path / "old", input_limit=7000, input_requested=7030,
    )
    cache = ProviderLimitCache(tmp_path / "cache")
    assert cache.record(old, old_ref)["observed_input_limit_tokens"] == 7000
    stopped = _cold_stopped_copy(tmp_path, old, cache)
    before = _tree_digest(stopped)
    newer, newer_ref, _ = _receipt(
        tmp_path / "newer", status="complete", http_status=200,
    )
    result = cache.record(newer, newer_ref)
    assert result["receipt_ref"] == newer_ref
    assert result["input_limit_receipt_ref"] == old_ref
    assert result["observed_input_limit_tokens"] == 7000
    assert _tree_digest(stopped) == before


def test_source_write_during_snapshot_fails_closed_then_revalidates(tmp_path, monkeypatch):
    source, receipt, scope = _receipt(tmp_path / "source")
    cache = ProviderLimitCache(tmp_path / "cache")
    assert cache.record(source, receipt) is not None
    stopped = _cold_stopped_copy(tmp_path, source, cache)
    copyfile = shutil.copyfile
    writes = []

    def write_while_copying(source_path, target_path, *args, **kwargs):
        result = copyfile(source_path, target_path, *args, **kwargs)
        if source_path == stopped / "state.sqlite3" and not writes:
            with sqlite3.connect(stopped / "state.sqlite3") as db:
                db.execute("CREATE TABLE synthetic_concurrent_write (value INTEGER)")
            writes.append(True)
        return result

    monkeypatch.setattr(provider_limits.shutil, "copyfile", write_while_copying)
    assert cache.latest(scope) is None
    assert writes == [True]
    monkeypatch.setattr(provider_limits.shutil, "copyfile", copyfile)
    assert cache.latest(scope)["receipt_ref"] == receipt
