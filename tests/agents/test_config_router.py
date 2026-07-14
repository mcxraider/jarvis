"""Focused settings tests for router defaults."""

from agents.agent_api.app.config import load_settings


def test_router_timeout_default_is_five_seconds(monkeypatch):
    monkeypatch.delenv("ROUTER_REQUEST_TIMEOUT_SECONDS", raising=False)
    settings = load_settings()
    assert settings.router_request_timeout_seconds == 5.0


def test_model_router_default_reasoning_is_high(monkeypatch):
    monkeypatch.delenv("MODEL_ROUTER_DEFAULT_REASONING", raising=False)
    settings = load_settings()
    assert settings.model_router_default_reasoning == "high"
