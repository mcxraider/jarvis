"""Composition root for the active tool set.

Two ways to build the registry:

- ``build_runtime_registry`` — the production path. Intersects the registered
  :mod:`domain_adapters` with the user's active connections from a resolved runtime
  context, building each domain's client from its Vault secret. This is where per
  user capability is decided; adding a domain is one adapter entry, never a change
  here.
- ``build_registry_from_clients`` — an explicit dependency-injection seam for
  offline runs and tests that supply their own (fake) clients. It performs no
  credential resolution and knows nothing about users or the database.
"""

from typing import Any, Dict, List, Optional, Tuple

from agents.agent_api.app.tools.base import ToolRegistry
from agents.agent_api.app.tools.google_calendar.tools import (
    build_calendar_langchain_tools,
    get_calendar_tool_specs,
)
from agents.agent_api.app.tools.control import get_control_tool_specs
from agents.agent_api.app.tools.domain_adapters import DOMAIN_ADAPTERS
from agents.agent_api.app.tools.todoist.tools import (
    build_todoist_langchain_tools,
    get_todoist_tool_specs,
)
from agents.agent_api.app.tracing import TracePrinter
from agents.agent_api.app.user_context.runtime import ResolvedRuntimeContext


def build_registry_from_clients(
    todoist_client: Optional[Any] = None,
    calendar_client: Optional[Any] = None,
) -> ToolRegistry:
    """Build a registry from already-constructed clients (DI seam, no credentials).

    Used by offline/CLI runs and tests that inject fake clients. Registers control
    pseudo-tools first (so ``ask_user`` leads the tool list), then the provided
    domains. Not used on the production request path — that goes through
    ``build_runtime_registry``.
    """

    registry = ToolRegistry()
    registry.register(get_control_tool_specs())
    if todoist_client is not None:
        registry.register(
            get_todoist_tool_specs(todoist_client),
            langchain_builder=build_todoist_langchain_tools,
        )
    if calendar_client is not None:
        registry.register(
            get_calendar_tool_specs(calendar_client),
            langchain_builder=build_calendar_langchain_tools,
        )
    return registry


def build_runtime_registry(
    runtime_context: ResolvedRuntimeContext,
    tracer: TracePrinter,
) -> Tuple[ToolRegistry, List[Any], Dict[str, List[str]]]:
    """Build tools by intersecting runtime adapters with active connections.

    Returns the registry, the constructed domain clients, and a
    ``{provider: [tool_name, ...]}`` map. The caller records the tool names on the
    snapshot (``registered_tools`` and per-domain ``tool_names``) — this function
    does not mutate the snapshot.
    """

    registry = ToolRegistry()
    registry.register(get_control_tool_specs())
    clients: List[Any] = []
    tool_names_by_provider: Dict[str, List[str]] = {}
    domain_by_provider = {
        domain.provider: domain for domain in runtime_context.snapshot.domains
    }

    for provider, adapter in DOMAIN_ADAPTERS.items():
        domain = domain_by_provider.get(provider)
        credential = runtime_context.credentials.get(provider)
        if domain is None or domain.status != "active" or credential is None:
            continue

        client = adapter.build_client(credential, tracer)
        specs = adapter.get_tool_specs(client)
        registry.register(specs, langchain_builder=adapter.langchain_builder)
        tool_names_by_provider[provider] = [spec.name for spec in specs]
        clients.append(client)

    return registry, clients, tool_names_by_provider


def apply_registered_tools(
    runtime_context: ResolvedRuntimeContext,
    registry: ToolRegistry,
    tool_names_by_provider: Dict[str, List[str]],
) -> None:
    """Record the built tool names on the snapshot before it is persisted."""

    runtime_context.snapshot.registered_tools = [spec.name for spec in registry.specs]
    for domain in runtime_context.snapshot.domains:
        domain.tool_names = tool_names_by_provider.get(domain.provider, [])


__all__ = [
    "apply_registered_tools",
    "build_registry_from_clients",
    "build_runtime_registry",
]
