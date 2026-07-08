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

import re
from typing import Any, Dict, List, Optional, Set

from agents.agent_api.app.router.client import RouterClient, RouterClientError
from agents.agent_api.app.router.prompt import RouterDecision, effective_router_domains
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
_UNSUPPORTED_PROVIDER_PATTERN = re.compile(
    r"\b(?:notion|e-?mail|gmail|slack|docs|google\s+docs?|gdocs)\b",
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
        **kwargs: Any,
    ) -> None:
        # allow_mutations (and any other selector kwarg) is intentionally swallowed:
        # mutation blocking is enforced at dispatch, so this selector only shapes
        # which domains' tools the model sees — never their write-ness.
        self._client = router_client
        self._snapshot = snapshot
        self._tracer = tracer or NULL_TRACE
        self._fallback = fallback_selector or StaticToolSelector()
        self._decision: Optional[RouterDecision] = None
        # Per-run cache: keyed by routing query string. Selector instances are
        # created per run, so the cache scope matches the run scope naturally.
        self._cached_query: Optional[str] = None
        self._cached_decision: Optional[RouterDecision] = None

    @property
    def decision(self) -> Optional[RouterDecision]:
        """The last router decision, or None if the router fell back this turn."""

        return self._decision

    def select_schemas(self, query: str, registry: ToolRegistry) -> List[Dict[str, Any]]:
        # Reset per turn: a fallback must leave .decision None so the agent node
        # cannot read a stale decision from a previous turn.
        self._decision = None

        if self._cached_decision is not None and query == self._cached_query:
            self._tracer.event(
                "router.cache_hit",
                "Reusing cached router decision for same query.",
                domains=len(self._cached_decision.domains),
                effective_domains=len(effective_router_domains(self._cached_decision)),
            )
            decision = self._cached_decision
        else:
            self._tracer.event("router.start", "Classifying query domains.")
            try:
                decision = self._client.classify(query, self._snapshot)
            except RouterClientError as error:
                self._tracer.event(
                    "router.fallback",
                    "Router failed; using fallback selector.",
                    error_type=error.payload.get("type"),
                    attempts=error.payload.get("attempts"),
                    fallback_selector=type(self._fallback).__name__,
                    error_payload=error.payload,
                )
                return self._fallback.select_schemas(query, registry)
            decision = self._apply_routing_guardrails(query, decision)
            self._cached_query = query
            self._cached_decision = decision

        self._decision = decision
        self._tracer.event(
            "router.response",
            "Router decision received.",
            raw_domains=decision.domains,
            candidate_domains=decision.candidate_domains,
            uncertain=decision.uncertain,
            effective_domains=effective_router_domains(decision),
            has_rewrite=bool(decision.rewritten_query),
        )

        # Keep only domains the router named AND that are actually connected. A
        # requested-but-disconnected domain (or an empty decision) collapses to
        # ask_user only, so the orchestrator can explain rather than expose tools
        # it cannot run.
        relevant = set(effective_router_domains(decision)) & self._snapshot.active_providers()
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
        generic_event_request = bool(_GENERIC_EVENT_PATTERN.search(query))
        unsupported_provider_request = bool(_UNSUPPORTED_PROVIDER_PATTERN.search(query))

        if explicit_google_calendar and "google_calendar" not in domains:
            domains.append("google_calendar")

        if (
            routing.calendar_usage == "explicit_only"
            and not explicit_google_calendar
            and not use_candidates
        ):
            domains = [domain for domain in domains if domain != "google_calendar"]

        if not domains and generic_event_request and not unsupported_provider_request:
            provider = routing.event_provider
            if provider != "google_calendar" or explicit_google_calendar:
                domains.append(provider)

        if domains == original_effective_domains:
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
            generic_event_request=generic_event_request,
            unsupported_provider_request=unsupported_provider_request,
        )
        if use_candidates:
            return RouterDecision(
                domains=decision.domains,
                uncertain=decision.uncertain,
                candidate_domains=domains,
                rewritten_query=decision.rewritten_query,
                reasoning=decision.reasoning,
            )
        return RouterDecision(
            domains=domains,
            uncertain=decision.uncertain,
            candidate_domains=decision.candidate_domains,
            rewritten_query=decision.rewritten_query,
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
