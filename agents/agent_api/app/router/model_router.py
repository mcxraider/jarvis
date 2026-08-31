"""Model router: selects model + reasoning effort from fused routing signals.

Pure in-memory rule evaluation — zero network calls, microsecond latency. The router
evaluates a priority-ordered rule list against query complexity, domain uncertainty,
and domain count, then returns the first match. When disabled or when no
RouterDecision is available, returns the default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from agents.agent_api.app.llm.provider import (
    LLMProvider,
    LLMProviderProfile,
    validate_model_for_profile,
    validate_reasoning_for_profile,
)
from agents.agent_api.app.router.prompt import QueryComplexity, RouterDecision


@dataclass(frozen=True)
class ModelSelection:
    """The resolved model, reasoning effort, and optional timeout for one turn."""

    model: str
    reasoning_effort: str
    request_timeout_seconds: Optional[float] = None
    provider: Optional[LLMProvider] = None


@dataclass(frozen=True)
class ModelRoutingRule:
    """A single routing rule: if condition(decision) is True, use selection."""

    name: str
    condition: Callable[[RouterDecision], bool]
    selection: ModelSelection


class ModelRouter:
    """Priority-ordered rule-based model selector.

    Rules are evaluated top-down; first match wins. Falls back to *default*
    when no rule matches or when the router is disabled.
    """

    def __init__(
        self,
        rules: List[ModelRoutingRule],
        default: ModelSelection,
        *,
        enabled: bool = True,
    ):
        self._rules = rules
        self._default = default
        self._enabled = enabled

    @property
    def default(self) -> ModelSelection:
        return self._default

    def select(self, decision: Optional[RouterDecision]) -> ModelSelection:
        """Return the model selection for this turn.

        Returns the default when disabled, when *decision* is None (no router
        ran), or when no rule matches.
        """
        if not self._enabled or decision is None:
            return self._default
        for rule in self._rules:
            if rule.condition(decision):
                return rule.selection
        return self._default


def create_default_model_router(
    *,
    enabled: bool = True,
    default_model: str = "deepseek-v4-flash",
    default_reasoning: str = "high",
    default_timeout_seconds: float = 30.0,
    simple_reasoning: str = "low",
    complex_model: str = "deepseek-v4-pro",
    complex_reasoning: str = "max",
    complex_timeout_seconds: float = 90.0,
    multi_domain_reasoning: str = "high",
    multi_domain_timeout_seconds: float = 60.0,
    profile: Optional[LLMProviderProfile] = None,
) -> ModelRouter:
    """Build the standard model router from configuration values.

    Strongest-signal-wins priority (first match wins):
    1. uncertain or high complexity → complex model/reasoning/timeout
    2. empty domains → default selection
    3. multi-domain (>1) or medium complexity → complex model + multi-domain budget
    4. low complexity, certain, single domain → default model + simple reasoning
    """
    provider: Optional[LLMProvider] = None
    if profile is not None:
        validate_model_for_profile(profile, default_model)
        validate_model_for_profile(profile, complex_model)
        validate_reasoning_for_profile(profile, default_reasoning)
        validate_reasoning_for_profile(profile, simple_reasoning)
        validate_reasoning_for_profile(profile, complex_reasoning)
        validate_reasoning_for_profile(profile, multi_domain_reasoning)
        provider = profile.provider

    default = ModelSelection(
        model=default_model,
        reasoning_effort=default_reasoning,
        request_timeout_seconds=default_timeout_seconds,
        provider=provider,
    )

    rules: List[ModelRoutingRule] = [
        ModelRoutingRule(
            name="high_complexity_or_uncertain",
            condition=lambda d: (
                d.uncertain or d.complexity == QueryComplexity.HIGH
            ),
            selection=ModelSelection(
                model=complex_model,
                reasoning_effort=complex_reasoning,
                request_timeout_seconds=complex_timeout_seconds,
                provider=provider,
            ),
        ),
        ModelRoutingRule(
            name="empty_domains",
            condition=lambda d: not d.domains,
            selection=default,
        ),
        ModelRoutingRule(
            name="medium_complexity_or_multi_domain",
            condition=lambda d: (
                len(d.domains) > 1 or d.complexity == QueryComplexity.MEDIUM
            ),
            selection=ModelSelection(
                model=complex_model,
                reasoning_effort=multi_domain_reasoning,
                request_timeout_seconds=multi_domain_timeout_seconds,
                provider=provider,
            ),
        ),
        ModelRoutingRule(
            name="simple_certain_single_domain",
            condition=lambda d: (
                not d.uncertain
                and d.complexity == QueryComplexity.LOW
                and len(d.domains) == 1
            ),
            selection=ModelSelection(
                model=default_model,
                reasoning_effort=simple_reasoning,
                request_timeout_seconds=default_timeout_seconds,
                provider=provider,
            ),
        ),
    ]

    return ModelRouter(rules, default, enabled=enabled)
