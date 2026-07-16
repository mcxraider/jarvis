"""Tests for RouterToolSelector — per-turn, LLM-classified tool selection.

Uses a FakeRouterClient returning canned decisions (or raising) so the selector's
domain→tool filtering and its fallback contract are tested without any LLM. The
registry + snapshot mirror the real Jarvis tool set via runtime_helpers.
"""

import asyncio
import os

import pytest

# Disable tracing before importing anything that touches LangSmith/LangChain.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from agents.agent_api.app.router.client import RouterClientError
from agents.agent_api.app.router.cache import reset_router_cache
from agents.agent_api.app.router.prompt import RouterDecision
from agents.agent_api.app.tools.selectors import router as router_selector_module
from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.selectors.router import RouterToolSelector
from agents.agent_api.app.tracing import TracePrinter
from tests.agents.runtime_helpers import make_snapshot

# Tool names here match runtime_helpers._TOOL_NAMES so a snapshot's per-domain
# tool_names line up with what the registry actually holds.
_TODOIST_TOOLS = ["add_todoist_task", "get_tasks"]
_CALENDAR_TOOLS = ["list_calendar_events", "delete_calendar_event"]
_ALL_TOOLS = ["ask_user", *_TODOIST_TOOLS, *_CALENDAR_TOOLS]


@pytest.fixture(autouse=True)
def _isolate_legacy_selector_tests(monkeypatch):
    """Keep pre-Stage-7 tests focused on the injected router-client contract."""

    reset_router_cache()
    monkeypatch.setattr(
        router_selector_module,
        "fast_path_classify",
        lambda _query, _snapshot: None,
    )
    yield
    reset_router_cache()


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
        self.async_calls = 0

    def classify(self, query, snapshot):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._decision

    async def async_classify(self, query, snapshot, **kwargs):
        del query, snapshot, kwargs
        self.async_calls += 1
        if self._error is not None:
            raise self._error
        return self._decision


def _selector(decision=None, error=None, snapshot=None, fallback_selector=None, tracer=None):
    return RouterToolSelector(
        router_client=FakeRouterClient(decision=decision, error=error),
        snapshot=snapshot or make_snapshot(),
        fallback_selector=fallback_selector,
        tracer=tracer,
    )


