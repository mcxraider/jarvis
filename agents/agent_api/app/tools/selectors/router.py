"""LLM-backed, run-scoped domain tool selection.

Every unique routing query is classified once per selector instance. Router failures
degrade to all connected tools and are memoized for the rest of the run so a broken
classifier cannot repeatedly consume the turn budget.
"""

import re
from typing import Any, Dict, List, Optional, Set

from agents.agent_api.app.async_offload import bounded_to_thread
from agents.agent_api.app.router.client import RouterClient, RouterClientError
from agents.agent_api.app.router.prompt import RouterDecision, effective_router_domains
from agents.agent_api.app.tools.base import ToolRegistry
from agents.agent_api.app.tools.control import ASK_USER_TOOL_NAME, RECALL_IMAGE_TOOL_NAME
from agents.agent_api.app.tools.selectors.static import StaticToolSelector
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter
from agents.agent_api.app.user_context.runtime import RuntimeContextSnapshotLike

_EXIT_PATTERNS = re.compile(
    r"\b(exit|cancel|never\s?mind|stop|quit)\b",
    re.IGNORECASE,
)


class RouterToolSelector:
    """Narrow tools using one live router classification per unique run query."""

    def __init__(
        self,
        router_client: RouterClient,
        snapshot: RuntimeContextSnapshotLike,
        tracer: Optional[TracePrinter] = None,
        fallback_selector: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        self._client = router_client
        self._snapshot = snapshot
        self._tracer = tracer or NULL_TRACE
        self._fallback = fallback_selector or StaticToolSelector()
        self._decision: Optional[RouterDecision] = None
        self._selected_domains: frozenset[str] = frozenset()
        self._decisions: Dict[str, RouterDecision] = {}
        self._failed_queries: Set[str] = set()

    @property
    def decision(self) -> Optional[RouterDecision]:
        return self._decision

    @property
    def selected_domains(self) -> frozenset[str]:
        return self._selected_domains

    def select_schemas(
        self,
        query: str,
        registry: ToolRegistry,
        active_domains: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        self._reset_turn_state()
        decision = self._cached_decision(query)
        if decision is None and query in self._failed_queries:
            self._trace_fallback_cache_hit()
            return self._fallback.select_schemas(query, registry, active_domains)
        if decision is None:
            self._tracer.event("router.start", "Classifying query domains.")
            try:
                decision = self._client.classify(query, self._snapshot)
            except RouterClientError as error:
                self._record_failure(query, error)
                return self._fallback.select_schemas(query, registry, active_domains)
            self._decisions[query] = decision
        return self._schemas_for_decision(query, registry, active_domains, decision)

    async def async_select_schemas(
        self,
        query: str,
        registry: ToolRegistry,
        active_domains: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        self._reset_turn_state()
        decision = self._cached_decision(query)
        if decision is None and query in self._failed_queries:
            self._trace_fallback_cache_hit()
            return await self._async_fallback_schemas(query, registry, active_domains)
        if decision is None:
            self._tracer.event("router.start", "Classifying query domains.")
            try:
                async_classify = getattr(self._client, "async_classify", None)
                if callable(async_classify):
                    decision = await async_classify(
                        query,
                        self._snapshot,
                        tracer=self._tracer,
                    )
                else:
                    decision = await bounded_to_thread(
                        self._client.classify,
                        query,
                        self._snapshot,
                    )
            except RouterClientError as error:
                self._record_failure(query, error)
                return await self._async_fallback_schemas(
                    query,
                    registry,
                    active_domains,
                )
            self._decisions[query] = decision
        return self._schemas_for_decision(query, registry, active_domains, decision)

    def _reset_turn_state(self) -> None:
        self._decision = None
        self._selected_domains = frozenset()

    def _cached_decision(self, query: str) -> Optional[RouterDecision]:
        decision = self._decisions.get(query)
        if decision is None:
            return None
        self._tracer.event(
            "router.cache_hit",
            "Reusing router decision for the same query in this run.",
            domains=len(decision.domains),
            effective_domains=len(effective_router_domains(decision)),
        )
        return decision

    def _trace_fallback_cache_hit(self) -> None:
        self._tracer.event(
            "router.fallback_cache_hit",
            "Reusing all-tools fallback after router failure in this run.",
            fallback_selector=type(self._fallback).__name__,
        )

    def _record_failure(self, query: str, error: RouterClientError) -> None:
        self._failed_queries.add(query)
        self._tracer.event(
            "router.fallback",
            "Router failed; exposing all connected tools.",
            error_type=error.payload.get("type"),
            attempts=error.payload.get("attempts"),
            fallback_selector=type(self._fallback).__name__,
            error_payload=error.payload,
        )

    async def _async_fallback_schemas(
        self,
        query: str,
        registry: ToolRegistry,
        active_domains: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        async_select = getattr(self._fallback, "async_select_schemas", None)
        if callable(async_select):
            return await async_select(query, registry, active_domains)
        return await bounded_to_thread(
            self._fallback.select_schemas,
            query,
            registry,
            active_domains,
        )

    def _schemas_for_decision(
        self,
        query: str,
        registry: ToolRegistry,
        active_domains: Optional[List[str]],
        decision: RouterDecision,
    ) -> List[Dict[str, Any]]:
        self._decision = decision
        self._tracer.event(
            "router.response",
            "Router decision received.",
            raw_domains=decision.domains,
            candidate_domains=decision.candidate_domains,
            uncertain=decision.uncertain,
            effective_domains=effective_router_domains(decision),
            outcome=decision.outcome.value,
        )

        relevant = set(effective_router_domains(decision)) & self._snapshot.active_providers()
        router_empty = not effective_router_domains(decision)
        is_exit = router_empty and bool(_EXIT_PATTERNS.search(query))
        if active_domains and not is_exit:
            pinned = set(active_domains) & self._snapshot.active_providers()
            if pinned - relevant:
                self._tracer.event(
                    "router.domain_merge",
                    "Merged pinned active domains into routing.",
                    pinned=sorted(pinned),
                    router_domains=sorted(relevant),
                    merged=sorted(relevant | pinned),
                )
            relevant |= pinned

        self._selected_domains = frozenset(relevant)
        allowed = self._allowed_tool_names(relevant)
        schemas = [spec.openai_schema for spec in registry.specs if spec.name in allowed]
        self._tracer.event(
            "router.tools.selected",
            "Selected tools for routed domains.",
            relevant=sorted(relevant) or None,
            available=len(registry.specs),
            selected=len(schemas),
        )
        return schemas

    def _allowed_tool_names(self, relevant: Set[str]) -> Set[str]:
        allowed: Set[str] = {ASK_USER_TOOL_NAME, RECALL_IMAGE_TOOL_NAME}
        tool_names_by_provider = {
            domain.provider: domain.tool_names for domain in self._snapshot.domains
        }
        for provider in relevant:
            allowed.update(tool_names_by_provider.get(provider, []))
        return allowed


__all__ = ["RouterToolSelector"]
