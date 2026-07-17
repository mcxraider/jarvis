from agents.agent_api.app.config import load_settings


def test_health_limit_defaults_match_timeout_contract(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_RUN_DEADLINE_SECONDS", raising=False)

    settings = load_settings()

    assert settings.run_deadline_seconds == 150.0
    assert settings.model_router_complex_timeout_seconds == 90.0
