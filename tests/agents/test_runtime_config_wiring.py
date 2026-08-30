"""Stage 3 wiring: RunDeps forced model/reasoning pins applied in the agent node.

Drives create_agent_node with a RunDeps carried through the LangGraph config, so
it verifies the production seam (deps.forced_* overriding the router/client) without
a real DeepSeek or LangGraph run. The resolver precedence itself is covered by
tests/agents/test_llm_preferences.py.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict
from unittest.mock import patch

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

with patch("langsmith.wrappers.wrap_openai", side_effect=lambda c, **_: c):
    from agents.agent_api.app.graph.nodes.orchestrator import create_agent_node

from agents.agent_api.app.graph.prompts.context import build_initial_messages
from agents.agent_api.app.graph.run_deps import CONFIGURABLE_DEPS_KEY, RunDeps
from agents.agent_api.app.router.model_router import create_default_model_router
from agents.agent_api.app.router.prompt import RouterDecision
from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.selectors.static import StaticToolSelector
from tests.agents.runtime_helpers import make_snapshot


def _registry() -> ToolRegistry:
    specs = [
        ToolSpec(name=name, openai_schema={"type": "function", "function": {"name": name}})
        for name in ("ask_user", "add_todoist_task", "get_tasks")
    ]
    return ToolRegistry().register(specs)


class RecordingClient:
    """Captures create_message kwargs; ends the turn with no tool calls."""

    def __init__(self) -> None:
        self.seen_kwargs: Dict[str, Any] = {}

    def create_message(self, messages, tools, **kwargs):
        self.seen_kwargs = dict(kwargs)
        return {"role": "assistant", "content": "done"}


class FakeDecisionSelector:
    """Selector exposing a router decision and passing through all tools."""

    def __init__(self, decision) -> None:
        self._decision = decision

    @property
    def decision(self):
        return self._decision

    def select_schemas(self, query, registry, **kwargs):
        return registry.openai_schemas()


def _decision(complexity="low"):
    return RouterDecision(
        outcome="routed",
        domains=["todoist"],
        uncertain=False,
        candidate_domains=[],
        complexity=complexity,
    )


def _state(snapshot):
    return {
        "messages": build_initial_messages("add buy milk", runtime_context=snapshot),
        "user_prompt": "add buy milk",
        "turn_count": 0,
        "runtime_context": snapshot.model_dump(mode="json"),
    }


def _run(*, selector, model_router=None, forced_model=None, forced_reasoning_effort=None):
    client = RecordingClient()
    node = create_agent_node(
        client,
        _registry(),
        max_agent_turns=20,
        tool_selector=selector,
        model_router=model_router,
    )
    config = {
        "configurable": {
            CONFIGURABLE_DEPS_KEY: RunDeps(
                forced_model=forced_model,
                forced_reasoning_effort=forced_reasoning_effort,
            )
        }
    }
    snapshot = make_snapshot(active=("todoist",))
    asyncio.run(node(_state(snapshot), config))
    return client


class TestForcedPinOverRouter:
    def test_forced_model_overrides_router_selection(self):
        router = create_default_model_router(
            default_model="flash",
            default_reasoning="high",
            complex_model="pro",
            complex_reasoning="max",
        )
        client = _run(
            selector=FakeDecisionSelector(_decision("high")),  # router would pick pro/max
            model_router=router,
            forced_model="user-pinned-model",
        )
        assert client.seen_kwargs["model"] == "user-pinned-model"
        # Reasoning not pinned -> router's choice stands.
        assert client.seen_kwargs["reasoning_effort"] == "max"

    def test_forced_reasoning_overrides_router_selection(self):
        router = create_default_model_router(
            default_model="flash",
            default_reasoning="high",
            complex_model="pro",
            complex_reasoning="max",
        )
        client = _run(
            selector=FakeDecisionSelector(_decision("high")),
            model_router=router,
            forced_reasoning_effort="low",
        )
        assert client.seen_kwargs["reasoning_effort"] == "low"
        assert client.seen_kwargs["model"] == "pro"  # model not pinned


class TestForcedPinNoRouterDecision:
    def test_pin_applies_when_no_router_ran(self):
        # StaticToolSelector exposes no `decision`, so the router block is skipped;
        # overrides start None and the pin must still take effect.
        client = _run(
            selector=StaticToolSelector(),
            model_router=None,
            forced_model="pinned",
            forced_reasoning_effort="max",
        )
        assert client.seen_kwargs["model"] == "pinned"
        assert client.seen_kwargs["reasoning_effort"] == "max"


class TestNoPin:
    def test_absent_pins_leave_client_defaults(self):
        # No router, no pin -> both overrides stay None (client default downstream).
        client = _run(selector=StaticToolSelector())
        assert client.seen_kwargs["model"] is None
        assert client.seen_kwargs["reasoning_effort"] is None


class TestConcurrentUserConfigIsolation:
    def test_two_users_forced_pins_do_not_leak_across_shared_node(self):
        # One compiled node, two concurrent runs with different per-user RunDeps.
        # Each run must see only its own forced_model — no cross-contamination.
        node = create_agent_node(
            None,  # agent_client comes from each run's RunDeps
            _registry(),
            max_agent_turns=20,
            tool_selector=StaticToolSelector(),
        )
        snapshot = make_snapshot(active=("todoist",))
        users = {
            "alpha": ("pro", "max"),
            "beta": ("flash", "low"),
        }
        clients = {name: RecordingClient() for name in users}

        def invoke(name):
            model, effort = users[name]
            config = {
                "configurable": {
                    CONFIGURABLE_DEPS_KEY: RunDeps(
                        agent_client=clients[name],
                        forced_model=model,
                        forced_reasoning_effort=effort,
                    )
                }
            }
            asyncio.run(node(_state(snapshot), config))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(invoke, name) for name in users]
            for future in futures:
                future.result(timeout=5)

        assert clients["alpha"].seen_kwargs["model"] == "pro"
        assert clients["alpha"].seen_kwargs["reasoning_effort"] == "max"
        assert clients["beta"].seen_kwargs["model"] == "flash"
        assert clients["beta"].seen_kwargs["reasoning_effort"] == "low"
