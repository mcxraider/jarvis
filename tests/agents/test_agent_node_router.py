"""Agent-node integration of the router decision: prompt slimming + history safety.

These tests drive create_agent_node directly with a fake decision-carrying
selector and a recording LLM client, so they verify the node's wiring (slim
messages[0], preserve tool history) without any real DeepSeek or LangGraph run.
"""

import asyncio
import os
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

# Disable tracing before importing anything that touches LangSmith/LangChain.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

with patch("langsmith.wrappers.wrap_openai", side_effect=lambda c, **_: c):
    from agents.agent_api.app.graph.nodes.orchestrator import (
        DeepSeekAgentClient,
        UsageSummary,
        create_agent_node,
    )

from agents.agent_api.app.graph.prompts.context import build_initial_messages
from agents.agent_api.app.router.model_router import create_default_model_router
from agents.agent_api.app.router.prompt import RouterDecision
from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.selectors.router import RouterToolSelector
from agents.agent_api.app.tools.selectors.static import StaticToolSelector
from tests.agents.runtime_helpers import make_snapshot


class _CannedRouterClient:
    """A RouterClient stand-in whose classify() returns a fixed decision."""

    def __init__(self, decision) -> None:
        self._decision = decision
        self.seen_queries: List[str] = []

    def classify(self, query, snapshot):
        self.seen_queries.append(query)
        return self._decision

SENTINEL_TOOL_RESULT = "SENTINEL_TOOL_RESULT_do_not_lose_me"


def _registry() -> ToolRegistry:
    specs = [
        ToolSpec(name=name, openai_schema={"type": "function", "function": {"name": name}})
        for name in ("ask_user", "add_todoist_task", "get_tasks", "list_calendar_events")
    ]
    return ToolRegistry().register(specs)


class RecordingClient:
    """Captures the messages passed to create_message; ends the turn (no tool calls)."""

    def __init__(self) -> None:
        self.seen_messages: List[Dict[str, Any]] = []
        self.seen_tools: List[Dict[str, Any]] = []
        self.seen_kwargs: Dict[str, Any] = {}

    def create_message(self, messages, tools, **kwargs):
        # Snapshot the list as passed: the node appends the assistant reply to the
        # same list object afterwards, so holding a reference would corrupt [-1].
        self.seen_messages = list(messages)
        self.seen_tools = list(tools)
        self.seen_kwargs = dict(kwargs)
        return {"role": "assistant", "content": "done"}


class FakeDecisionSelector:
    """Selector that exposes a router decision and passes through all tools."""

    def __init__(self, decision) -> None:
        self._decision = decision
        self.seen_queries: List[str] = []

    @property
    def decision(self):
        return self._decision

    def select_schemas(self, query, registry, **kwargs):
        self.seen_queries.append(query)
        return registry.openai_schemas()


def _state_with_history(snapshot, *, turn_count=1):
    """A mid-run state: system + user, then an assistant tool-call + tool result."""
    messages = build_initial_messages("add buy milk", runtime_context=snapshot)
    messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_tasks", "arguments": "{}"}}
            ],
        }
    )
    messages.append({"role": "tool", "tool_call_id": "c1", "content": SENTINEL_TOOL_RESULT})
    return {
        "messages": messages,
        "user_prompt": "add buy milk",
        "turn_count": turn_count,
        "runtime_context": snapshot.model_dump(mode="json"),
    }


def _run_node(state, selector):
    client = RecordingClient()
    node = create_agent_node(client, _registry(), max_agent_turns=20, tool_selector=selector)
    result = asyncio.run(node(state))
    return client, result


