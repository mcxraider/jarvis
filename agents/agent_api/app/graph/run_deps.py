"""Request-scoped dependencies for a shared, compile-once Jarvis graph.

The graph topology is process-shared, while dispatchers, selectors, tracers,
usage accounting, and cancellation state belong to one invocation.  Callers
therefore inject a :class:`RunDeps` instance through LangGraph's
``config["configurable"]["deps"]`` mapping instead of closing over those
objects when the graph is compiled.

Node factories still capture optional fallbacks for direct unit tests and
Studio-style callers that invoke a node without a LangGraph config.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

CONFIGURABLE_DEPS_KEY = "deps"


@dataclass
class RunDeps:
    """Dependencies and mutable control state isolated to one graph run."""

    agent_client: Any = None
    registry: Any = None
    dispatcher: Any = None
    tracer: Any = None
    tool_selector: Any = None
    model_router: Any = None
    usage_accumulator: Any = None
    max_agent_turns: Optional[int] = None
    run_control: Any = None

    _tool_node: Any = field(default=None, init=False, repr=False, compare=False)

    def get_tool_node(self) -> Any:
        """Build and cache a ToolNode against this run's dispatcher only.

        ``RunDeps`` itself is per invocation, so the cache cannot share a tool
        registry or dispatcher across concurrent users.  Construction remains
        lazy because runs that never execute a tool do not need LangChain tool
        wrappers at all.
        """

        if self._tool_node is None:
            if self.dispatcher is None:
                raise RuntimeError(
                    "RunDeps.dispatcher is required to build the tool node."
                )
            from langgraph.prebuilt import ToolNode

            self._tool_node = ToolNode(
                self.dispatcher.build_langchain_tools(),
                handle_tool_errors=True,
            )
        return self._tool_node


def deps_from_config(config: Any) -> Optional[RunDeps]:
    """Return configured per-run dependencies, or ``None`` when absent.

    LangGraph supplies a mapping-compatible ``RunnableConfig``.  Treating an
    invalid value as absent keeps direct node calls compatible and prevents an
    arbitrary object from being mistaken for the dependency container.
    """

    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    deps = configurable.get(CONFIGURABLE_DEPS_KEY)
    return deps if isinstance(deps, RunDeps) else None


__all__ = ["CONFIGURABLE_DEPS_KEY", "RunDeps", "deps_from_config"]
