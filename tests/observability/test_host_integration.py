"""Mission acceptance and shutdown remain independent of optional monitoring."""

from pathlib import Path

from ora.contracts.models import LiveStartRequest
from ora.mission import MissionService
from ora.observability import runtime
from ora.observability.export import ExportConfig

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ExportConfig(enabled=True, endpoint="https://api.smith.langchain.com/otel/v1/traces",
                      api_key="synthetic-key", project="synthetic-project")


class Observer:
    instances = []

    def __init__(self, store, config):
        self.registrations = []
        self.closed = False
        self.instances.append(self)

    def register(self, run_id, *, newly_created):
        self.registrations.append((run_id, newly_created))

    def close(self, *, timeout_seconds):
        assert timeout_seconds <= 5
        self.closed = True
        return True

    def status(self):
        return {"enabled": True, "closed": self.closed}


def test_new_run_and_replay_register_without_a_model_call(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "RuntimeObserver", Observer)
    host = MissionService(ROOT, tmp_path, execution_mode="live", start_supervisor=False,
                          trace_config=CONFIG)
    try:
        request = LiveStartRequest(request_id="synthetic-trace-start", question="Compare notes",
                                   agent_ids=["agent-synthesis"], credential_profile="primary")
        first = host.start(request)
        replay = host.start(request)
        observer = Observer.instances[-1]
        assert observer.registrations == [(first.run_id, True), (replay.run_id, False)]
        assert not host.store.records("model_dispatch_attempt")
    finally:
        host.close()
    assert observer.closed


def test_offline_host_never_starts_enabled_exporter(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Offline host started monitoring")

    monkeypatch.setattr(runtime, "RuntimeObserver", forbidden)
    host = MissionService(ROOT, tmp_path, execution_mode="offline_test",
                          start_supervisor=False, trace_config=CONFIG)
    assert host.tracing_status() == {"enabled": False, "error_code": None}
    host.close()


def test_monitoring_failure_does_not_reject_accepted_run(tmp_path, monkeypatch):
    class FailingObserver(Observer):
        def register(self, *_args, **_kwargs):
            raise RuntimeError("synthetic private text must not escape")

    monkeypatch.setattr(runtime, "RuntimeObserver", FailingObserver)
    host = MissionService(ROOT, tmp_path, execution_mode="live", start_supervisor=False,
                          trace_config=CONFIG)
    try:
        receipt = host.start(LiveStartRequest(request_id="synthetic-observer-error",
                                             question="Compare notes",
                                             agent_ids=["agent-synthesis"]))
        assert host.store.get("mission_run", receipt.run_id)
        status = host.tracing_status()
        assert status["host_error_code"] == "observer_registration_failed"
        assert "private text" not in str(status)
    finally:
        host.close()
