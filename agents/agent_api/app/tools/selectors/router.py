"""Router-backed tool selector (per-turn, LLM-classified).

Unlike the keyword selector — which matches the query against a static routing
table — this selector asks the :class:`RouterClient` which service *domains* the
query needs, then exposes only those domains' tools (plus ``ask_user``). The
classification is stable turn-to-turn within a run (the routing query is
constant), so the decision is memoized on the selector instance keyed by query
string. A different query (e.g. a HITL clarification reply that redirects to a
new domain) is a natural cache miss and re-classifies.

Non-critical by contract: any :class:`RouterClientError` degrades to a fallback
selector (the static, all-tools selector by default), so a router hiccup never
fails the run — it just gives up the narrowing for this turn.

The selector also exposes the last :class:`RouterDecision` via ``.decision`` so
the agent node can slim the system prompt and apply an optional query rewrite in
later stages; when the router falls back, ``.decision`` is ``None`` and the node
leaves the prompt/messages untouched (today's behavior).
"""

import json
import re
from typing import Any, Dict, List, Optional, Set

from agents.agent_api.app.router.cache import RouterCache, get_router_cache
from agents.agent_api.app.router.client import RouterClient, RouterClientError
from agents.agent_api.app.router.fast_path import fast_path_classify
from agents.agent_api.app.router.prompt import (
    RouterDecision,
    RouterOutcome,
    effective_router_domains,
    router_prompt_schema_fingerprint,
)
from agents.agent_api.app.async_offload import bounded_to_thread
from agents.agent_api.app.tools.base import ToolRegistry
from agents.agent_api.app.tools.control import ASK_USER_TOOL_NAME
from agents.agent_api.app.tools.selectors.static import StaticToolSelector
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter
from agents.agent_api.app.user_context.runtime import RuntimeContextSnapshot

_EXPLICIT_GOOGLE_CALENDAR_PATTERN = re.compile(
    r"\b(?:google\s+calendar|google\s+cal|gcal|google_calendar)\b",
    re.IGNORECASE,
)
_GENERIC_EVENT_PATTERN = re.compile(
    r"\b(?:cal|calendar|schedule|free|busy|availability|available|event|events|meeting|meetings|appointment|appointments)\b",
    re.IGNORECASE,
)
_REMINDER_PATTERN = re.compile(r"\b(?:remind|reminder|reminders)\b", re.IGNORECASE)
_TIME_RELATED_PATTERN = re.compile(
    r"\b(?:block(?:\s+out)?|focus\s+time|deep\s+work|time\s+block)\b",
    re.IGNORECASE,
)
_EXPLICIT_GENERIC_CALENDAR_PATTERN = re.compile(
    r"\b(?:put|add|save|create|move)\b.{0,80}\b(?:my|the)\s+calendar\b",
    re.IGNORECASE,
)
_TASK_PATTERN = re.compile(
    r"\b(?:task|tasks|to-?do|todo|project|projects|deadline|deadlines)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_PROVIDER_PATTERN = re.compile(
    r"\b(?:notion|e-?mail|gmail|slack|docs|google\s+docs?|gdocs)\b",
    re.IGNORECASE,
)
_EXIT_PATTERNS = re.compile(
    r"\b(exit|cancel|never\s?mind|stop|quit)\b",
    re.IGNORECASE,
)


