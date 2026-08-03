from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agents.agent_api.app.api.routes import health as health_module
from agents.agent_api.app.api.schemas import DetailedHealthResponse
from agents.agent_api.app.config import load_settings
from agents.agent_api.app.llm.provider import LLMProvider


def test_health_limit_defaults_match_timeout_contract(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_RUN_DEADLINE_SECONDS", raising=False)

    settings = load_settings()

    assert settings.run_deadline_seconds == 150.0
    assert settings.model_router_complex_timeout_seconds == 90.0


@pytest.mark.parametrize("provider", [LLMProvider.DEEPSEEK, LLMProvider.OPENAI])
def test_llm_probe_uses_selected_profile(monkeypatch, provider: LLMProvider) -> None:
    profile = SimpleNamespace(
        provider=provider,
        api_key="selected-key",
        base_url=(
            "https://api.deepseek.example"
            if provider is LLMProvider.DEEPSEEK
            else "https://api.openai.example/v1"
        ),
        model="deepseek-v4-flash" if provider is LLMProvider.DEEPSEEK else "gpt-5.6-luna",
        request_timeout_seconds=60.0,
    )
    sdk = MagicMock()
    openai = MagicMock(return_value=sdk)
    monkeypatch.setattr("openai.OpenAI", openai)

    assert health_module._check_llm(profile) == {"ok": True, "detail": "reachable"}
    openai.assert_called_once_with(
        api_key="selected-key",
        base_url=profile.base_url,
        timeout=5.0,
        max_retries=0,
    )
    sdk.models.list.assert_called_once_with()
    sdk.close.assert_called_once_with()


def test_llm_probe_reports_missing_selected_key_without_sdk_call(monkeypatch) -> None:
    profile = SimpleNamespace(
        api_key="",
        base_url="https://api.openai.example/v1",
        request_timeout_seconds=60.0,
    )
    openai = MagicMock()
    monkeypatch.setattr("openai.OpenAI", openai)

    assert health_module._check_llm(profile) == {"ok": False, "detail": "no API key"}
    openai.assert_not_called()


def test_health_detail_identifies_orchestrator_not_role_override(monkeypatch) -> None:
    orchestrator = SimpleNamespace(
        provider=LLMProvider.OPENAI,
        model="gpt-5.6-luna",
        api_key="orchestrator-key",
        base_url="https://api.openai.example/v1",
        request_timeout_seconds=60.0,
    )
    fake_settings = SimpleNamespace(
        orchestrator_llm=orchestrator,
        # A different router profile must not affect main LLM health identity.
        router_llm=SimpleNamespace(
            provider=LLMProvider.DEEPSEEK,
            model="deepseek-v4-flash",
        ),
        run_deadline_seconds=150.0,
        max_agent_turns=20,
        model_router_complex_timeout_seconds=90.0,
    )
    monkeypatch.setattr(health_module, "settings", fake_settings)
    monkeypatch.setattr(
        health_module,
        "_check_llm",
        MagicMock(return_value={"ok": True, "detail": "reachable"}),
    )
    monkeypatch.setattr(
        health_module,
        "_check_todoist",
        MagicMock(return_value={"ok": True, "detail": "2 project(s)"}),
    )
    monkeypatch.setattr(
        "agents.agent_api.app.run_logging.log_writer_healthy",
        lambda: True,
    )

    result = DetailedHealthResponse.model_validate(health_module.health_detail(123))
    assert result.status == "ok"
    assert result.provider == "openai"
    assert result.model == "gpt-5.6-luna"
    assert result.checks["llm"].ok is True
    assert result.limits.llm_request_timeout_seconds == 60.0
    health_module._check_llm.assert_called_once_with(orchestrator)
