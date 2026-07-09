"""Tool selection layer.

Sits between the :class:`ToolRegistry` (the *full* catalogue of every tool Jarvis
can run) and the agent node (which sends a tool list to the LLM on each turn).

The ``ToolSelector`` protocol defines the interface all selectors implement.
Concrete selectors live in ``selectors/`` — each file is one strategy.
Use ``get_selector(name)`` to instantiate by name (config-driven swap).
"""

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from agents.agent_api.app.tools.base import ToolRegistry


@runtime_checkable
class ToolSelector(Protocol):
    """Chooses which tool schemas the LLM sees for a given user query.

    Implementations receive the user's query and the full tool registry and
    return the OpenAI/DeepSeek function schemas to expose for this turn.
    """

    def select_schemas(
        self,
        query: str,
        registry: ToolRegistry,
        active_domains: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        ...


def get_selector(name: str = "keyword", **kwargs: Any) -> ToolSelector:
    """Instantiate a tool selector by name.

    Supported names:
      - "static"  — pass-through, exposes all tools (StaticToolSelector)
      - "keyword" — keyword matching with fallback (KeywordToolSelector)
      - "router"  — LLM domain classifier (RouterToolSelector); requires
        ``router_client`` and ``snapshot`` kwargs (see run_jarvis wiring).

    Additional kwargs are forwarded to the selector constructor.
    """
    from agents.agent_api.app.tools.selectors.keyword import KeywordToolSelector
    from agents.agent_api.app.tools.selectors.static import StaticToolSelector

    registry: Dict[str, type] = {
        "static": StaticToolSelector,
        "keyword": KeywordToolSelector,
    }
    # Imported lazily and only on demand: the router selector pulls in the router
    # client, which reuses the orchestrator's usage helpers — and the orchestrator
    # imports this module. Eager import here would create a load-order cycle
    # (DEFAULT_TOOL_SELECTOR below calls get_selector at module init). By the time
    # anything requests "router" at runtime, every module is fully loaded.
    if name == "router":
        from agents.agent_api.app.tools.selectors.router import RouterToolSelector

        registry["router"] = RouterToolSelector
    cls = registry.get(name)
    if cls is None:
        available = ", ".join(sorted(registry.keys()))
        raise ValueError(f"Unknown tool selector {name!r}. Available: {available}")
    return cls(**kwargs)


DEFAULT_TOOL_SELECTOR: ToolSelector = get_selector("static")

# Re-export concrete selectors for backward-compatible imports.
from agents.agent_api.app.tools.selectors.keyword import KeywordToolSelector  # noqa: E402
from agents.agent_api.app.tools.selectors.static import StaticToolSelector  # noqa: E402

__all__ = [
    "ToolSelector",
    "get_selector",
    "DEFAULT_TOOL_SELECTOR",
    "StaticToolSelector",
    "KeywordToolSelector",
]