class RouterToolSelector:
    """Narrows the tool set to the domains an LLM router says the query needs."""

    def __init__(
        self,
        router_client: RouterClient,
        snapshot: RuntimeContextSnapshot,
        tracer: Optional[TracePrinter] = None,
        fallback_selector: Optional[Any] = None,
        use_fast_path: bool = True,
        use_lru_cache: bool = True,
        router_cache: Optional[RouterCache] = None,
        **kwargs: Any,
    ) -> None:
        # allow_mutations (and any other selector kwarg) is intentionally swallowed:
        # mutation blocking is enforced at dispatch, so this selector only shapes
        # which domains' tools the model sees — never their write-ness.
        self._client = router_client
        self._snapshot = snapshot
        self._tracer = tracer or NULL_TRACE
        self._fallback = fallback_selector or StaticToolSelector()
        self._use_fast_path = use_fast_path
        self._use_lru_cache = use_lru_cache
        self._router_cache = router_cache or get_router_cache()
        self._active_providers = frozenset(snapshot.active_providers())
        self._routing_preferences = json.dumps(
            snapshot.preferences.routing.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        self._prompt_schema_fingerprint = router_prompt_schema_fingerprint(snapshot)
        self._decision: Optional[RouterDecision] = None
        # Per-run cache: keyed by routing query string. Selector instances are
        # created per run, so the cache scope matches the run scope naturally.
        self._cached_query: Optional[str] = None
        self._cached_decision: Optional[RouterDecision] = None
        # Non-retryable failures (invalid request/response contract) are stable
        # for an unchanged query. Remember them for this run so every agent turn
        # does not repeat the same doomed router request. A changed clarification
        # query is a different key and gets a fresh classification attempt.
        self._failed_queries: Set[str] = set()

    @property
    def decision(self) -> Optional[RouterDecision]:
        """The last router decision, or None if the router fell back this turn."""

        return self._decision

    def select_schemas(
        self,
        query: str,
        registry: ToolRegistry,
        active_domains: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        self._decision = None
        decision = self._cached_or_deterministic_decision(query)
        if decision is None and query in self._failed_queries:
            self._trace_fallback_cache_hit()
            return self._fallback.select_schemas(query, registry)

        if decision is None:
            self._tracer.event("router.start", "Classifying query domains.")
            try:
                decision = self._client.classify(query, self._snapshot)
            except RouterClientError as error:
                self._record_failure(query, error)
                return self._fallback.select_schemas(query, registry)
            self._cache_router_result(query, decision)
            decision = self._apply_routing_guardrails(query, decision)
            self._cache_decision(query, decision)

        return self._schemas_for_decision(
            query,
            registry,
            active_domains,
            decision,
        )

    async def async_select_schemas(
        self,
        query: str,
        registry: ToolRegistry,
        active_domains: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Select schemas without blocking on the router's network request.

        ``RouterClient.async_classify`` is the production path. A sync-only
        injected client is retained as a compatibility seam and is moved to a
        worker thread so it cannot block the event loop.
        """

        self._decision = None
        decision = self._cached_or_deterministic_decision(query)
        if decision is None and query in self._failed_queries:
            self._trace_fallback_cache_hit()
            return await self._async_fallback_schemas(
                query,
                registry,
                active_domains,
            )

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
            self._cache_router_result(query, decision)
            decision = self._apply_routing_guardrails(query, decision)
            self._cache_decision(query, decision)

        return self._schemas_for_decision(
            query,
            registry,
            active_domains,
            decision,
        )

    def _cached_decision_for_query(self, query: str) -> Optional[RouterDecision]:
        if self._cached_decision is None or query != self._cached_query:
            return None
        self._tracer.event(
            "router.cache_hit",
            "Reusing cached router decision for same query.",
            domains=len(self._cached_decision.domains),
            effective_domains=len(effective_router_domains(self._cached_decision)),
        )
        return self._cached_decision

    def _cached_or_deterministic_decision(
        self,
        query: str,
    ) -> Optional[RouterDecision]:
        decision = self._cached_decision_for_query(query)
        if decision is not None:
            return decision

        if self._use_fast_path:
            decision = fast_path_classify(query, self._snapshot)
            if decision is not None:
                self._tracer.event(
                    "router.fast_path",
                    "Used a deterministic low-complexity router decision.",
                    outcome=decision.outcome.value,
                    domains=[domain.value for domain in decision.domains],
                )
                decision = self._apply_routing_guardrails(query, decision)
                self._cache_decision(query, decision)
                return decision

        if not self._use_lru_cache:
            return None
        decision = self._router_cache.get(
            query,
            active_providers=self._active_providers,
            routing_preferences=self._routing_preferences,
            prompt_schema_fingerprint=self._prompt_schema_fingerprint,
        )
        if decision is None:
            return None
        self._tracer.event(
            "router.lru_cache_hit",
            "Reused a successful process-local router decision.",
            outcome=decision.outcome.value,
            domains=[domain.value for domain in decision.domains],
        )
        decision = self._apply_routing_guardrails(query, decision)
        self._cache_decision(query, decision)
        return decision

    def _cache_router_result(self, query: str, decision: RouterDecision) -> None:
        if not self._use_lru_cache:
            return
        self._router_cache.put(
            query,
            decision,
            active_providers=self._active_providers,
            routing_preferences=self._routing_preferences,
            prompt_schema_fingerprint=self._prompt_schema_fingerprint,
        )

    def _trace_fallback_cache_hit(self) -> None:
        self._tracer.event(
            "router.fallback_cache_hit",
            "Reusing static fallback after non-retryable router failure.",
            fallback_selector=type(self._fallback).__name__,
        )

    def _record_failure(self, query: str, error: RouterClientError) -> None:
        if not error.payload.get("retryable", False):
            self._failed_queries.add(query)
        self._tracer.event(
            "router.fallback",
            "Router failed; using fallback selector.",
            error_type=error.payload.get("type"),
            attempts=error.payload.get("attempts"),
            fallback_selector=type(self._fallback).__name__,
            error_payload=error.payload,
        )

    def _cache_decision(self, query: str, decision: RouterDecision) -> None:
        self._cached_query = query
        self._cached_decision = decision

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

        # Keep only domains the router named AND that are actually connected. A
        # requested-but-disconnected domain (or an empty decision) collapses to
        # ask_user only, so the orchestrator can explain rather than expose tools
        # it cannot run.
        relevant = set(effective_router_domains(decision)) & self._snapshot.active_providers()

        # Merge pinned domains from the original request (HITL resume path).
        # If the router returns empty AND the query matches exit patterns, the
        # user is cancelling — don't pin domains, allow clean teardown.
        if active_domains:
            router_empty = not effective_router_domains(decision)
            is_exit = router_empty and bool(_EXIT_PATTERNS.search(query))
            if not is_exit:
                pinned = set(active_domains) & self._snapshot.active_providers()
                if pinned - relevant:
                    self._tracer.event(
                        "router.domain_merge",
                        "Merged pinned active_domains into routing.",
                        pinned=sorted(pinned),
                        router_domains=sorted(relevant),
                        merged=sorted(relevant | pinned),
                    )
                relevant |= pinned

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

    def _apply_routing_guardrails(
        self,
        query: str,
        decision: RouterDecision,
    ) -> RouterDecision:
        """Correct known-dangerous routing ambiguities before exposing tools."""

        use_candidates = decision.uncertain and bool(decision.candidate_domains)
        original_effective_domains = effective_router_domains(decision)
        domains = list(original_effective_domains)
        routing = self._snapshot.preferences.routing
        explicit_google_calendar = bool(_EXPLICIT_GOOGLE_CALENDAR_PATTERN.search(query))
        explicit_generic_calendar = bool(
            _EXPLICIT_GENERIC_CALENDAR_PATTERN.search(query)
        )
        generic_event_request = bool(_GENERIC_EVENT_PATTERN.search(query))
        reminder_request = bool(_REMINDER_PATTERN.search(query))
        time_related_request = bool(_TIME_RELATED_PATTERN.search(query))
        task_request = bool(_TASK_PATTERN.search(query))
        unsupported_provider_request = bool(_UNSUPPORTED_PROVIDER_PATTERN.search(query))

        if explicit_google_calendar and "google_calendar" not in domains:
            domains.append("google_calendar")

        if (
            routing.calendar_usage == "explicit_only"
            and not explicit_google_calendar
            and not explicit_generic_calendar
            and not use_candidates
        ):
            domains = [domain for domain in domains if domain != "google_calendar"]

        if not domains and explicit_generic_calendar and not unsupported_provider_request:
            domains.append(routing.explicit_calendar_provider)

        if not domains and reminder_request and not unsupported_provider_request:
            domains.append(routing.reminder_provider)

        if not domains and time_related_request and not unsupported_provider_request:
            domains.append(routing.time_related_provider)

        if not domains and generic_event_request and not unsupported_provider_request:
            provider = routing.event_provider
            domains.append(provider)

        if not domains and task_request and not unsupported_provider_request:
            domains.append(routing.task_provider)

        corrected_miss = (
            decision.outcome in {
                RouterOutcome.CONVERSATION,
                RouterOutcome.UNSUPPORTED_PROVIDER,
            }
            and bool(domains)
        )
        if domains == original_effective_domains and not corrected_miss:
            return decision

        self._tracer.event(
            "router.guardrail",
            "Adjusted router decision from routing preferences.",
            original_domains=decision.domains,
            original_candidate_domains=decision.candidate_domains,
            original_effective_domains=original_effective_domains,
            adjusted_effective_domains=domains,
            uncertain=decision.uncertain,
            explicit_google_calendar=explicit_google_calendar,
            explicit_generic_calendar=explicit_generic_calendar,
            generic_event_request=generic_event_request,
            reminder_request=reminder_request,
            time_related_request=time_related_request,
            unsupported_provider_request=unsupported_provider_request,
            task_request=task_request,
        )
        if corrected_miss:
            self._tracer.event(
                "router.classifier_miss_corrected",
                "Corrected router outcome using deterministic service anchors.",
                original_outcome=decision.outcome.value,
                adjusted_outcome=RouterOutcome.ROUTED.value,
                adjusted_domains=domains,
                matched_event_anchor=generic_event_request,
                matched_task_anchor=task_request,
                matched_explicit_calendar=explicit_google_calendar,
            )
        if use_candidates:
            primary_domains = [
                domain.value for domain in decision.domains if domain.value in domains
            ]
            outcome = decision.outcome
            if outcome == RouterOutcome.ROUTED and not primary_domains:
                primary_domains = domains[:1]
            return RouterDecision(
                outcome=outcome,
                domains=primary_domains,
                uncertain=decision.uncertain,
                candidate_domains=domains,
                complexity=decision.complexity,
                reasoning=decision.reasoning,
            )
        return RouterDecision(
            outcome=RouterOutcome.ROUTED,
            domains=domains,
            uncertain=False,
            candidate_domains=[],
            complexity=decision.complexity,
            reasoning=decision.reasoning,
        )

    def _allowed_tool_names(self, relevant: Set[str]) -> Set[str]:
        """Union of the relevant domains' registered tool names, plus ask_user."""

        allowed: Set[str] = {ASK_USER_TOOL_NAME}
        tool_names_by_provider = {
            domain.provider: domain.tool_names for domain in self._snapshot.domains
        }
        for provider in relevant:
            allowed.update(tool_names_by_provider.get(provider, []))
        return allowed


__all__ = ["RouterToolSelector"]
