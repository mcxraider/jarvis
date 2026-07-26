"""LangGraph Studio entrypoint for the Jarvis graph.

This module intentionally builds a local-safe graph for Studio inspection:
no Telegram/Supabase identity resolution, and external tool domains only when
their direct local credentials are present. LangGraph dev supplies persistence.
"""

import os

from agents.agent_api.app.config import settings
from agents.agent_api.app.graph.builder import create_jarvis_graph
from agents.agent_api.app.graph.nodes.orchestrator import DeepSeekAgentClient
from agents.agent_api.app.tools.dispatcher import ToolDispatcher
from agents.agent_api.app.tools.registry_factory import build_registry_from_clients
from agents.agent_api.app.tools.selection import get_selector
from agents.agent_api.app.tools.todoist.client import TodoistApiClient
from agents.agent_api.app.tracing import TracePrinter


def _build_studio_graph():
    tracer = TracePrinter()
    todoist_api_key = os.getenv("TODOIST_API_KEY")
    todoist_client = (
        TodoistApiClient(api_key=todoist_api_key, tracer=tracer)
        if todoist_api_key
        else None
    )
    registry = build_registry_from_clients(todoist_client=todoist_client)
    dispatcher = ToolDispatcher(
        registry,
        allow_mutations=False,
        tracer=tracer,
        idempotency_store=None,
    )
    return create_jarvis_graph(
        DeepSeekAgentClient(tracer=tracer),
        dispatcher,
        max_agent_turns=settings.max_agent_turns,
        tracer=tracer,
        checkpointer=None,
        tool_selector=get_selector("static", allow_mutations=False),
    )


graph = _build_studio_graph()
