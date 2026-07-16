"""Stage 6 gate: _resolve_tool_selector chooses the router selector only when opted in.

Tests the pure gating logic run_jarvis uses to pick a selector, without driving a
full graph run. The router path is opt-in and never a hard-failure path, so the
branch table below is the contract.
"""

import os
from unittest.mock import patch

# Disable tracing before importing anything that touches LangSmith/LangChain.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from agents.agent_api.app.graph.builder import _resolve_tool_selector
from agents.agent_api.app.tools.selectors.keyword import KeywordToolSelector
from agents.agent_api.app.tools.selectors.router import RouterToolSelector
from agents.agent_api.app.tools.selectors.static import StaticToolSelector
from agents.agent_api.app.tracing import NULL_TRACE
from agents.agent_api.app.user_context.runtime import ResolvedRuntimeContext
from tests.agents.runtime_helpers import make_snapshot

_ROUTER_CLIENT_PATH = "agents.agent_api.app.router.client.get_shared_router_client"


def _context():
    return ResolvedRuntimeContext(snapshot=make_snapshot(), credentials={})


def _resolve(
    *,
    runtime_context=None,
    resuming=False,
    router_enabled=True,
    tool_selector_name="router",
):
    return _resolve_tool_selector(
        runtime_context,
        resuming,
        True,  # allow_mutations
        NULL_TRACE,
        router_enabled=router_enabled,
        tool_selector_name=tool_selector_name,
    )


class FakeRouterClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def with_tracer(self, tracer):
        self.tracer = tracer
        return self


class TestRouterGateMet:
    def test_all_conditions_met_builds_router_selector(self):
        context = _context()
        with patch(_ROUTER_CLIENT_PATH, FakeRouterClient):
            selector = _resolve(runtime_context=context)
        assert isinstance(selector, RouterToolSelector)

    def test_router_selector_gets_snapshot_and_static_fallback(self):
        context = _context()
        with patch(_ROUTER_CLIENT_PATH, FakeRouterClient):
            selector = _resolve(runtime_context=context)
        # Snapshot is threaded through; fallback is the static all-tools selector.
        assert selector._snapshot is context.snapshot
        assert isinstance(selector._fallback, StaticToolSelector)
        assert isinstance(selector._client, FakeRouterClient)


class TestRouterGateNotMet:
    def test_router_disabled_uses_static(self):
        selector = _resolve(runtime_context=_context(), router_enabled=False)
        assert isinstance(selector, StaticToolSelector)

    def test_selector_name_not_router_uses_static(self):
        selector = _resolve(runtime_context=_context(), tool_selector_name="static")
        assert isinstance(selector, StaticToolSelector)

    def test_keyword_name_honored(self):
        selector = _resolve(runtime_context=_context(), tool_selector_name="keyword")
        assert isinstance(selector, KeywordToolSelector)

    def test_no_runtime_context_uses_static(self):
        selector = _resolve(runtime_context=None)
        assert isinstance(selector, StaticToolSelector)

    def test_resuming_uses_router(self):
        """Clarification resumes keep the narrow router-selected tool surface."""
        context = _context()
        with patch(_ROUTER_CLIENT_PATH, FakeRouterClient):
            selector = _resolve(runtime_context=context, resuming=True)
        assert isinstance(selector, RouterToolSelector)

    def test_router_name_without_gate_degrades_to_static(self):
        """tool_selector='router' but router_enabled=False -> static, not an error."""
        selector = _resolve(
            runtime_context=_context(),
            router_enabled=False,
            tool_selector_name="router",
        )
        assert isinstance(selector, StaticToolSelector)


class TestRouterConstructionFailure:
    def test_client_construction_error_degrades_to_static(self):
        """A missing API key (or any construction error) must not fail the run."""

        def _boom():
            raise RuntimeError("ROUTER_API_KEY missing")

        with patch(_ROUTER_CLIENT_PATH, _boom):
            selector = _resolve(runtime_context=_context())
        assert isinstance(selector, StaticToolSelector)
