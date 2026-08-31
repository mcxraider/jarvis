"""Validated provider configuration and immutable role-profile tests."""

from dataclasses import FrozenInstanceError

import pytest

from agents.agent_api.app.config import (
    ORCHESTRATOR_LLM,
    ROUTER_LLM,
    SUMMARIZER_LLM,
    load_settings,
    settings as process_settings,
)
from agents.agent_api.app.llm.provider import (
    LLMProvider,
    LLMProviderError,
    OpenAIChatProfile,
    OpenAIResponsesProfile,
    validate_model_for_profile,
    validate_reasoning_for_profile,
)


def _clean_llm_env(monkeypatch) -> None:
    for name in (
        "LLM_PROVIDER",
        "ROUTER_PROVIDER",
        "SUMMARIZER_PROVIDER",
        "ROUTER_ENABLED",
        "TOOL_SELECTOR",
        "ROUTER_MODEL",
        "JARVIS_SUMMARIZER_MODEL",
        "MODEL_ROUTER_DEFAULT_MODEL",
        "MODEL_ROUTER_COMPLEX_MODEL",
        "MODEL_ROUTER_DEFAULT_REASONING",
        "MODEL_ROUTER_SIMPLE_REASONING",
        "MODEL_ROUTER_COMPLEX_REASONING",
        "MODEL_ROUTER_MULTI_DOMAIN_REASONING",
        "OPENAI_COMPLEX_MODEL",
        "OPENAI_REASONING_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("LLM_SAFETY_IDENTIFIER_SECRET", "test-safety-secret")


def _enable_openai(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("LLM_SAFETY_IDENTIFIER_SECRET", "test-safety-secret")


def test_openai_luna_medium_is_default_and_profiles_are_frozen(monkeypatch):
    _clean_llm_env(monkeypatch)
    configured = load_settings()

    assert configured.llm_provider is LLMProvider.OPENAI
    assert isinstance(configured.orchestrator_llm, OpenAIResponsesProfile)
    assert isinstance(configured.router_llm, OpenAIChatProfile)
    assert isinstance(configured.summarizer_llm, OpenAIChatProfile)
    assert configured.orchestrator_llm.model == "gpt-5.6-luna"
    assert configured.orchestrator_llm.reasoning_effort == "medium"
    assert configured.router_llm.reasoning_effort == "none"
    assert configured.summarizer_llm.reasoning_effort == "none"
    assert configured.model_router_default_model == "gpt-5.6-luna"
    assert configured.model_router_complex_model == "gpt-5.6-luna"
    assert configured.model_router_default_reasoning == "medium"
    assert configured.model_router_simple_reasoning == "low"
    assert configured.model_router_complex_reasoning == "medium"
    assert configured.model_router_multi_domain_reasoning == "medium"
    assert "test-openai-key" not in repr(configured.orchestrator_llm)
    with pytest.raises(FrozenInstanceError):
        configured.orchestrator_llm.model = "gpt-5.6-sol"  # type: ignore[misc]


def test_deepseek_remains_an_explicit_rollback(monkeypatch):
    _clean_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")

    configured = load_settings()

    assert configured.llm_provider is LLMProvider.DEEPSEEK
    assert configured.orchestrator_llm.model == "deepseek-v4-flash"
    assert configured.router_llm.provider is LLMProvider.DEEPSEEK
    assert configured.summarizer_llm.provider is LLMProvider.DEEPSEEK


def test_process_role_aliases_resolve_from_validated_settings():
    assert ORCHESTRATOR_LLM is process_settings.orchestrator_llm
    assert ROUTER_LLM is process_settings.router_llm
    assert SUMMARIZER_LLM is process_settings.summarizer_llm


@pytest.mark.parametrize("raw", [" OPENAI ", "OpenAI", "openai"])
def test_provider_parsing_normalizes_case_and_whitespace(monkeypatch, raw):
    _clean_llm_env(monkeypatch)
    _enable_openai(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", raw)

    configured = load_settings()

    assert isinstance(configured.orchestrator_llm, OpenAIResponsesProfile)
    assert isinstance(configured.router_llm, OpenAIChatProfile)
    assert isinstance(configured.summarizer_llm, OpenAIChatProfile)
    assert configured.orchestrator_llm.model == "gpt-5.6-luna"
    assert configured.model_router_complex_model == "gpt-5.6-luna"
    assert configured.model_router_default_reasoning == "medium"
    assert configured.model_router_simple_reasoning == "low"


@pytest.mark.parametrize("raw", ["", "   ", "anthropic"])
def test_unknown_or_empty_provider_fails(monkeypatch, raw):
    _clean_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", raw)
    with pytest.raises(LLMProviderError, match="LLM_PROVIDER"):
        load_settings()


def test_only_selected_provider_key_is_required(monkeypatch):
    _clean_llm_env(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMProviderError, match="OPENAI_API_KEY is required"):
        load_settings()

    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(LLMProviderError, match="DEEPSEEK_API_KEY is required"):
        load_settings()


@pytest.mark.parametrize("secret", [None, "", "   "])
def test_openai_requires_non_empty_safety_secret(monkeypatch, secret):
    _clean_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    if secret is None:
        monkeypatch.delenv("LLM_SAFETY_IDENTIFIER_SECRET", raising=False)
    else:
        monkeypatch.setenv("LLM_SAFETY_IDENTIFIER_SECRET", secret)
    with pytest.raises(LLMProviderError, match="LLM_SAFETY_IDENTIFIER_SECRET"):
        load_settings()


@pytest.mark.parametrize(
    ("global_provider", "router_override", "summarizer_override"),
    [
        (global_provider, router_override, summarizer_override)
        for global_provider in ("deepseek", "openai")
        for router_override in (None, "deepseek", "openai")
        for summarizer_override in (None, "deepseek", "openai")
    ],
)
def test_role_provider_inheritance_matrix(
    monkeypatch,
    global_provider,
    router_override,
    summarizer_override,
):
    _clean_llm_env(monkeypatch)
    _enable_openai(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", global_provider)
    if router_override is not None:
        monkeypatch.setenv("ROUTER_PROVIDER", router_override)
    if summarizer_override is not None:
        monkeypatch.setenv("SUMMARIZER_PROVIDER", summarizer_override)

    configured = load_settings()

    assert configured.orchestrator_llm.provider.value == global_provider
    assert configured.router_llm.provider.value == (router_override or global_provider)
    assert configured.summarizer_llm.provider.value == (
        summarizer_override or global_provider
    )


def test_role_override_requires_its_provider_key_and_secret(monkeypatch):
    _clean_llm_env(monkeypatch)
    monkeypatch.setenv("ROUTER_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMProviderError, match="OPENAI_API_KEY is required"):
        load_settings()


@pytest.mark.parametrize(
    ("router_enabled", "tool_selector"),
    [("false", "router"), ("true", "static")],
)
def test_inactive_router_override_does_not_require_unused_provider(
    monkeypatch, router_enabled, tool_selector
):
    _clean_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("ROUTER_PROVIDER", "openai")
    monkeypatch.setenv("ROUTER_ENABLED", router_enabled)
    monkeypatch.setenv("TOOL_SELECTOR", tool_selector)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_SAFETY_IDENTIFIER_SECRET", raising=False)
    monkeypatch.setenv("ROUTER_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("ROUTER_REASONING_EFFORT", "none")

    configured = load_settings()

    assert configured.router_llm.provider is LLMProvider.DEEPSEEK


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DEEPSEEK_MAX_TOKENS", "0"),
        ("DEEPSEEK_MAX_RETRY_ATTEMPTS", "-1"),
        ("OPENAI_REQUEST_TIMEOUT_SECONDS", "nan"),
        ("OPENAI_RETRY_MAX_DELAY_SECONDS", "inf"),
    ],
)
def test_numeric_limits_must_be_positive_and_finite(monkeypatch, name, value):
    _clean_llm_env(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises((ValueError, LLMProviderError)):
        load_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DEEPSEEK_MODEL", "  "),
        ("OPENAI_MODEL", ""),
        ("ROUTER_MODEL", " "),
        ("JARVIS_SUMMARIZER_MODEL", " "),
    ],
)
def test_model_names_must_not_be_empty(monkeypatch, name, value):
    _clean_llm_env(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(LLMProviderError, match=name):
        load_settings()


def test_model_and_reasoning_pins_are_provider_checked(monkeypatch):
    _clean_llm_env(monkeypatch)
    _enable_openai(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    configured = load_settings()
    profile = configured.orchestrator_llm

    assert validate_model_for_profile(profile, "  gpt-5.6-sol ") == "gpt-5.6-sol"
    assert validate_reasoning_for_profile(profile, None) == "medium"
    assert validate_reasoning_for_profile(profile, "xhigh") == "xhigh"
    with pytest.raises(LLMProviderError, match="incompatible"):
        validate_model_for_profile(profile, "deepseek-v4-pro")
    with pytest.raises(LLMProviderError, match="must be one of"):
        validate_reasoning_for_profile(profile, "off")
    with pytest.raises(LLMProviderError, match="requires a GPT-5.6 model"):
        validate_model_for_profile(profile, "gpt-5.4")


def test_openai_router_rejects_deepseek_reasoning_override(monkeypatch):
    _clean_llm_env(monkeypatch)
    _enable_openai(monkeypatch)
    monkeypatch.setenv("ROUTER_PROVIDER", "openai")
    monkeypatch.setenv("ROUTER_REASONING_EFFORT", "high")

    with pytest.raises(LLMProviderError, match="must be 'none'"):
        load_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ROUTER_MODEL", "deepseek-v4-flash"),
        ("JARVIS_SUMMARIZER_MODEL", "deepseek-v4-flash"),
        ("MODEL_ROUTER_DEFAULT_MODEL", "deepseek-v4-flash"),
        ("MODEL_ROUTER_COMPLEX_MODEL", "deepseek-v4-pro"),
    ],
)
def test_openai_rejects_foreign_models_in_every_role(monkeypatch, name, value):
    _clean_llm_env(monkeypatch)
    _enable_openai(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv(name, value)

    with pytest.raises(LLMProviderError, match="incompatible"):
        load_settings()


@pytest.mark.parametrize(
    "name",
    [
        "MODEL_ROUTER_DEFAULT_REASONING",
        "MODEL_ROUTER_SIMPLE_REASONING",
        "MODEL_ROUTER_COMPLEX_REASONING",
        "MODEL_ROUTER_MULTI_DOMAIN_REASONING",
    ],
)
def test_openai_responses_accepts_reasoning_model_router_overrides(monkeypatch, name):
    _clean_llm_env(monkeypatch)
    _enable_openai(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv(name, "high")

    configured = load_settings()
    field_name = name.lower()
    assert getattr(configured, field_name) == "high"


def test_openai_responses_rejects_off_reasoning(monkeypatch):
    _clean_llm_env(monkeypatch)
    _enable_openai(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "off")

    with pytest.raises(LLMProviderError, match="must be one of"):
        load_settings()
