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
        decision = RouterDecision(domains=["todoist", "google_calendar"], uncertain=True)
        result = router.select(decision)
        assert result == router.default

    def test_returns_default_when_decision_is_none(self):
        router = create_default_model_router()
        result = router.select(None)
        assert result == router.default

    def test_uncertain_takes_priority_over_multi_domain(self):
        router = create_default_model_router(
            complex_model="pro",
            complex_reasoning="max",
            multi_domain_reasoning="high",
        )
        decision = RouterDecision(
            domains=["todoist", "google_calendar"],
            uncertain=True,
        )
        result = router.select(decision)
        assert result.model == "pro"
        assert result.reasoning_effort == "max"

    def test_multi_domain_fires_when_not_uncertain(self):
        router = create_default_model_router(
            complex_model="pro",
            multi_domain_reasoning="high",
        )
        decision = RouterDecision(domains=["todoist", "google_calendar"])
        result = router.select(decision)
        assert result.model == "pro"
        assert result.reasoning_effort == "high"

    def test_single_domain_uses_default(self):
        router = create_default_model_router(
            default_model="flash",
            default_reasoning="max",
            complex_model="pro",
        )
        decision = RouterDecision(domains=["todoist"])
        result = router.select(decision)
        assert result.model == "flash"
        assert result.reasoning_effort == "max"

    def test_empty_domains_uses_default(self):
        router = create_default_model_router(default_model="flash")
        decision = RouterDecision(domains=[])
        result = router.select(decision)
        assert result.model == "flash"

    def test_custom_rules(self):
        custom_selection = ModelSelection(model="custom", reasoning_effort="low")
        custom_rule = ModelRoutingRule(
            name="always_custom",
            condition=lambda d: True,
            selection=custom_selection,
        )
        default = ModelSelection(model="default", reasoning_effort="max")
        router = ModelRouter([custom_rule], default)
        decision = RouterDecision(domains=["todoist"])
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
        assert router.select(RouterDecision(domains=["x"])) == sel_a
