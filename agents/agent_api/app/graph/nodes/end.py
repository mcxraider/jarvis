"""Terminal marker node.

LangGraph's ``END`` is the sentinel string ``"__end__"``, not a node, so a graph
that routes straight to it produces no terminal span — traces just stop after the
last real node. This node is a real callable registered as ``graph.end``, so every
exit path closes with a visible span carrying the run's terminal summary.

It writes no state: the graph's result is whatever the previous node left behind.
"""

from typing import Optional

from langchain_core.runnables import RunnableConfig

from agents.agent_api.app.graph.run_deps import RunDeps, deps_from_config
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


def create_end_node(tracer: Optional[TracePrinter] = None):
    """Create the terminal node that gives every graph exit a ``graph.end`` span."""

    _captured = RunDeps(tracer=tracer or NULL_TRACE)

    async def end_node(
        state: JarvisState,
        config: RunnableConfig | None = None,
    ) -> JarvisState:
        deps = deps_from_config(config)
        tracer = (
            deps.tracer
            if deps is not None and deps.tracer is not None
            else _captured.tracer
        )
        tracer.event(
            "graph.end",
            "Graph reached terminal state.",
            turns=state.get("turn_count"),
            tool_results=len(state.get("tool_results", [])),
            has_error=bool(state.get("error")),
            interrupted=bool(state.get("interrupted")),
        )
        return {}

    return end_node


__all__ = ["create_end_node"]
