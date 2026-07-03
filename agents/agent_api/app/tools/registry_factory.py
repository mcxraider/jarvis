"""Composition root for the active tool set.

This is the one place that declares which tool domains Jarvis exposes. Adding a
domain (Gmail, Calendar, Notion, ...) is a single ``registry.register(...)`` line
here — no graph node, dispatcher, or builder change required.
"""

from typing import Any, Optional

from agents.agent_api.app.tools.base import ToolRegistry
from agents.agent_api.app.tools.calendar.tools import (
    build_calendar_langchain_tools,
    get_calendar_tool_specs,
)
from agents.agent_api.app.tools.control import get_control_tool_specs
from agents.agent_api.app.tools.todoist.tools import (
    build_todoist_langchain_tools,
    get_todoist_tool_specs,
)


def build_default_registry(
    todoist_client: Any,
    calendar_client: Optional[Any] = None,
) -> ToolRegistry:
    """Build the registry of control pseudo-tools plus every active domain.

    ``calendar_client`` is registered only when supplied — the caller
    (``builder.run_jarvis``) passes one iff Google Calendar is configured for
    this run (``auth.is_calendar_configured()``). A single-user machine with no
    ``token.json`` therefore never sees calendar tools that would only fail with
    an auth error, and the model isn't tempted to call them.
    """

    registry = ToolRegistry()
    # Control pseudo-tools first so ``ask_user`` leads the LLM tool list.
    registry.register(get_control_tool_specs())
    registry.register(
        get_todoist_tool_specs(todoist_client),
        langchain_builder=build_todoist_langchain_tools,
    )
    if calendar_client is not None:
        registry.register(
            get_calendar_tool_specs(calendar_client),
            langchain_builder=build_calendar_langchain_tools,
        )
    # Future domains plug in here, e.g.:
    # registry.register(get_gmail_tool_specs(gmail_client), build_gmail_langchain_tools)
    return registry


__all__ = ["build_default_registry"]
