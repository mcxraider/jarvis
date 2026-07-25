"""Stage 7 router fast-path and process-cache coverage."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agents.agent_api.app.router.cache import RouterCache, normalize_router_query
from agents.agent_api.app.router.fast_path import fast_path_classify
from agents.agent_api.app.router.model_router import create_default_model_router
from agents.agent_api.app.router.prompt import RouterDecision, RouterOutcome
from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.selectors.router import RouterToolSelector
from tests.agents.runtime_helpers import make_preferences, make_snapshot


def _decision(
    *,
    outcome: str = "routed",
    domains: list[str] | None = None,
    uncertain: bool = False,
    candidates: list[str] | None = None,
    complexity: str = "low",
    reasoning: str = "test",
) -> RouterDecision:
    return RouterDecision(
        outcome=outcome,
        domains=domains if domains is not None else ["todoist"],
        uncertain=uncertain,
        candidate_domains=candidates or [],
        complexity=complexity,
        reasoning=reasoning,
    )


class _Client:
    def __init__(self, decision: RouterDecision):
        self.decision = decision
        self.sync_calls = 0
        self.async_calls = 0

    def classify(self, _query, _snapshot):
        self.sync_calls += 1
        return self.decision

    async def async_classify(self, _query, _snapshot, **_kwargs):
        self.async_calls += 1
        return self.decision


def _registry() -> ToolRegistry:
    names = [
        "ask_user",
        "add_todoist_task",
        "get_tasks",
        "list_calendar_events",
        "delete_calendar_event",
    ]
    return ToolRegistry().register(
        [
            ToolSpec(
                name=name,
                openai_schema={"type": "function", "function": {"name": name}},
            )
            for name in names
        ]
    )


def _schema_names(schemas) -> set[str]:
    return {schema["function"]["name"] for schema in schemas}


class TestFastPath:
    @pytest.mark.parametrize(
        ("query", "outcome", "domains"),
        [
            ("hello!", RouterOutcome.CONVERSATION, []),
            ("show my tasks", RouterOutcome.ROUTED, ["todoist"]),
            (
                "what is on my Google Calendar today?",
                RouterOutcome.ROUTED,
                ["google_calendar"],
            ),
        ],
    )
    def test_returns_complete_strict_low_complexity_decisions(
        self, query, outcome, domains
    ):
        decision = fast_path_classify(query, make_snapshot())

        assert decision is not None
        assert decision.outcome is outcome
        assert decision.domains == domains
        assert decision.uncertain is False
        assert decision.candidate_domains == []
        assert decision.complexity == "low"

    def test_tasks_always_use_todoist(self):
        snapshot = make_snapshot(
            preferences=make_preferences(
                task_provider="todoist",
                event_provider="todoist",
            )
        )

        decision = fast_path_classify("show my tasks", snapshot)

        assert decision is not None
        assert decision.domains == ["todoist"]

    def test_routing_exceptions_disable_the_deterministic_fast_path(self):
        snapshot = make_snapshot(
            preferences=make_preferences(
                routing_exceptions=[
                    {
                        "when": "requests about the launch project",
                        "provider": "google_calendar",
                    }
                ]
            )
        )

        assert fast_path_classify("show my tasks", snapshot) is None

    @pytest.mark.parametrize(
        "query",
        [
            "analyze all my tasks and optimize my monthly plan",
            "compare my Todoist tasks then update Google Calendar",
            "what is on my schedule tomorrow",
            "check Slack for my tasks",
        ],
    )
    def test_uncertain_or_complex_queries_fall_through(self, query):
        assert fast_path_classify(query, make_snapshot()) is None

    def test_disconnected_explicit_provider_falls_through(self):
        snapshot = make_snapshot(active=("todoist",))
        assert fast_path_classify("show my Google Calendar", snapshot) is None

    def test_sync_selector_bypasses_router_client(self):
        client = _Client(_decision(complexity="high"))
        selector = RouterToolSelector(
            client,
            make_snapshot(),
            router_cache=RouterCache(),
        )

        schemas = selector.select_schemas("show my tasks", _registry())

        assert _schema_names(schemas) == {
            "ask_user",
            "add_todoist_task",
            "get_tasks",
        }
        assert client.sync_calls == 0
        assert selector.decision.complexity == "low"

    def test_async_selector_bypasses_router_client(self):
        client = _Client(_decision(complexity="high"))
        selector = RouterToolSelector(
            client,
            make_snapshot(),
            router_cache=RouterCache(),
        )

        schemas = asyncio.run(
            selector.async_select_schemas(
                "show my Google Calendar",
                _registry(),
            )
        )

        assert _schema_names(schemas) == {
            "ask_user",
            "list_calendar_events",
            "delete_calendar_event",
        }
        assert client.async_calls == 0

    def test_complex_task_query_preserves_llm_complexity_for_model_router(self):
        client = _Client(_decision(complexity="high"))
        selector = RouterToolSelector(
            client,
            make_snapshot(),
            router_cache=RouterCache(),
        )

        selector.select_schemas(
            "analyze all my tasks and optimize my monthly plan",
            _registry(),
        )
        selection = create_default_model_router(
            default_model="flash",
            complex_model="pro",
        ).select(selector.decision)

        assert client.sync_calls == 1
        assert selector.decision.complexity == "high"
        assert selection.model == "pro"


class TestRouterCache:
    _CONTEXT = {
        "active_providers": ("todoist",),
        "routing_preferences": '{"task_provider":"todoist"}',
        "prompt_schema_fingerprint": "fingerprint-a",
    }

    def test_normalizes_unicode_case_and_whitespace(self):
        cache = RouterCache()
        assert cache.put("  SHOW\u3000My Tasks ", _decision(), **self._CONTEXT)

        cached = cache.get("show my   tasks", **self._CONTEXT)

        assert cached == _decision()
        assert normalize_router_query("  SHOW\u3000My Tasks ") == "show my tasks"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("active_providers", ("todoist", "google_calendar")),
            ("routing_preferences", '{"task_provider":"google_calendar"}'),
            ("prompt_schema_fingerprint", "fingerprint-b"),
        ],
    )
    def test_context_components_isolate_entries(self, field, value):
        cache = RouterCache()
        cache.put("show my tasks", _decision(), **self._CONTEXT)
        changed = {**self._CONTEXT, field: value}

        assert cache.get("show my tasks", **changed) is None

    def test_fake_clock_expiry_requires_no_sleep(self):
        now = [10.0]
        cache = RouterCache(ttl_seconds=5, clock=lambda: now[0])
        cache.put("query", _decision(), **self._CONTEXT)
        now[0] = 14.999
        assert cache.get("query", **self._CONTEXT) is not None
        now[0] = 15.0
        assert cache.get("query", **self._CONTEXT) is None

    def test_lru_eviction_and_locked_stats(self):
        cache = RouterCache(max_entries=2)
        cache.put("one", _decision(), **self._CONTEXT)
        cache.put("two", _decision(), **self._CONTEXT)
        assert cache.get("one", **self._CONTEXT) is not None
        cache.put("three", _decision(), **self._CONTEXT)

        assert cache.get("two", **self._CONTEXT) is None
        assert cache.stats == {"hits": 1, "misses": 1, "size": 2}

    def test_uncertain_decisions_are_not_cached(self):
        cache = RouterCache()
        uncertain = _decision(
            uncertain=True,
            candidates=["todoist", "google_calendar"],
        )

        assert cache.put("query", uncertain, **self._CONTEXT) is False
        assert cache.get("query", **self._CONTEXT) is None

    def test_cached_decisions_are_isolated_copies(self):
        cache = RouterCache()
        cache.put("query", _decision(), **self._CONTEXT)
        first = cache.get("query", **self._CONTEXT)
        assert first is not None
        first.reasoning = "mutated"

        second = cache.get("query", **self._CONTEXT)

        assert second is not None
        assert second.reasoning == "test"

    def test_concurrent_gets_and_puts_remain_bounded(self):
        cache = RouterCache(max_entries=16)
        barrier = threading.Barrier(8)

        def write_and_read(index: int) -> RouterDecision | None:
            barrier.wait()
            query = f"query {index}"
            cache.put(query, _decision(reasoning=str(index)), **self._CONTEXT)
            return cache.get(query, **self._CONTEXT)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(write_and_read, range(8)))

        assert all(result is not None for result in results)
        assert cache.stats["size"] == 8


class TestSelectorProcessCache:
    def test_sync_result_is_reused_by_async_selector(self):
        cache = RouterCache()
        snapshot = make_snapshot()
        first_client = _Client(_decision())
        first = RouterToolSelector(
            first_client,
            snapshot,
            use_fast_path=False,
            router_cache=cache,
        )
        first.select_schemas("route this request", _registry())

        second_client = _Client(_decision(domains=["google_calendar"]))
        second = RouterToolSelector(
            second_client,
            snapshot,
            use_fast_path=False,
            router_cache=cache,
        )
        schemas = asyncio.run(
            second.async_select_schemas("  ROUTE   this request ", _registry())
        )

        assert first_client.sync_calls == 1
        assert second_client.async_calls == 0
        assert _schema_names(schemas) == {
            "ask_user",
            "add_todoist_task",
            "get_tasks",
        }

    def test_cached_raw_classifier_miss_reapplies_guardrails(self):
        cache = RouterCache()
        snapshot = make_snapshot()
        raw_miss = _decision(
            outcome="conversation",
            domains=[],
            reasoning="classifier miss",
        )
        first_client = _Client(raw_miss)
        first = RouterToolSelector(
            first_client,
            snapshot,
            use_fast_path=False,
            router_cache=cache,
        )
        first.select_schemas("show my tasks", _registry())

        second_client = _Client(_decision(domains=["google_calendar"]))
        second = RouterToolSelector(
            second_client,
            snapshot,
            use_fast_path=False,
            router_cache=cache,
        )
        second.select_schemas("show my tasks", _registry())

        assert first.decision.outcome == second.decision.outcome == "routed"
        assert first.decision.domains == second.decision.domains == ["todoist"]
        assert second_client.sync_calls == 0

    def test_uncertain_result_does_not_cross_selector_boundary(self):
        cache = RouterCache()
        snapshot = make_snapshot()
        uncertain = _decision(
            uncertain=True,
            candidates=["todoist", "google_calendar"],
        )
        first_client = _Client(uncertain)
        RouterToolSelector(
            first_client,
            snapshot,
            use_fast_path=False,
            router_cache=cache,
        ).select_schemas("ambiguous request", _registry())

        second_client = _Client(_decision())
        RouterToolSelector(
            second_client,
            snapshot,
            use_fast_path=False,
            router_cache=cache,
        ).select_schemas("ambiguous request", _registry())

        assert first_client.sync_calls == 1
        assert second_client.sync_calls == 1
