"""Composition root for the active tool set.

This is the one place that declares which tool domains Jarvis exposes. Adding a
domain (Gmail, Calendar, Notion, ...) is a single ``registry.register(...)`` line
here — no graph node, dispatcher, or builder change required.
"""

from typing import Any

from agents.agent_api.app.tools.base import ToolRegistry
from agents.agent_api.app.tools.control import get_control_tool_specs
from agents.agent_api.app.tools.todoist.tools import (
    build_todoist_langchain_tools,
    get_todoist_tool_specs,
)


def build_default_registry(todoist_client: Any) -> ToolRegistry:
    """Build the registry of control pseudo-tools plus every active domain."""

    registry = ToolRegistry()
    # Control pseudo-tools first so ``ask_user`` leads the LLM tool list.
    registry.register(get_control_tool_specs())
    registry.register(
        get_todoist_tool_specs(todoist_client),
        langchain_builder=build_todoist_langchain_tools,
    )
    # Future domains plug in here, e.g.:
    # registry.register(get_gmail_tool_specs(gmail_client), build_gmail_langchain_tools)
    return registry


__all__ = ["build_default_registry"]
