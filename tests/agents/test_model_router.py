"""Unit tests for the model router (per-turn model + reasoning selection)."""

import os

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

import pytest

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
