"""Integration tests for router context preservation across HITL resumes.

Validates the 6-stage fix for domain loss:
  Stage 1: active_domains persisted in state after turn 1
  Stage 2: Contextual routing query composed for resumes
  Stage 3: Domain merging in RouterToolSelector
  Stage 4: Prompt slimming respects pinned domains
  Stage 5: Self-healing route expansion in validate_entities
  Stage 6: End-to-end (this file)
"""

import asyncio
import os
from typing import Any, Dict, List, Optional
from unittest.mock import patch

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

with patch("langsmith.wrappers.wrap_openai", side_effect=lambda c: c):
    from agents.agent_api.app.graph.nodes.orchestrator import create_agent_node
    from agents.agent_api.app.graph.nodes.validate_entities import (
        create_validate_entities_node,
    )

from agents.agent_api.app.graph.prompts.context import build_initial_messages
from agents.agent_api.app.router.prompt import RouterDecision, effective_router_domains
from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.selectors.router import RouterToolSelector
from agents.agent_api.app.tracing import NULL_TRACE
from tests.agents.runtime_helpers import make_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _CannedRouterClient:
    """RouterClient that returns fixed decisions per query."""

    def __init__(self, decisions: Dict[str, RouterDecision]) -> None:
        self._decisions = decisions
        self._default = RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")
        self.seen_queries: List[str] = []

    def classify(self, query: str, snapshot: Any) -> RouterDecision:
        self.seen_queries.append(query)
        for key, decision in self._decisions.items():
            if key in query:
                return decision
        return self._default


class _RecordingClient:
    """Captures messages and tools passed to create_message."""

    def __init__(self, response: Optional[Dict[str, Any]] = None) -> None:
        self.seen_messages: List[Dict[str, Any]] = []
        self.seen_tools: List[Dict[str, Any]] = []
        self._response = response or {"role": "assistant", "content": "done"}

    def create_message(self, messages, tools, **kwargs):
        self.seen_messages = list(messages)
        self.seen_tools = list(tools)
        return self._response


def _registry() -> ToolRegistry:
    specs = [
        ToolSpec(name=name, openai_schema={"type": "function", "function": {"name": name}})
        for name in (
            "ask_user",
            "add_todoist_task",
            "get_tasks",
            "list_calendar_events",
            "create_calendar_event",
            "delete_calendar_event",
        )
    ]
    return ToolRegistry().register(specs)


def _fresh_state(snapshot, user_prompt="add event to google calendar", **overrides):
    """Build a fresh turn-0 state with the given snapshot."""
    state = {
        "messages": build_initial_messages(user_prompt, runtime_context=snapshot),
        "user_prompt": user_prompt,
        "turn_count": 0,
        "runtime_context": snapshot.model_dump(mode="json"),
    }
    state.update(overrides)
    return state