class TestDomainFiltering:
    def test_todoist_only_exposes_todoist_tools_plus_ask_user(self):
        selector = _selector(decision=RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test"))
        result = _names(selector.select_schemas("add buy milk", _build_registry()))
        assert result == {"ask_user", *_TODOIST_TOOLS}
        assert "list_calendar_events" not in result

    def test_calendar_only_exposes_calendar_tools_plus_ask_user(self):
        selector = _selector(decision=RouterDecision(outcome="routed", domains=["google_calendar"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test"))
        result = _names(selector.select_schemas("what's on my google calendar", _build_registry()))
        assert result == {"ask_user", *_CALENDAR_TOOLS}

    def test_both_domains_expose_all_tools(self):
        selector = _selector(
            decision=RouterDecision(outcome="routed", domains=["todoist", "google_calendar"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")
        )
        result = _names(selector.select_schemas("schedule this in google calendar", _build_registry()))
        assert result == set(_ALL_TOOLS)

    def test_logged_multi_domain_request_keeps_calendar_and_todoist(self):
        selector = _selector(
            decision=RouterDecision(
                outcome="routed",
                domains=["google_calendar", "todoist"],
                uncertain=False,
                candidate_domains=[],
                complexity="low",
                reasoning="calendar lookup and task update",
            )
        )
        result = _names(
            selector.select_schemas(
                "pull my events from my govtech google calendar and then update "
                "lunch with grandparents on todoist to p3 task.",
                _build_registry(),
            )
        )
        assert result == set(_ALL_TOOLS)
        assert selector.decision.outcome == "routed"
        assert set(selector.decision.domains) == {"google_calendar", "todoist"}


class TestEdgeDecisions:
    def test_empty_domains_exposes_only_ask_user(self):
        """A greeting routes to no domain -> ask_user only (fastest path)."""
        selector = _selector(decision=RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], complexity="low", reasoning="test"))
        result = _names(selector.select_schemas("hello there", _build_registry()))
        assert result == {"ask_user"}

    def test_requested_but_disconnected_domain_exposes_only_ask_user(self):
        """Calendar requested but not connected -> no calendar tools, just ask_user."""
        snapshot = make_snapshot(active=("todoist",))
        selector = _selector(
            decision=RouterDecision(outcome="routed", domains=["google_calendar"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test"),
            snapshot=snapshot,
        )
        result = _names(selector.select_schemas("cancel my google calendar meeting", _build_registry()))
        assert result == {"ask_user"}

    def test_unknown_domain_name_is_rejected(self):
        with pytest.raises(Exception):
            RouterDecision(outcome="routed", domains=["gmail"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")


class TestRoutingGuardrails:
    @pytest.mark.parametrize(
        "query",
        [
            "check my notion schedule for tomorrow",
            "can u check my email for the invoice tomorrow",
            "search gmail for the meeting invite",
            "check slack docs for next week",
            "look in google docs for tomorrow's plan",
        ],
    )
    def test_empty_unsupported_provider_decision_stays_empty(self, query):
        selector = _selector(decision=RouterDecision(outcome="unsupported_provider", domains=[], uncertain=False, candidate_domains=[], complexity="low", reasoning="test"))
        result = _names(selector.select_schemas(query, _build_registry()))
        assert result == {"ask_user"}
        assert selector.decision.domains == []

    def test_empty_generic_calendar_decision_routes_to_event_provider(self):
        selector = _selector(decision=RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], complexity="high", reasoning="test"))
        result = _names(selector.select_schemas("what's on my calendar this week", _build_registry()))
        assert result == {"ask_user", *_TODOIST_TOOLS}
        assert selector.decision.domains == ["todoist"]
        assert selector.decision.outcome == "routed"
        assert selector.decision.complexity == "high"

    def test_empty_generic_cal_decision_routes_to_event_provider(self):
        selector = _selector(decision=RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], complexity="low", reasoning="test"))
        result = _names(selector.select_schemas("whats on my cal for this week", _build_registry()))
        assert result == {"ask_user", *_TODOIST_TOOLS}
        assert selector.decision.domains == ["todoist"]

    def test_empty_task_classifier_miss_routes_to_task_provider(self):
        class RecordingTracer:
            def __init__(self):
                self.events = []

            def event(self, stage, message, **fields):
                self.events.append((stage, message, fields))

        tracer = RecordingTracer()
        selector = _selector(
            decision=RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], complexity="low", reasoning="miss"),
            tracer=tracer,
        )
        result = _names(selector.select_schemas("show my tasks", _build_registry()))
        assert result == {"ask_user", *_TODOIST_TOOLS}
        assert selector.decision.outcome == "routed"
        assert any(event[0] == "router.classifier_miss_corrected" for event in tracer.events)

    def test_generic_schedule_removes_google_calendar_when_explicit_only(self):
        selector = _selector(decision=RouterDecision(outcome="routed", domains=["todoist", "google_calendar"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test"))
        result = _names(selector.select_schemas("schedule my 3pm task", _build_registry()))
        assert result == {"ask_user", *_TODOIST_TOOLS}
        assert selector.decision.domains == ["todoist"]

    def test_generic_free_busy_removes_google_calendar_when_explicit_only(self):
        selector = _selector(decision=RouterDecision(outcome="routed", domains=["google_calendar"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test"))
        result = _names(selector.select_schemas("when am i free this week", _build_registry()))
        assert result == {"ask_user", *_TODOIST_TOOLS}
        assert selector.decision.domains == ["todoist"]

    def test_explicit_google_calendar_mention_keeps_calendar_route(self):
        selector = _selector(decision=RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], complexity="low", reasoning="test"))
        result = _names(selector.select_schemas("what's on my google calendar this week", _build_registry()))
        assert result == {"ask_user", *_CALENDAR_TOOLS}
        assert selector.decision.domains == ["google_calendar"]

    def test_explicit_supported_provider_survives_unsupported_anchor(self):
        selector = _selector(decision=RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], complexity="low", reasoning="test"))
        result = _names(
            selector.select_schemas(
                "check my gmail and google calendar for tomorrow",
                _build_registry(),
            )
        )
        assert result == {"ask_user", *_CALENDAR_TOOLS}
        assert selector.decision.domains == ["google_calendar"]


class TestUncertainDecisions:
    def test_uncertain_decision_uses_candidate_domains(self):
        selector = _selector(
            decision=RouterDecision(
                outcome="routed",
                domains=["todoist"],
                uncertain=True,
                candidate_domains=["todoist", "google_calendar"],
                complexity="low",
                reasoning="test",
            )
        )
        result = _names(selector.select_schemas("ambiguous planning request", _build_registry()))
        assert result == set(_ALL_TOOLS)

    def test_certain_decision_rejects_candidate_domains(self):
        with pytest.raises(Exception):
            RouterDecision(
                outcome="routed", domains=["todoist"], uncertain=False,
                candidate_domains=["todoist", "google_calendar"], complexity="low", reasoning="test",
            )

    def test_explicit_only_does_not_discard_uncertain_candidate_domains(self):
        selector = _selector(
            decision=RouterDecision(
                outcome="routed",
                domains=["todoist"],
                uncertain=True,
                candidate_domains=["todoist", "google_calendar"],
                complexity="low",
                reasoning="test",
            )
        )
        result = _names(selector.select_schemas("schedule my 3pm task", _build_registry()))
        assert result == set(_ALL_TOOLS)
        assert selector.decision.domains == ["todoist"]
        assert selector.decision.candidate_domains == ["todoist", "google_calendar"]


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

    def test_non_retryable_failure_is_cached_for_same_query(self):
        error = RouterClientError(
            {"source": "router", "type": "client_error", "retryable": False, "attempts": 1, "message": "x"}
        )
        client = FakeRouterClient(error=error)
        selector = RouterToolSelector(router_client=client, snapshot=make_snapshot())
        registry = _build_registry()

        assert _names(selector.select_schemas("add buy milk", registry)) == set(_ALL_TOOLS)
        assert _names(selector.select_schemas("add buy milk", registry)) == set(_ALL_TOOLS)
        assert client.calls == 1
        assert selector.decision is None

    def test_changed_query_retries_after_cached_non_retryable_failure(self):
        error = RouterClientError(
            {"source": "router", "type": "client_error", "retryable": False, "attempts": 1, "message": "x"}
        )
        client = FakeRouterClient(error=error)
        selector = RouterToolSelector(router_client=client, snapshot=make_snapshot())
        registry = _build_registry()

        selector.select_schemas("add buy milk", registry)
        selector.select_schemas("show my google calendar", registry)
        assert client.calls == 2

    def test_retryable_failure_is_not_cached(self):
        error = RouterClientError(
            {"source": "router", "type": "timeout", "retryable": True, "attempts": 2, "message": "x"}
        )
        client = FakeRouterClient(error=error)
        selector = RouterToolSelector(router_client=client, snapshot=make_snapshot())
        registry = _build_registry()

        selector.select_schemas("add buy milk", registry)
        selector.select_schemas("add buy milk", registry)
        assert client.calls == 2

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
        decision = RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")
        selector = _selector(decision=decision)
        selector.select_schemas("add milk", _build_registry())
        assert selector.decision is decision

    def test_decision_reset_between_turns(self):
        """A fallback turn after a good turn must clear the stale decision.

        Uses distinct queries so the per-query cache does not short-circuit the
        second call — we need it to actually reach the (now-erroring) client.
        """
        client = FakeRouterClient(decision=RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test"))
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
        client = FakeRouterClient(decision=RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test"))
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
        client = FakeRouterClient(decision=RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test"))
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
            router_client=FakeRouterClient(decision=RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")),
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
            router_client=FakeRouterClient(decision=RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")),
            snapshot=make_snapshot(),
        )
        assert isinstance(selector, RouterToolSelector)

    def test_get_selector_unknown_still_raises(self):
        from agents.agent_api.app.tools.selection import get_selector

        with pytest.raises(ValueError, match="Unknown tool selector"):
            get_selector("nonexistent")


class TestAsyncSelection:
    def test_awaits_async_router_without_calling_sync_path(self):
        client = FakeRouterClient(
            decision=RouterDecision(
                outcome="routed",
                domains=["todoist"],
                uncertain=False,
                candidate_domains=[],
                complexity="low",
                reasoning="test",
            )
        )
        selector = RouterToolSelector(router_client=client, snapshot=make_snapshot())

        result = asyncio.run(
            selector.async_select_schemas("add buy milk", _build_registry())
        )

        assert _names(result) == {"ask_user", *_TODOIST_TOOLS}
        assert client.async_calls == 1
        assert client.calls == 0
        assert selector.decision.outcome == "routed"

    def test_guardrails_and_pinned_domains_match_sync_path(self):
        client = FakeRouterClient(
            decision=RouterDecision(
                outcome="conversation",
                domains=[],
                uncertain=False,
                candidate_domains=[],
                complexity="high",
                reasoning="test",
            )
        )
        selector = RouterToolSelector(router_client=client, snapshot=make_snapshot())

        result = asyncio.run(
            selector.async_select_schemas(
                "show my tasks",
                _build_registry(),
                active_domains=["google_calendar"],
            )
        )

        assert _names(result) == set(_ALL_TOOLS)
        assert selector.decision.outcome == "routed"
        assert selector.decision.domains == ["todoist"]
        assert selector.decision.complexity == "high"

    def test_repeat_query_uses_same_decision_cache(self):
        client = FakeRouterClient(
            decision=RouterDecision(
                outcome="routed",
                domains=["todoist"],
                uncertain=False,
                candidate_domains=[],
                complexity="low",
                reasoning="test",
            )
        )
        selector = RouterToolSelector(router_client=client, snapshot=make_snapshot())
        registry = _build_registry()

        async def run():
            first = await selector.async_select_schemas("add milk", registry)
            second = await selector.async_select_schemas("add milk", registry)
            return first, second

        first, second = asyncio.run(run())
        assert _names(first) == _names(second) == {"ask_user", *_TODOIST_TOOLS}
        assert client.async_calls == 1
        assert client.calls == 0

    def test_non_retryable_failure_and_fallback_are_cached(self):
        error = RouterClientError(
            {
                "source": "router",
                "type": "client_error",
                "retryable": False,
                "attempts": 1,
                "message": "x",
            }
        )
        client = FakeRouterClient(error=error)
        selector = RouterToolSelector(router_client=client, snapshot=make_snapshot())
        registry = _build_registry()

        async def run():
            first = await selector.async_select_schemas("add milk", registry)
            second = await selector.async_select_schemas("add milk", registry)
            return first, second

        first, second = asyncio.run(run())
        assert _names(first) == _names(second) == set(_ALL_TOOLS)
        assert client.async_calls == 1
        assert selector.decision is None

    def test_retryable_failure_is_retried(self):
        error = RouterClientError(
            {
                "source": "router",
                "type": "timeout",
                "retryable": True,
                "attempts": 2,
                "message": "x",
            }
        )
        client = FakeRouterClient(error=error)
        selector = RouterToolSelector(router_client=client, snapshot=make_snapshot())
        registry = _build_registry()

        async def run():
            await selector.async_select_schemas("add milk", registry)
            await selector.async_select_schemas("add milk", registry)

        asyncio.run(run())

        assert client.async_calls == 2
        assert selector.decision is None

    def test_exit_query_does_not_merge_pinned_domains(self):
        client = FakeRouterClient(
            decision=RouterDecision(
                outcome="conversation",
                domains=[],
                uncertain=False,
                candidate_domains=[],
                complexity="low",
                reasoning="test",
            )
        )
        selector = RouterToolSelector(router_client=client, snapshot=make_snapshot())

        result = asyncio.run(
            selector.async_select_schemas(
                "cancel",
                _build_registry(),
                active_domains=["todoist"],
            )
        )

        assert _names(result) == {"ask_user"}
        assert selector.decision.outcome == "conversation"
        assert selector.decision.domains == []

    def test_async_custom_fallback_is_awaited(self):
        sentinel = [{"type": "function", "function": {"name": "sentinel"}}]

        class AsyncFallback:
            def __init__(self):
                self.calls = 0

            def select_schemas(self, query, registry, active_domains=None):
                raise AssertionError("sync fallback must not run on async path")

            async def async_select_schemas(
                self, query, registry, active_domains=None
            ):
                del query, registry, active_domains
                self.calls += 1
                return sentinel

        fallback = AsyncFallback()
        error = RouterClientError(
            {
                "source": "router",
                "type": "timeout",
                "retryable": True,
                "attempts": 2,
                "message": "x",
            }
        )
        selector = _selector(error=error, fallback_selector=fallback)

        result = asyncio.run(selector.async_select_schemas("hi", _build_registry()))

        assert result is sentinel
        assert fallback.calls == 1

    def test_sync_only_client_runs_through_compatibility_adapter(self):
        class SyncOnlyClient:
            def __init__(self):
                self.calls = 0

            def classify(self, query, snapshot):
                del query, snapshot
                self.calls += 1
                return RouterDecision(
                    outcome="routed",
                    domains=["todoist"],
                    uncertain=False,
                    candidate_domains=[],
                    complexity="low",
                    reasoning="test",
                )

        client = SyncOnlyClient()
        selector = RouterToolSelector(router_client=client, snapshot=make_snapshot())

        result = asyncio.run(
            selector.async_select_schemas("show tasks", _build_registry())
        )

        assert _names(result) == {"ask_user", *_TODOIST_TOOLS}
        assert client.calls == 1
