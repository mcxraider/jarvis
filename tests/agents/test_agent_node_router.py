"""Agent-node integration of the router decision: prompt slimming + history safety.

These tests drive create_agent_node directly with a fake decision-carrying
selector and a recording LLM client, so they verify the node's wiring (slim
messages[0], preserve tool history) without any real DeepSeek or LangGraph run.
"""

import os
from typing import Any, Dict, List
from unittest.mock import patch

# Disable tracing before importing anything that touches LangSmith/LangChain.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

with patch("langsmith.wrappers.wrap_openai", side_effect=lambda c: c):
    from agents.agent_api.app.graph.nodes.orchestrator import create_agent_node

from agents.agent_api.app.graph.prompts.context import build_initial_messages
from agents.agent_api.app.router.prompt import RouterDecision
from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.selectors.router import RouterToolSelector
from agents.agent_api.app.tools.selectors.static import StaticToolSelector
from tests.agents.runtime_helpers import make_snapshot


class _CannedRouterClient:
    """A RouterClient stand-in whose classify() returns a fixed decision."""

    def __init__(self, decision) -> None:
        self._decision = decision

    def classify(self, query, snapshot):
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

    def create_message(self, messages, tools, **kwargs):
        # Snapshot the list as passed: the node appends the assistant reply to the
        # same list object afterwards, so holding a reference would corrupt [-1].
        self.seen_messages = list(messages)
        self.seen_tools = list(tools)
        return {"role": "assistant", "content": "done"}


class FakeDecisionSelector:
    """Selector that exposes a router decision and passes through all tools."""

    def __init__(self, decision) -> None:
        self._decision = decision

    @property
    def decision(self):
        return self._decision

    def select_schemas(self, query, registry, **kwargs):
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
    result = node(state)
    return client, result


class TestPromptSlimming:
    def test_decision_slims_system_prompt_to_routed_domain(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_with_history(snapshot)
        selector = FakeDecisionSelector(RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], reasoning="test"))

        client, _result = _run_node(state, selector)

        system = client.seen_messages[0]["content"]
        assert "## Todoist tool tips" in system
        assert "## Google Calendar tool tips" not in system

    def test_tool_history_survives_the_turn(self):
        """Slimming rebuilds only messages[0]; the tool result must remain."""
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_with_history(snapshot)
        selector = FakeDecisionSelector(RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], reasoning="test"))

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
        selector = FakeDecisionSelector(RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], reasoning="test"))

        client, _result = _run_node(state, selector)

        system = client.seen_messages[0]["content"]
        assert "## Todoist tool tips" not in system
        assert "## Google Calendar tool tips" not in system
        # Availability summary still present so the model knows what exists.
        assert "- Todoist: available" in system


def _state_turn0(snapshot, user_prompt="add buy milk"):
    """A fresh turn-0 state: exactly [system, user], no tool history yet."""
    return {
        "messages": build_initial_messages(user_prompt, runtime_context=snapshot),
        "user_prompt": user_prompt,
        "turn_count": 0,
        "runtime_context": snapshot.model_dump(mode="json"),
    }


class TestOriginalQueryPreservation:
    def test_original_request_is_unchanged_on_turn_zero(self):
        snapshot = make_snapshot(active=("todoist",))
        state = _state_turn0(snapshot)
        original_user = state["messages"][-1]["content"]
        selector = FakeDecisionSelector(RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], reasoning="test"))

        client, _result = _run_node(state, selector)

        assert client.seen_messages[-1]["content"] == original_user

class TestEndToEndThroughRealSelector:
    """The real RouterToolSelector + agent_node compose: filter and slim."""

    def test_router_selector_filters_and_slims_in_one_turn(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_turn0(snapshot)
        decision = RouterDecision(
            outcome="routed", domains=["todoist"], uncertain=False,
            candidate_domains=[], reasoning="test",
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

    def test_calendar_route_does_not_advertise_todoist_tools(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = _state_turn0(snapshot, user_prompt="what's on my google calendar")
        selector = RouterToolSelector(
            router_client=_CannedRouterClient(RouterDecision(outcome="routed", domains=["google_calendar"], uncertain=False, candidate_domains=[], reasoning="test")),
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
            router_client=_CannedRouterClient(RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], reasoning="test")),
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
            reasoning="test",
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
            router_client=_CannedRouterClient(RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], reasoning="test")),
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
        selector = FakeDecisionSelector(RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], reasoning="test"))

        client, _result = _run_node(state, selector)

        assert client.seen_messages[0]["content"] == original_system
