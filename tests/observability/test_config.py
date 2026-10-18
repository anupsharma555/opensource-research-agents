"""Runtime tracing needs explicit local opt-in and fails independently of research."""

from ora.observability.config import TRACE_ENV_KEYS, runtime_export_config, trace_environment


def test_runtime_requires_its_own_switch_and_process_values_win(tmp_path, monkeypatch):
    for key in TRACE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "LANGSMITH_API_KEY=synthetic-key\nOSRA_TRACE_EXPORT_ENABLED=true\n"
        "OSRA_TRACE_OTLP_TRACES_ENDPOINT=https://api.smith.langchain.com/otel/v1/traces\n"
        "OSRA_TRACE_LANGSMITH_PROJECT=synthetic-project\nUNRELATED_SECRET=never-loaded\n"
    )
    assert not runtime_export_config(tmp_path).enabled
    assert "UNRELATED_SECRET" not in trace_environment(tmp_path)
    monkeypatch.setenv("OSRA_TRACE_RUNTIME_ENABLED", "true")
    assert runtime_export_config(tmp_path).enabled
    monkeypatch.setenv("OSRA_TRACE_EXPORT_ENABLED", "false")
    assert not runtime_export_config(tmp_path).enabled


def test_bad_monitoring_configuration_never_blocks_research_or_leaks(tmp_path, monkeypatch, caplog):
    for key in TRACE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "OSRA_TRACE_RUNTIME_ENABLED=true\nOSRA_TRACE_EXPORT_ENABLED=true\n"
        "LANGSMITH_API_KEY=synthetic-private-key\n"
        "OSRA_TRACE_OTLP_TRACES_ENDPOINT=https://bad.example/secret-path\n"
        "OSRA_TRACE_LANGSMITH_PROJECT=private-project\n"
    )
    assert not runtime_export_config(tmp_path).enabled
    assert "configuration_error" in caplog.text
    assert "synthetic-private" not in caplog.text
    assert "secret-path" not in caplog.text
