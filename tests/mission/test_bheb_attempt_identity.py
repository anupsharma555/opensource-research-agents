"""Host retry generations cannot turn explicit refresh into uncertain HTTP replay."""

from types import SimpleNamespace

import pytest

from ora.mission.bheb_tools import search_bheb
from ora.retrieval.bheb import BhebCorpusService
from ora.retrieval.providers import HTTPResult
from ora.state import StateStore
from tests.retrieval.test_bheb import _fixture, _response


@pytest.mark.parametrize("first_outcome", ["completed", "uncertain", "waiting"])
def test_only_known_rate_wait_can_start_new_host_attempt(tmp_path, monkeypatch, first_outcome):
    config, attrs, _ = _fixture(tmp_path, monkeypatch, source=False)
    now = [1000.0]
    state = StateStore(tmp_path / "state", clock=lambda: now[0])
    sent = []

    def transport(*args, **kwargs):
        sent.append(1)
        if len(sent) == 1 and first_outcome == "uncertain":
            raise TimeoutError("synthetic unknown")
        if len(sent) == 1 and first_outcome == "waiting":
            return HTTPResult(429, b"{}", {"retry-after": "1"})
        return _response(attrs)

    bheb = BhebCorpusService(config, state, transport=transport)
    generation = [1]
    host = SimpleNamespace(
        bheb=bheb, store=state,
        _run=lambda _: {"body": {"bheb_corpus_enabled": True,
                                "bheb_config_digest": bheb.summary()["config_digest"]}},
        capacity=SimpleNamespace(get=lambda _: {"generation": generation[0]}),
        executor=SimpleNamespace(_capacity_id=lambda _: "synthetic-capacity"),
    )
    spec = SimpleNamespace(work_id="synthetic-work", mission_id="m", run_id="r",
                           actor_id="a", proposal_ref={"id": "t"})
    first = search_bheb(host, spec, {"query": "fictional evidence", "refresh": True})
    assert first["status"] == first_outcome
    generation[0] = 2
    now[0] += 2
    second = search_bheb(host, spec, {"query": "fictional evidence", "refresh": True})
    if first_outcome == "waiting":
        assert second["status"] == "completed" and len(sent) == 2
        assert first["operation_id"] != second["operation_id"]
    else:
        assert len(sent) == 1
        assert second["operation_id"] == first["operation_id"]
        assert second["status"] == first_outcome