class TestPromptSlimming:
    def test_decision_slims_system_prompt_to_routed_domain(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_with_history(snapshot)
        selector = FakeDecisionSelector(RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low"))

        client, _result = _run_node(state, selector)

        system = client.seen_messages[0]["content"]
        assert "## Todoist tool tips" in system
        assert "## Google Calendar tool tips" not in system

    def test_tool_history_survives_the_turn(self):
        """Slimming rebuilds only messages[0]; the tool result must remain."""
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_with_history(snapshot)
        selector = FakeDecisionSelector(RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low"))

        client, result = _run_node(state, selector)

        # The pre-seeded tool message is still in the list sent to the LLM...
        assert any(
            m.get("content") == SENTINEL_TOOL_RESULT for m in client.seen_messages
        )
        # ...and in the state the node returns.
        assert any(m.get("content") == SENTINEL_TOOL_RESULT for m in result["messages"])

    def test_empty_decision_slims_to_no_domain_fragments(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_with_history(snapshot)
        selector = FakeDecisionSelector(RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], complexity="low"))

        client, _result = _run_node(state, selector)

        system = client.seen_messages[0]["content"]
        assert "## Todoist tool tips" not in system
        assert "## Google Calendar tool tips" not in system
        # Availability summary still present so the model knows what exists.
        assert "- Todoist: available" in system


def _state_turn0(snapshot, user_prompt="add buy milk", reply_context=None):
    """A fresh turn-0 state: exactly [system, user], no tool history yet."""
    return {
        "messages": build_initial_messages(
            user_prompt,
            runtime_context=snapshot,
            reply_context=reply_context,
        ),
        "user_prompt": user_prompt,
        "reply_context": reply_context,
        "turn_count": 0,
        "runtime_context": snapshot.model_dump(mode="json"),
    }


class TestOriginalQueryPreservation:
    def test_original_request_is_unchanged_on_turn_zero(self):
        snapshot = make_snapshot(active=("todoist",))
        state = _state_turn0(snapshot)
        original_user = state["messages"][-1]["content"]
        selector = FakeDecisionSelector(RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low"))

        client, _result = _run_node(state, selector)

        assert client.seen_messages[-1]["content"] == original_user

    def test_no_reply_context_keeps_raw_router_query(self):
        snapshot = make_snapshot(active=("todoist",))
        selector = FakeDecisionSelector(RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low"))

        _run_node(_state_turn0(snapshot), selector)

        assert selector.seen_queries == ["add buy milk"]


class TestEndToEndThroughRealSelector:
    """The real RouterToolSelector + agent_node compose: filter and slim."""

    def test_router_selector_filters_and_slims_in_one_turn(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_turn0(snapshot)
        decision = RouterDecision(
            outcome="routed", domains=["todoist"], uncertain=False,
            candidate_domains=[], complexity="low",
        )
        selector = RouterToolSelector(
            router_client=_CannedRouterClient(decision),
            snapshot=snapshot,
        )

        client, _result = _run_node(state, selector)

        # (2) tool schemas narrowed to the routed domain + ask_user
        tool_names = {t["function"]["name"] for t in client.seen_tools}
        assert tool_names == {"ask_user", "add_todoist_task", "get_tasks"}
        # (4) system prompt slimmed to the routed domain
        system = client.seen_messages[0]["content"]
        assert "## Todoist tool tips" in system
        assert "## Google Calendar tool tips" not in system
        assert "Available tools: ask_user, add_todoist_task, get_tasks" in system
        assert "list_calendar_events" not in system
        assert "add buy milk" in client.seen_messages[-1]["content"]

    def test_reply_context_reaches_router_and_routes_referential_request(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        reply_context = {
            "role": "assistant",
            "message": "Created task: Buy milk",
        }
        state = _state_turn0(
            snapshot,
            user_prompt="make it due tomorrow",
            reply_context=reply_context,
        )
        router_client = _CannedRouterClient(
            RouterDecision(
                outcome="routed",
                domains=["todoist"],
                uncertain=False,
                candidate_domains=[],
                complexity="low",
            )
        )
        selector = RouterToolSelector(
            router_client=router_client,
            snapshot=snapshot,
            use_fast_path=False,
            use_lru_cache=False,
        )

        client, _result = _run_node(state, selector)

        assert router_client.seen_queries == [
            "Reply context:\n"
            "- Replied-to role: assistant\n"
            "- Replied-to message: Created task: Buy milk\n\n"
            "Current user message:\n"
            "make it due tomorrow"
        ]
        assert {tool["function"]["name"] for tool in client.seen_tools} == {
            "ask_user",
            "add_todoist_task",
            "get_tasks",
        }
        assert "Reply context:" in client.seen_messages[-1]["content"]
        assert "Created task: Buy milk" in client.seen_messages[-1]["content"]

    def test_calendar_route_does_not_advertise_todoist_tools(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_turn0(snapshot, user_prompt="what's on my google calendar")
        selector = RouterToolSelector(
            router_client=_CannedRouterClient(RouterDecision(outcome="routed", domains=["google_calendar"], uncertain=False, candidate_domains=[], complexity="low")),
            snapshot=snapshot,
        )

        client, _result = _run_node(state, selector)

        system = client.seen_messages[0]["content"]
        assert "Available tools: ask_user, list_calendar_events" in system
        assert "add_todoist_task" not in system
        assert "get_tasks" not in system
        assert "## Google Calendar tool tips" in system
        assert "## Todoist tool tips" not in system

    def test_todoist_calendar_clarification_does_not_advertise_google_calendar(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_turn0(snapshot, user_prompt="check my todoist calendar")
        selector = RouterToolSelector(
            router_client=_CannedRouterClient(RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], complexity="low")),
            snapshot=snapshot,
        )

        client, _result = _run_node(state, selector)

        tool_names = {t["function"]["name"] for t in client.seen_tools}
        assert tool_names == {"ask_user", "add_todoist_task", "get_tasks"}
        system = client.seen_messages[0]["content"]
        assert "## Todoist tool tips" in system
        assert "## Google Calendar tool tips" not in system
        assert "list_calendar_events" not in system

    def test_uncertain_candidates_keep_tools_and_prompt_aligned(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_turn0(snapshot, user_prompt="ambiguous planning request")
        decision = RouterDecision(
            outcome="routed",
            domains=["todoist"],
            uncertain=True,
            candidate_domains=["todoist", "google_calendar"],
            complexity="low",
        )
        selector = RouterToolSelector(
            router_client=_CannedRouterClient(decision),
            snapshot=snapshot,
        )

        client, _result = _run_node(state, selector)

        tool_names = {t["function"]["name"] for t in client.seen_tools}
        assert tool_names == {
            "ask_user",
            "add_todoist_task",
            "get_tasks",
            "list_calendar_events",
        }
        system = client.seen_messages[0]["content"]
        assert "## Todoist tool tips" in system
        assert "## Google Calendar tool tips" in system
        assert "Available tools: ask_user, add_todoist_task, get_tasks, list_calendar_events" in system

    def test_selected_tool_names_are_returned_in_state(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_turn0(snapshot)
        selector = RouterToolSelector(
            router_client=_CannedRouterClient(RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low")),
            snapshot=snapshot,
        )

        _client, result = _run_node(state, selector)

        assert result["selected_tool_names"] == ["ask_user", "add_todoist_task", "get_tasks"]
        assert result["router_outcome"] == "routed"


class TestNoSlimming:
    def test_selector_without_decision_leaves_prompt_intact(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_with_history(snapshot)
        original_system = state["messages"][0]["content"]

        client, _result = _run_node(state, StaticToolSelector())

        assert client.seen_messages[0]["content"] == original_system
        # Both domains' fragments intact (today's behavior).
        assert "## Todoist tool tips" in client.seen_messages[0]["content"]
        assert "## Google Calendar tool tips" in client.seen_messages[0]["content"]

    def test_missing_runtime_context_skips_slimming(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_with_history(snapshot)
        state["runtime_context"] = {}  # no snapshot -> no slimming
        original_system = state["messages"][0]["content"]
        selector = FakeDecisionSelector(RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low"))

        client, _result = _run_node(state, selector)

        assert client.seen_messages[0]["content"] == original_system


class TestComplexityModelRouting:
    def test_high_complexity_single_domain_overrides_model_and_reasoning(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_turn0(snapshot)
        selector = FakeDecisionSelector(
            RouterDecision(
                outcome="routed",
                domains=["todoist"],
                uncertain=False,
                candidate_domains=[],
                complexity="high",
            )
        )
        client = RecordingClient()
        node = create_agent_node(
            client,
            _registry(),
            max_agent_turns=20,
            tool_selector=selector,
            model_router=create_default_model_router(
                default_model="flash",
                default_reasoning="high",
                complex_model="pro",
                complex_reasoning="max",
            ),
        )

        asyncio.run(node(state))

        assert client.seen_kwargs["model"] == "pro"
        assert client.seen_kwargs["reasoning_effort"] == "max"
        assert client.seen_kwargs["request_timeout_seconds"] == 90.0


class TestAsyncCompatibility:
    def test_deepseek_node_uses_async_client_and_run_usage_accumulator(self):
        client = DeepSeekAgentClient(
            api_key="test-key",
            client=MagicMock(),
            async_client=MagicMock(),
        )
        client.create_message = MagicMock(side_effect=AssertionError("sync path used"))
        client.async_create_message = AsyncMock(
            return_value={"role": "assistant", "content": "done"}
        )
        usage = UsageSummary()
        node = create_agent_node(
            client,
            _registry(),
            max_agent_turns=20,
            tool_selector=FakeDecisionSelector(
                RouterDecision(
                    outcome="routed",
                    domains=["todoist"],
                    uncertain=False,
                    candidate_domains=[],
                    complexity="low",
                )
            ),
            usage_accumulator=usage,
        )

        result = asyncio.run(node(_state_turn0(make_snapshot(active=("todoist",)))))

        assert result["final_response"] == "done"
        client.async_create_message.assert_awaited_once()
        assert client.async_create_message.await_args.kwargs["usage_accumulator"] is usage
        client.create_message.assert_not_called()

    def test_injected_deepseek_without_async_transport_keeps_configured_sync_client(self):
        client = DeepSeekAgentClient(api_key="test-key", client=MagicMock())
        client.create_message = MagicMock(
            return_value={"role": "assistant", "content": "configured"}
        )
        client.async_create_message = AsyncMock(
            side_effect=AssertionError("global async path used")
        )
        node = create_agent_node(
            client,
            _registry(),
            max_agent_turns=20,
            tool_selector=FakeDecisionSelector(
                RouterDecision(
                    outcome="routed",
                    domains=["todoist"],
                    uncertain=False,
                    candidate_domains=[],
                    complexity="low",
                )
            ),
        )

        result = asyncio.run(node(_state_turn0(make_snapshot(active=("todoist",)))))

        assert result["final_response"] == "configured"
        client.create_message.assert_called_once()
        client.async_create_message.assert_not_awaited()

    def test_magicmock_async_attributes_do_not_bypass_sync_compatibility(self):
        client = MagicMock()
        client.create_message.return_value = {"role": "assistant", "content": "done"}
        selector = MagicMock()
        selector.select_schemas.return_value = _registry().openai_schemas()
        selector.decision = None
        node = create_agent_node(
            client,
            _registry(),
            max_agent_turns=20,
            tool_selector=selector,
        )

        result = asyncio.run(node(_state_turn0(make_snapshot(active=("todoist",)))))

        assert result["final_response"] == "done"
        selector.select_schemas.assert_called_once()
        client.create_message.assert_called_once()
