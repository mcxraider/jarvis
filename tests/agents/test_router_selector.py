"""Tests for RouterToolSelector — per-turn, LLM-classified tool selection.

Uses a FakeRouterClient returning canned decisions (or raising) so the selector's
domain→tool filtering and its fallback contract are tested without any LLM. The
registry + snapshot mirror the real Jarvis tool set via runtime_helpers.
"""

import os

import pytest

# Disable tracing before importing anything that touches LangSmith/LangChain.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from agents.agent_api.app.router.client import RouterClientError
from agents.agent_api.app.router.prompt import RouterDecision
from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.selectors.router import RouterToolSelector
from agents.agent_api.app.tracing import TracePrinter
from tests.agents.runtime_helpers import make_snapshot

# Tool names here match runtime_helpers._TOOL_NAMES so a snapshot's per-domain
# tool_names line up with what the registry actually holds.
_TODOIST_TOOLS = ["add_todoist_task", "get_tasks"]
_CALENDAR_TOOLS = ["list_calendar_events", "delete_calendar_event"]
_ALL_TOOLS = ["ask_user", *_TODOIST_TOOLS, *_CALENDAR_TOOLS]


def _build_registry() -> ToolRegistry:
    specs = [
        ToolSpec(name=name, openai_schema={"type": "function", "function": {"name": name}})
        for name in _ALL_TOOLS
    ]
    return ToolRegistry().register(specs)


def _names(schemas):
    return {s["function"]["name"] for s in schemas}


class FakeRouterClient:
    """Stand-in RouterClient: returns a canned decision or raises."""

    def __init__(self, *, decision=None, error=None):
        self._decision = decision
        self._error = error
        self.calls = 0

    def classify(self, query, snapshot):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._decision


def _selector(decision=None, error=None, snapshot=None, fallback_selector=None):
    return RouterToolSelector(
        router_client=FakeRouterClient(decision=decision, error=error),
        snapshot=snapshot or make_snapshot(),
        fallback_selector=fallback_selector,
    )


class TestDomainFiltering:
    def test_todoist_only_exposes_todoist_tools_plus_ask_user(self):
        selector = _selector(decision=RouterDecision(domains=["todoist"]))
        result = _names(selector.select_schemas("add buy milk", _build_registry()))
        assert result == {"ask_user", *_TODOIST_TOOLS}
        assert "list_calendar_events" not in result

    def test_calendar_only_exposes_calendar_tools_plus_ask_user(self):
        selector = _selector(decision=RouterDecision(domains=["google_calendar"]))
        result = _names(selector.select_schemas("what's on my google calendar", _build_registry()))
        assert result == {"ask_user", *_CALENDAR_TOOLS}

    def test_both_domains_expose_all_tools(self):
        selector = _selector(
            decision=RouterDecision(domains=["todoist", "google_calendar"])
        )
        result = _names(selector.select_schemas("schedule this in google calendar", _build_registry()))
        assert result == set(_ALL_TOOLS)


class TestEdgeDecisions:
    def test_empty_domains_exposes_only_ask_user(self):
        """A greeting routes to no domain -> ask_user only (fastest path)."""
        selector = _selector(decision=RouterDecision(domains=[]))
        result = _names(selector.select_schemas("hello there", _build_registry()))
        assert result == {"ask_user"}

    def test_requested_but_disconnected_domain_exposes_only_ask_user(self):
        """Calendar requested but not connected -> no calendar tools, just ask_user."""
        snapshot = make_snapshot(active=("todoist",))
        selector = _selector(
            decision=RouterDecision(domains=["google_calendar"]),
            snapshot=snapshot,
        )
        result = _names(selector.select_schemas("cancel my google calendar meeting", _build_registry()))
        assert result == {"ask_user"}

    def test_unknown_domain_name_is_ignored(self):
        """An invented domain key intersects to nothing -> ask_user only."""
        selector = _selector(decision=RouterDecision(domains=["gmail"]))
        result = _names(selector.select_schemas("email bob", _build_registry()))
        assert result == {"ask_user"}


class TestRoutingGuardrails:
    def test_empty_generic_calendar_decision_routes_to_event_provider(self):
        selector = _selector(decision=RouterDecision(domains=[]))
        result = _names(selector.select_schemas("what's on my calendar this week", _build_registry()))
        assert result == {"ask_user", *_TODOIST_TOOLS}
        assert selector.decision.domains == ["todoist"]

    def test_empty_generic_cal_decision_routes_to_event_provider(self):
        selector = _selector(decision=RouterDecision(domains=[]))
        result = _names(selector.select_schemas("whats on my cal for this week", _build_registry()))
        assert result == {"ask_user", *_TODOIST_TOOLS}
        assert selector.decision.domains == ["todoist"]

    def test_generic_schedule_removes_google_calendar_when_explicit_only(self):
        selector = _selector(decision=RouterDecision(domains=["todoist", "google_calendar"]))
        result = _names(selector.select_schemas("schedule my 3pm task", _build_registry()))
        assert result == {"ask_user", *_TODOIST_TOOLS}
        assert selector.decision.domains == ["todoist"]

    def test_generic_free_busy_removes_google_calendar_when_explicit_only(self):
        selector = _selector(decision=RouterDecision(domains=["google_calendar"]))
        result = _names(selector.select_schemas("when am i free this week", _build_registry()))
        assert result == {"ask_user", *_TODOIST_TOOLS}
        assert selector.decision.domains == ["todoist"]

    def test_explicit_google_calendar_mention_keeps_calendar_route(self):
        selector = _selector(decision=RouterDecision(domains=[]))
        result = _names(selector.select_schemas("what's on my google calendar this week", _build_registry()))
        assert result == {"ask_user", *_CALENDAR_TOOLS}
        assert selector.decision.domains == ["google_calendar"]


