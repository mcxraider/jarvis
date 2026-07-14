"""Model router: selects model + reasoning effort from fused routing signals.

Pure in-memory rule evaluation — zero network calls, microsecond latency. The router
evaluates a priority-ordered rule list against query complexity, domain uncertainty,
and domain count, then returns the first match. When disabled or when no
RouterDecision is available, returns the default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from agents.agent_api.app.router.prompt import QueryComplexity, RouterDecision


@dataclass(frozen=True)
class ModelSelection:
    """The resolved model + reasoning effort for one orchestrator turn."""

    model: str
    reasoning_effort: str


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
    complex_model: str = "deepseek-v4-pro",
    complex_reasoning: str = "max",
    multi_domain_reasoning: str = "high",
) -> ModelRouter:
    """Build the standard model router from configuration values.

    Strongest-signal-wins priority (first match wins):
    1. uncertain or high complexity → complex_model / complex_reasoning
    2. multi-domain (>1) or medium complexity → complex_model / multi_domain_reasoning
    3. low complexity, certain, single/empty domain → default selection
    """
    default = ModelSelection(model=default_model, reasoning_effort=default_reasoning)

    rules: List[ModelRoutingRule] = [
        ModelRoutingRule(
            name="high_complexity_or_uncertain",
            condition=lambda d: (
                d.uncertain or d.complexity == QueryComplexity.HIGH
            ),
            selection=ModelSelection(
                model=complex_model, reasoning_effort=complex_reasoning
            ),
        ),
        ModelRoutingRule(
            name="medium_complexity_or_multi_domain",
            condition=lambda d: (
                len(d.domains) > 1 or d.complexity == QueryComplexity.MEDIUM
            ),
            selection=ModelSelection(
                model=complex_model, reasoning_effort=multi_domain_reasoning
            ),
        ),
    ]

    return ModelRouter(rules, default, enabled=enabled)
