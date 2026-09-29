"""Production router selector contract after the custom-instructions cutover."""

import asyncio

from agents.agent_api.app.router.client import RouterClientError
from agents.agent_api.app.router.prompt import RouterDecision
from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.selectors.router import RouterToolSelector
from agents.agent_api.app.tracing import TracePrinter
from tests.agents.runtime_helpers import make_snapshot

_TODOIST_TOOLS = {"add_todoist_task", "get_tasks"}
_CALENDAR_TOOLS = {"list_calendar_events", "delete_calendar_event"}
_ALL_TOOLS = {"ask_user", *_TODOIST_TOOLS, *_CALENDAR_TOOLS}


def _registry() -> ToolRegistry:
    specs = [
        ToolSpec(name=name, openai_schema={"type": "function", "function": {"name": name}})
        for name in sorted(_ALL_TOOLS)
    ]
    return ToolRegistry().register(specs)


def _names(schemas):
    return {schema["function"]["name"] for schema in schemas}


def _decision(*domains: str, outcome: str = "routed") -> RouterDecision:
    return RouterDecision(
        outcome=outcome,
        domains=list(domains),
        uncertain=False,
        candidate_domains=[],
        complexity="low",
    )


class FakeRouterClient:
    def __init__(self, decisions=None, error=None):
        self.decisions = decisions or {}
        self.error = error
        self.calls = []
        self.async_calls = []

    def classify(self, query, snapshot):
        del snapshot
        self.calls.append(query)
        if self.error:
            raise self.error
        return self.decisions.get(query) or next(iter(self.decisions.values()))

    async def async_classify(self, query, snapshot, **kwargs):
        del snapshot, kwargs
        self.async_calls.append(query)
        if self.error:
            raise self.error
        return self.decisions.get(query) or next(iter(self.decisions.values()))


class RecordingTracer(TracePrinter):
    def __init__(self):
        super().__init__(enabled=False)
        self.events = []

    def event(self, stage, message, **fields):
        self.events.append((stage, message, fields))


def test_filters_exact_router_domains_without_preference_guardrails():
    client = FakeRouterClient({"tasks": _decision("todoist")})
    selector = RouterToolSelector(client, make_snapshot())

    assert _names(selector.select_schemas("tasks", _registry())) == {
        "ask_user",
        *_TODOIST_TOOLS,
    }
    assert client.calls == ["tasks"]


def test_multi_domain_and_conversation_routes():
    client = FakeRouterClient(
        {
            "both": _decision("todoist", "google_calendar"),
            "none": _decision(outcome="conversation"),
        }
    )
    selector = RouterToolSelector(client, make_snapshot())

    assert _names(selector.select_schemas("both", _registry())) == _ALL_TOOLS
    assert _names(selector.select_schemas("none", _registry())) == {"ask_user"}


def test_uncertain_route_uses_safe_candidate_domains():
    decision = RouterDecision(
        outcome="ambiguous",
        domains=[],
        uncertain=True,
        candidate_domains=["todoist", "google_calendar"],
        complexity="medium",
    )
    selector = RouterToolSelector(FakeRouterClient({"q": decision}), make_snapshot())

    assert _names(selector.select_schemas("q", _registry())) == _ALL_TOOLS


def test_same_run_memoizes_each_unique_query_not_only_the_latest():
    client = FakeRouterClient(
        {"a": _decision("todoist"), "b": _decision("google_calendar")}
    )
    selector = RouterToolSelector(client, make_snapshot())

    selector.select_schemas("a", _registry())
    selector.select_schemas("b", _registry())
    selector.select_schemas("a", _registry())

    assert client.calls == ["a", "b"]


def test_separate_selector_instances_do_not_share_decisions():
    first = FakeRouterClient({"q": _decision("todoist")})
    second = FakeRouterClient({"q": _decision("todoist")})

    RouterToolSelector(first, make_snapshot()).select_schemas("q", _registry())
    RouterToolSelector(second, make_snapshot()).select_schemas("q", _registry())

    assert first.calls == ["q"]
    assert second.calls == ["q"]


def test_router_failure_falls_back_to_all_connected_tools_and_clears_decision():
    error = RouterClientError(
        {"source": "router", "type": "timeout", "retryable": True, "attempts": 2}
    )
    client = FakeRouterClient(error=error)
    selector = RouterToolSelector(client, make_snapshot())

    assert _names(selector.select_schemas("q", _registry())) == _ALL_TOOLS
    assert selector.decision is None


def test_router_failure_is_memoized_within_run_for_same_query():
    error = RouterClientError(
        {"source": "router", "type": "invalid_response", "retryable": False, "attempts": 1}
    )
    client = FakeRouterClient(error=error)
    selector = RouterToolSelector(client, make_snapshot())

    selector.select_schemas("q", _registry())
    selector.select_schemas("q", _registry())

    assert client.calls == ["q"]


def test_pinned_domains_are_merged_except_for_exit():
    client = FakeRouterClient(
        {
            "redirect": _decision("todoist"),
            "cancel": _decision(outcome="conversation"),
        }
    )
    selector = RouterToolSelector(client, make_snapshot())

    assert _names(
        selector.select_schemas(
            "redirect", _registry(), active_domains=["google_calendar"]
        )
    ) == _ALL_TOOLS
    assert _names(
        selector.select_schemas(
            "cancel", _registry(), active_domains=["google_calendar"]
        )
    ) == {"ask_user"}


def test_async_selection_memoizes_and_uses_async_client():
    client = FakeRouterClient({"q": _decision("google_calendar")})
    selector = RouterToolSelector(client, make_snapshot())

    async def run():
        first = await selector.async_select_schemas("q", _registry())
        second = await selector.async_select_schemas("q", _registry())
        return first, second

    first, second = asyncio.run(run())
    assert _names(first) == _names(second) == {"ask_user", *_CALENDAR_TOOLS}
    assert client.async_calls == ["q"]
    assert client.calls == []


def test_trace_distinguishes_live_call_and_run_local_cache():
    tracer = RecordingTracer()
    selector = RouterToolSelector(
        FakeRouterClient({"q": _decision("todoist")}),
        make_snapshot(),
        tracer=tracer,
    )

    selector.select_schemas("q", _registry())
    selector.select_schemas("q", _registry())

    stages = [stage for stage, _message, _fields in tracer.events]
    assert stages.count("router.start") == 1
    assert stages.count("router.cache_hit") == 1
    assert "router.fast_path" not in stages
    assert "router.lru_cache_hit" not in stages