def _resume_state(snapshot, user_prompt, clarification_history, active_domains, **overrides):
    """Build a state simulating a HITL resume turn."""
    state = {
        "messages": build_initial_messages(user_prompt, runtime_context=snapshot),
        "user_prompt": user_prompt,
        "turn_count": 1,
        "runtime_context": snapshot.model_dump(mode="json"),
        "clarification_history": clarification_history,
        "active_domains": active_domains,
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Stage 3: Domain Merging in RouterToolSelector
# ---------------------------------------------------------------------------


class TestDomainMerging:
    """RouterToolSelector merges active_domains as a floor on resume turns."""

    def test_merge_preserves_pinned_domain_on_resume(self):
        """active_domains=["google_calendar"], router returns ["todoist"]
        → effective tools include both domains."""
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        router_client = _CannedRouterClient({"events": RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")})
        selector = RouterToolSelector(router_client=router_client, snapshot=snapshot)

        schemas = selector.select_schemas(
            "find events from this week",
            _registry(),
            active_domains=["google_calendar"],
        )

        tool_names = {s["function"]["name"] for s in schemas}
        assert "list_calendar_events" in tool_names
        assert "add_todoist_task" in tool_names
        assert "ask_user" in tool_names

    def test_no_merge_on_fresh_invocation(self):
        """active_domains=None → only router-selected domains."""
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        router_client = _CannedRouterClient({})  # default → todoist
        selector = RouterToolSelector(router_client=router_client, snapshot=snapshot)

        schemas = selector.select_schemas("show my tasks", _registry(), active_domains=None)

        tool_names = {s["function"]["name"] for s in schemas}
        assert "add_todoist_task" in tool_names
        assert "list_calendar_events" not in tool_names

    def test_exit_clears_domains(self):
        """Router returns [] and reply contains "exit" → no merge, only ask_user."""
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        router_client = _CannedRouterClient({"exit": RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")})
        selector = RouterToolSelector(router_client=router_client, snapshot=snapshot)

        schemas = selector.select_schemas(
            "exit",
            _registry(),
            active_domains=["google_calendar"],
        )

        tool_names = {s["function"]["name"] for s in schemas}
        assert tool_names == {"ask_user"}

    def test_disconnected_domain_not_merged(self):
        """active_domains=["google_calendar"] but GCal disconnected → not merged."""
        snapshot = make_snapshot(
            active=("todoist",),
            unavailable={"google_calendar": "token expired"},
        )
        router_client = _CannedRouterClient({})
        selector = RouterToolSelector(router_client=router_client, snapshot=snapshot)

        schemas = selector.select_schemas(
            "find events",
            _registry(),
            active_domains=["google_calendar"],
        )

        tool_names = {s["function"]["name"] for s in schemas}
        assert "list_calendar_events" not in tool_names


# ---------------------------------------------------------------------------
# Stage 1 + 2: Orchestrator sets active_domains and composes contextual query
# ---------------------------------------------------------------------------


class TestOrchestratorActiveDomains:
    """The agent node persists active_domains on turn 1 and composes contextual queries."""

    def test_active_domains_set_on_first_turn(self):
        """Turn 1 with router → active_domains populated from decision."""
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        decision = RouterDecision(outcome="routed", domains=["google_calendar"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")
        router_client = _CannedRouterClient({"google calendar": decision})
        selector = RouterToolSelector(router_client=router_client, snapshot=snapshot)
        client = _RecordingClient()
        node = create_agent_node(client, _registry(), max_agent_turns=20, tool_selector=selector)

        state = _fresh_state(
            snapshot,
            user_prompt="add event to google calendar",
        )
        result = asyncio.run(node(state))

        assert "google_calendar" in result.get("active_domains", [])

    def test_active_domains_not_overwritten_on_resume(self):
        """Resume turn (clarification_history present) does not overwrite active_domains."""
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        # Router returns todoist on resume, but active_domains already set
        router_client = _CannedRouterClient({})  # default todoist
        selector = RouterToolSelector(router_client=router_client, snapshot=snapshot)
        client = _RecordingClient()
        node = create_agent_node(client, _registry(), max_agent_turns=20, tool_selector=selector)

        state = _resume_state(
            snapshot,
            user_prompt="add event to google calendar",
            clarification_history=[{"question": "Which day?", "reply": "next friday"}],
            active_domains=["google_calendar"],
        )
        result = asyncio.run(node(state))

        # active_domains should NOT be overwritten (already set)
        assert result.get("active_domains") is None or result.get("active_domains") == ["google_calendar"]

    def test_contextual_query_includes_original_prompt(self):
        """On resume, the routing query sent to the selector includes the original prompt."""
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        # Router returns google_calendar when it sees "google calendar" in query
        decision = RouterDecision(outcome="routed", domains=["google_calendar"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")
        router_client = _CannedRouterClient({"google calendar": decision})
        selector = RouterToolSelector(router_client=router_client, snapshot=snapshot)
        client = _RecordingClient()
        node = create_agent_node(client, _registry(), max_agent_turns=20, tool_selector=selector)

        state = _resume_state(
            snapshot,
            user_prompt="add event to google calendar next friday",
            clarification_history=[
                {"question": "What time?", "reply": "3pm"},
            ],
            active_domains=["google_calendar"],
        )
        asyncio.run(node(state))

        # The router should have seen "google calendar" (from user_prompt) in the
        # composed query, so it should return google_calendar decision.
        assert selector.decision is not None
        assert "google_calendar" in effective_router_domains(selector.decision)

    def test_clarification_query_excludes_persisted_telegram_reply_context(self):
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        router_client = _CannedRouterClient({})
        selector = RouterToolSelector(
            router_client=router_client,
            snapshot=snapshot,
            use_fast_path=False,
            use_lru_cache=False,
        )
        client = _RecordingClient()
        node = create_agent_node(client, _registry(), max_agent_turns=20, tool_selector=selector)
        state = _resume_state(
            snapshot,
            user_prompt="make it due tomorrow",
            clarification_history=[
                {"question": "Which task?", "reply": "Buy milk"},
            ],
            active_domains=["todoist"],
            reply_context={
                "role": "assistant",
                "message": "Created task: Buy milk",
            },
        )

        asyncio.run(node(state))

        assert router_client.seen_queries == [
            "make it due tomorrow "
            "[You asked: Which task?] "
            "[User replied: Buy milk]"
        ]
        assert "Reply context:" not in router_client.seen_queries[0]


# ---------------------------------------------------------------------------
# Stage 4: Prompt slimming respects pinned domains
# ---------------------------------------------------------------------------


class TestPromptSlimmingPinnedDomains:
    """System prompt includes instructions for pinned domains, not just router-selected."""

    def test_pinned_domain_instructions_in_system_prompt(self):
        """Router routes todoist only, but active_domains has google_calendar
        → system prompt includes Google Calendar instructions."""
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        # Router says todoist only for "next friday" reply
        router_client = _CannedRouterClient({"google calendar": RouterDecision(outcome="routed", domains=["google_calendar"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")})
        selector = RouterToolSelector(router_client=router_client, snapshot=snapshot)
        client = _RecordingClient()
        node = create_agent_node(client, _registry(), max_agent_turns=20, tool_selector=selector)

        state = _resume_state(
            snapshot,
            user_prompt="add event to google calendar next friday",
            clarification_history=[{"question": "What time?", "reply": "3pm"}],
            active_domains=["google_calendar"],
        )
        asyncio.run(node(state))

        system = client.seen_messages[0]["content"]
        # Active domains should produce Calendar instructions in the prompt
        assert "Google Calendar" in system or "google_calendar" in system.lower()


# ---------------------------------------------------------------------------
# Stage 5: Self-healing route expansion in validate_entities
# ---------------------------------------------------------------------------


class TestRouteExpansion:
    """validate_entities allows out-of-route tools from pinned active_domains."""

    def _make_state_with_tool_call(
        self,
        tool_call_name: str,
        selected_tool_names: List[str],
        active_domains: List[str],
        snapshot=None,
    ) -> Dict[str, Any]:
        snapshot = snapshot or make_snapshot(active=("todoist", "google_calendar"))
        return {
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "request"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": tool_call_name, "arguments": "{}"},
                        }
                    ],
                },
            ],
            "selected_tool_names": selected_tool_names,
            "active_domains": active_domains,
            "runtime_context": snapshot.model_dump(mode="json"),
            "tool_results": [],
        }

    def test_pinned_domain_tool_allowed_through(self):
        """list_calendar_events called, selected=todoist-only, active_domains has google_calendar
        → NOT rejected."""
        state = self._make_state_with_tool_call(
            tool_call_name="list_calendar_events",
            selected_tool_names=["ask_user", "add_todoist_task", "get_tasks"],
            active_domains=["google_calendar"],
        )
        node = create_validate_entities_node(tracer=NULL_TRACE)
        result = asyncio.run(node(state))

        # Should NOT redirect to agent (rejection). Should proceed to tools or confirm.
        assert result.get("next") in ("tools", "confirm")
        # No synthetic rejection messages added
        if "messages" in result:
            for msg in result["messages"]:
                if msg.get("role") == "tool":
                    content = msg.get("content", "")
                    assert "was not selected for this turn" not in content

    def test_unknown_tool_still_rejected(self):
        """A tool not in any domain → still rejected normally."""
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        state = {
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "request"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "send_email", "arguments": "{}"},
                        }
                    ],
                },
            ],
            "selected_tool_names": ["ask_user", "add_todoist_task"],
            "active_domains": ["google_calendar"],
            "runtime_context": snapshot.model_dump(mode="json"),
            "tool_results": [],
        }
        node = create_validate_entities_node(tracer=NULL_TRACE)
        result = asyncio.run(node(state))

        assert result.get("next") == "agent"

    def test_empty_active_domains_rejects_normally(self):
        """No active_domains → out-of-route tools rejected as before."""
        state = self._make_state_with_tool_call(
            tool_call_name="list_calendar_events",
            selected_tool_names=["ask_user", "add_todoist_task", "get_tasks"],
            active_domains=[],
        )
        node = create_validate_entities_node(tracer=NULL_TRACE)
        result = asyncio.run(node(state))

        assert result.get("next") == "agent"


# ---------------------------------------------------------------------------
# Stage 6: End-to-end scenario — HITL resume preserves google_calendar
# ---------------------------------------------------------------------------


class TestEndToEndContextPreservation:
    """Full pipeline: router → orchestrator → validate_entities across HITL resume.

    Simulates the telegram_529f7.log failure scenario where google_calendar was
    lost on resume. The fix ensures pinned domains survive the full pipeline.
    """

    def test_hitl_resume_preserves_google_calendar_tools(self):
        """Turn 1: routes google_calendar. Resume: router narrows to todoist.
        Result: google_calendar tools still available (merged from active_domains)."""
        snapshot = make_snapshot(active=("todoist", "google_calendar"))

        # --- Turn 1: initial request routes to google_calendar ---
        decision_gcal = RouterDecision(outcome="routed", domains=["google_calendar"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")
        router_client = _CannedRouterClient({"google calendar": decision_gcal})
        selector = RouterToolSelector(router_client=router_client, snapshot=snapshot)
        client = _RecordingClient()
        registry = _registry()
        node = create_agent_node(client, registry, max_agent_turns=20, tool_selector=selector)

        state_turn1 = _fresh_state(snapshot, user_prompt="add event to google calendar next friday")
        result_turn1 = asyncio.run(node(state_turn1))

        # Verify active_domains was set
        active_domains = result_turn1.get("active_domains", [])
        assert "google_calendar" in active_domains

        # --- Turn 2 (resume): reply is "find events from this week" ---
        # Router would classify this as todoist (no explicit "google calendar")
        # but domain merge should preserve google_calendar
        decision_todoist = RouterDecision(outcome="routed", domains=["todoist"], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")
        router_client_resume = _CannedRouterClient({
            "google calendar": decision_gcal,
            "find events": decision_todoist,
        })
        # The contextual query includes original prompt ("google calendar"), so it matches
        selector_resume = RouterToolSelector(
            router_client=router_client_resume, snapshot=snapshot
        )
        client_resume = _RecordingClient()
        node_resume = create_agent_node(
            client_resume, registry, max_agent_turns=20, tool_selector=selector_resume
        )

        state_resume = _resume_state(
            snapshot,
            user_prompt="add event to google calendar next friday",
            clarification_history=[
                {"question": "Which events?", "reply": "find events from this week"},
            ],
            active_domains=active_domains,
        )
        asyncio.run(node_resume(state_resume))

        # The tools offered to the LLM should include calendar tools
        tool_names_offered = {t["function"]["name"] for t in client_resume.seen_tools}
        assert "list_calendar_events" in tool_names_offered or "create_calendar_event" in tool_names_offered

    def test_fresh_todoist_request_no_calendar_leak(self):
        """A pure Todoist request must NOT expose google_calendar tools."""
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        router_client = _CannedRouterClient({})  # default → todoist
        selector = RouterToolSelector(router_client=router_client, snapshot=snapshot)
        client = _RecordingClient()
        node = create_agent_node(client, _registry(), max_agent_turns=20, tool_selector=selector)

        state = _fresh_state(snapshot, user_prompt="show me my tasks for today")
        result = asyncio.run(node(state))

        tool_names = {t["function"]["name"] for t in client.seen_tools}
        assert "list_calendar_events" not in tool_names
        assert "create_calendar_event" not in tool_names
        # active_domains should not include google_calendar
        assert "google_calendar" not in (result.get("active_domains") or [])

    def test_exit_clears_pinning_cleanly(self):
        """User says "exit" → domain merge is skipped, only ask_user remains."""
        snapshot = make_snapshot(active=("todoist", "google_calendar"))
        router_client = _CannedRouterClient({"exit": RouterDecision(outcome="conversation", domains=[], uncertain=False, candidate_domains=[], complexity="low", reasoning="test")})
        selector = RouterToolSelector(router_client=router_client, snapshot=snapshot)

        schemas = selector.select_schemas(
            "exit",
            _registry(),
            active_domains=["google_calendar", "todoist"],
        )

        tool_names = {s["function"]["name"] for s in schemas}
        assert tool_names == {"ask_user"}
