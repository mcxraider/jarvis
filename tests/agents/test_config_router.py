"""Focused settings tests for router defaults."""

import pytest

from agents.agent_api.app.config import load_settings


def test_router_timeout_default_is_five_seconds(monkeypatch):
    monkeypatch.delenv("ROUTER_REQUEST_TIMEOUT_SECONDS", raising=False)
    settings = load_settings()
    assert settings.router_request_timeout_seconds == 5.0


def test_model_router_default_reasoning_is_medium(monkeypatch):
    monkeypatch.delenv("MODEL_ROUTER_DEFAULT_REASONING", raising=False)
    monkeypatch.delenv("MODEL_ROUTER_SIMPLE_REASONING", raising=False)
    settings = load_settings()
    assert settings.model_router_default_reasoning == "medium"
    assert settings.model_router_simple_reasoning == "low"


def test_model_router_timeout_defaults(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_REQUEST_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("MODEL_ROUTER_DEFAULT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("MODEL_ROUTER_MULTI_DOMAIN_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("MODEL_ROUTER_COMPLEX_TIMEOUT_SECONDS", raising=False)

    settings = load_settings()

    assert settings.model_router_default_timeout_seconds == 60.0
    assert settings.model_router_multi_domain_timeout_seconds == 60.0
    assert settings.model_router_complex_timeout_seconds == 90.0


def test_model_router_default_timeout_falls_back_to_deepseek_timeout(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_REQUEST_TIMEOUT_SECONDS", "45")
    monkeypatch.delenv("MODEL_ROUTER_DEFAULT_TIMEOUT_SECONDS", raising=False)

    settings = load_settings()

    assert settings.deepseek_request_timeout_seconds == 45.0
    assert settings.model_router_default_timeout_seconds == 45.0


def test_model_router_timeout_overrides(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_DEFAULT_TIMEOUT_SECONDS", "35")
    monkeypatch.setenv("MODEL_ROUTER_MULTI_DOMAIN_TIMEOUT_SECONDS", "65")
    monkeypatch.setenv("MODEL_ROUTER_COMPLEX_TIMEOUT_SECONDS", "95")

    settings = load_settings()

    assert settings.model_router_default_timeout_seconds == 35.0
    assert settings.model_router_multi_domain_timeout_seconds == 65.0
    assert settings.model_router_complex_timeout_seconds == 95.0


@pytest.mark.parametrize(
    "name",
    [
        "MODEL_ROUTER_DEFAULT_TIMEOUT_SECONDS",
        "MODEL_ROUTER_MULTI_DOMAIN_TIMEOUT_SECONDS",
        "MODEL_ROUTER_COMPLEX_TIMEOUT_SECONDS",
    ],
)
def test_model_router_timeouts_must_be_positive(monkeypatch, name):
    monkeypatch.setenv(name, "0")

    with pytest.raises(
        ValueError,
        match=f"{name} must be finite and greater than zero",
    ):
        load_settings()