class TestFallback:
    def test_client_error_falls_back_to_all_tools(self):
        """Any RouterClientError degrades to the static (all-tools) selector."""
        error = RouterClientError(
            {"source": "router", "type": "timeout", "retryable": True, "attempts": 2, "message": "x"}
        )
        selector = _selector(error=error)
        result = _names(selector.select_schemas("add buy milk", _build_registry()))
        assert result == set(_ALL_TOOLS)

    def test_decision_is_none_after_fallback(self):
        error = RouterClientError(
            {"source": "router", "type": "invalid_response", "retryable": False, "attempts": 1, "message": "x"}
        )
        selector = _selector(error=error)
        selector.select_schemas("add buy milk", _build_registry())
        assert selector.decision is None

    def test_custom_fallback_selector_is_used(self):
        """A provided fallback selector is honored over the default static one."""
        sentinel = [{"type": "function", "function": {"name": "sentinel"}}]

        class FixedSelector:
            def select_schemas(self, query, registry):
                return sentinel

        error = RouterClientError(
            {"source": "router", "type": "timeout", "retryable": True, "attempts": 2, "message": "x"}
        )
        selector = _selector(error=error, fallback_selector=FixedSelector())
        assert selector.select_schemas("hi", _build_registry()) is sentinel

    def test_fallback_trace_includes_payload_and_selector_name(self):
        class RecordingTracer(TracePrinter):
            def __init__(self):
                super().__init__(enabled=False)
                self.events = []

            def event(self, stage, message, **fields):
                self.events.append((stage, message, fields))

        tracer = RecordingTracer()
        error = RouterClientError(
            {"source": "router", "type": "timeout", "retryable": True, "attempts": 2, "message": "x"}
        )
        selector = RouterToolSelector(
            router_client=FakeRouterClient(error=error),
            snapshot=make_snapshot(),
            tracer=tracer,
        )
        selector.select_schemas("hi", _build_registry())
        fallback = next(event for event in tracer.events if event[0] == "router.fallback")
        fields = fallback[2]
        assert fields["fallback_selector"] == "StaticToolSelector"
        assert fields["error_payload"]["type"] == "timeout"


class TestDecisionExposure:
    def test_decision_populated_on_success(self):
        decision = RouterDecision(domains=["todoist"], rewritten_query="add milk")
        selector = _selector(decision=decision)
        selector.select_schemas("add milk", _build_registry())
        assert selector.decision is decision

    def test_decision_reset_between_turns(self):
        """A fallback turn after a good turn must clear the stale decision.

        Uses distinct queries so the per-query cache does not short-circuit the
        second call — we need it to actually reach the (now-erroring) client.
        """
        client = FakeRouterClient(decision=RouterDecision(domains=["todoist"]))
        selector = RouterToolSelector(router_client=client, snapshot=make_snapshot())
        registry = _build_registry()
        selector.select_schemas("add milk", registry)
        assert selector.decision is not None
        # Swap the client to raise, and change the query so caching does not
        # short-circuit the classify call.
        client._error = RouterClientError(
            {"source": "router", "type": "timeout", "retryable": True, "attempts": 2, "message": "x"}
        )
        selector.select_schemas("delete the report task", registry)
        assert selector.decision is None


class TestDecisionCaching:
    def test_repeat_query_uses_cache_and_calls_classify_once(self):
        """The router LLM is called only once per unique query, even across many turns."""
        client = FakeRouterClient(decision=RouterDecision(domains=["todoist"]))
        selector = RouterToolSelector(router_client=client, snapshot=make_snapshot())
        registry = _build_registry()
        for _ in range(5):
            result = _names(selector.select_schemas("add buy milk", registry))
            assert result == {"ask_user", *_TODOIST_TOOLS}
        assert client.calls == 1
        # .decision is still populated on cache-hit turns (agent node reads it).
        assert selector.decision is not None

    def test_different_query_invalidates_cache_and_reclassifies(self):
        """A new routing query (e.g. HITL redirect) is a natural cache miss."""
        client = FakeRouterClient(decision=RouterDecision(domains=["todoist"]))
        selector = RouterToolSelector(router_client=client, snapshot=make_snapshot())
        registry = _build_registry()
        selector.select_schemas("add milk", registry)
        selector.select_schemas("add milk", registry)  # cache hit
        assert client.calls == 1
        selector.select_schemas("cancel my google calendar meeting", registry)  # different query
        assert client.calls == 2

    def test_cache_hit_emits_router_cache_hit_event(self):
        class RecordingTracer(TracePrinter):
            def __init__(self):
                super().__init__(enabled=False)
                self.events = []

            def event(self, stage, message, **fields):
                self.events.append((stage, message, fields))

        tracer = RecordingTracer()
        selector = RouterToolSelector(
            router_client=FakeRouterClient(decision=RouterDecision(domains=["todoist"])),
            snapshot=make_snapshot(),
            tracer=tracer,
        )
        registry = _build_registry()
        selector.select_schemas("add milk", registry)
        selector.select_schemas("add milk", registry)
        stages = [event[0] for event in tracer.events]
        assert "router.cache_hit" in stages
        # And exactly one "router.start" event (the first, uncached turn).
        assert stages.count("router.start") == 1


class TestFactory:
    def test_get_selector_router_returns_class(self):
        from agents.agent_api.app.tools.selection import get_selector

        selector = get_selector(
            "router",
            router_client=FakeRouterClient(decision=RouterDecision()),
            snapshot=make_snapshot(),
        )
        assert isinstance(selector, RouterToolSelector)

    def test_get_selector_unknown_still_raises(self):
        from agents.agent_api.app.tools.selection import get_selector

        with pytest.raises(ValueError, match="Unknown tool selector"):
            get_selector("nonexistent")
