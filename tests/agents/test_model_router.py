"""Unit tests for the model router (per-turn model + reasoning selection)."""

import os

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

import pytest

from agents.agent_api.app.llm.provider import (
    DeepSeekProfile,
    LLMProvider,
    LLMProviderError,
    OpenAIChatProfile,
    OpenAIResponsesProfile,
)
from agents.agent_api.app.router.model_router import (
    ModelRouter,
    ModelRoutingRule,
    ModelSelection,
    create_default_model_router,
)
from agents.agent_api.app.router.prompt import RouterDecision


class TestModelSelection:
    def test_frozen(self):
        sel = ModelSelection(model="m", reasoning_effort="high")
        with pytest.raises(Exception):
            sel.model = "other"


class TestModelRouter:
    def test_returns_default_when_disabled(self):
        router = create_default_model_router(enabled=False)
        decision = RouterDecision(outcome="routed", domains=["todoist", "google_calendar"], uncertain=True, candidate_domains=["todoist", "google_calendar"], complexity="low", reasoning="test")
        result = router.select(decision)
        assert result == router.default

    def test_returns_default_when_decision_is_none(self):
        router = create_default_model_router()
        result = router.select(None)
        assert result == router.default

    def test_uncertain_low_complexity_uses_complex_max(self):
        router = create_default_model_router(
            complex_model="pro",
            complex_reasoning="max",
            multi_domain_reasoning="high",
        )
        decision = RouterDecision(
            outcome="routed",
            domains=["todoist", "google_calendar"],
            uncertain=True,
            candidate_domains=["todoist", "google_calendar"],
            complexity="low",
            reasoning="test",
        )
        result = router.select(decision)
        assert result.model == "pro"
        assert result.reasoning_effort == "max"
        assert result.request_timeout_seconds == 90.0

    def test_low_complexity_multi_domain_uses_complex_high(self):
        router = create_default_model_router(
            complex_model="pro",
            multi_domain_reasoning="high",
        )
        decision = RouterDecision(outcome="routed", domains=["todoist", "google_calendar"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")
        result = router.select(decision)
        assert result.model == "pro"
        assert result.reasoning_effort == "high"
        assert result.request_timeout_seconds == 60.0

    def test_low_complexity_single_domain_uses_default_high(self):
        router = create_default_model_router(
            default_model="flash",
            default_reasoning="high",
            complex_model="pro",
        )
        decision = RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")
        result = router.select(decision)
        assert result.model == "flash"
        assert result.reasoning_effort == "high"
        assert result.request_timeout_seconds == 30.0

    def test_medium_complexity_single_domain_uses_complex_high(self):
        router = create_default_model_router(
            default_model="flash",
            complex_model="pro",
            multi_domain_reasoning="high",
        )
        decision = RouterDecision(
            outcome="routed",
            domains=["todoist"],
            uncertain=False,
            candidate_domains=[],
            complexity="medium",
            reasoning="test",
        )
        result = router.select(decision)
        assert result.model == "pro"
        assert result.reasoning_effort == "high"
        assert result.request_timeout_seconds == 60.0

    @pytest.mark.parametrize(
        ("domains", "uncertain", "candidate_domains"),
        [
            (["todoist"], False, []),
            (["todoist", "google_calendar"], False, []),
        ],
    )
    def test_high_complexity_always_uses_complex_max(
        self, domains, uncertain, candidate_domains
    ):
        router = create_default_model_router(
            default_model="flash",
            complex_model="pro",
            complex_reasoning="max",
        )
        decision = RouterDecision(
            outcome="routed",
            domains=domains,
            uncertain=uncertain,
            candidate_domains=candidate_domains,
            complexity="high",
            reasoning="test",
        )
        result = router.select(decision)
        assert result.model == "pro"
        assert result.reasoning_effort == "max"
        assert result.request_timeout_seconds == 90.0

    def test_empty_domains_uses_default(self):
        router = create_default_model_router(default_model="flash")
        decision = RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")
        result = router.select(decision)
        assert result.model == "flash"

    def test_custom_rules(self):
        custom_selection = ModelSelection(model="custom", reasoning_effort="low")
        assert custom_selection.request_timeout_seconds is None
        custom_rule = ModelRoutingRule(
            name="always_custom",
            condition=lambda d: True,
            selection=custom_selection,
        )
        default = ModelSelection(model="default", reasoning_effort="max")
        router = ModelRouter([custom_rule], default)
        decision = RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")
        assert router.select(decision) == custom_selection

    def test_first_matching_rule_wins(self):
        sel_a = ModelSelection(model="a", reasoning_effort="high")
        sel_b = ModelSelection(model="b", reasoning_effort="low")
        rules = [
            ModelRoutingRule(name="first", condition=lambda d: True, selection=sel_a),
            ModelRoutingRule(name="second", condition=lambda d: True, selection=sel_b),
        ]
        default = ModelSelection(model="default", reasoning_effort="max")
        router = ModelRouter(rules, default)
        assert router.select(RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")) == sel_a


def _profile(provider: LLMProvider):
    common = dict(
        api_key="test-key",
        base_url="https://example.test/v1",
        model=(
            "deepseek-v4-flash"
            if provider is LLMProvider.DEEPSEEK
            else "gpt-5.6-luna"
        ),
        max_output_tokens=100,
        request_timeout_seconds=30.0,
        max_retry_attempts=2,
        retry_max_delay_seconds=2.0,
        sdk_max_retries=0,
    )
    if provider is LLMProvider.DEEPSEEK:
        return DeepSeekProfile(**common, reasoning_effort="high", thinking_enabled=True)
    return OpenAIResponsesProfile(**common, reasoning_effort="medium")


def test_openai_responses_model_routes_preserve_configured_effort() -> None:
    router = create_default_model_router(
        profile=_profile(LLMProvider.OPENAI),
        default_model="gpt-5.6-luna",
        default_reasoning="high",
        complex_model="gpt-5.6-sol",
        complex_reasoning="max",
        multi_domain_reasoning="high",
    )
    assert router.default.provider is LLMProvider.OPENAI
    assert router.default.reasoning_effort == "high"
    decision = RouterDecision(
        outcome="routed",
        domains=["todoist"],
        uncertain=True,
        candidate_domains=["todoist"],
        complexity="high",
        reasoning="test",
    )
    assert router.select(decision).reasoning_effort == "max"
    assert router.select(decision).provider is LLMProvider.OPENAI


@pytest.mark.parametrize(
    ("provider", "foreign_model"),
    [
        (LLMProvider.OPENAI, "deepseek-v4-pro"),
        (LLMProvider.DEEPSEEK, "gpt-5.6-sol"),
    ],
)
def test_model_router_rejects_foreign_provider_models(
    provider: LLMProvider,
    foreign_model: str,
) -> None:
    valid_default = (
        "gpt-5.6-luna"
        if provider is LLMProvider.OPENAI
        else "deepseek-v4-flash"
    )
    with pytest.raises(LLMProviderError, match="incompatible"):
        create_default_model_router(
            profile=_profile(provider),
            default_model=valid_default,
            complex_model=foreign_model,
        )
