"""Declarative graph assembly over LangGraph's ``StateGraph``.

Nodes and their outgoing edges are described as :class:`NodeSpec` entries and
wired by :func:`build_graph`. Adding a node (planner, confirmation, rewriter,
worker-dispatch, ...) or an intermediate stage becomes a localized change — write
the node factory, append a ``NodeSpec`` — rather than editing a monolithic builder.

Routing model:
- ``static_route``: an unconditional edge to one node, or ``"end"`` for graph exit.
- ``router`` + ``route_map``: a conditional edge. The router returns a key; the map
  translates that key to a target node (``"end"`` -> ``END``). New decision branches
  are a single map entry. Reusable routers live in ``graph/edges.py`` (e.g.
  ``route_by_next`` reads ``state["next"]``).
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from langgraph.graph import END, StateGraph

from agents.agent_api.app.checkpointing import DEFAULT_CHECKPOINTER

_USE_DEFAULT_CHECKPOINTER = object()


@dataclass
class NodeSpec:
    """One graph node plus how it routes onward."""

    name: str
    node: Callable[[Any], Any]
    static_route: Optional[str] = None
    router: Optional[Callable[[Any], str]] = None
    route_map: Optional[Dict[str, str]] = None


def _resolve(target: str) -> Any:
    """Translate the reserved ``"end"`` target to LangGraph's ``END`` sentinel."""

    return END if target == "end" else target


def build_graph(
    state_type: Any,
    node_specs: List[NodeSpec],
    entry: str,
    checkpointer: Any = _USE_DEFAULT_CHECKPOINTER,
) -> Any:
    """Compile a LangGraph app from declarative node specs."""

    if checkpointer is _USE_DEFAULT_CHECKPOINTER:
        checkpointer = DEFAULT_CHECKPOINTER
    workflow = StateGraph(state_type)
    for spec in node_specs:
        workflow.add_node(spec.name, spec.node)

    workflow.set_entry_point(entry)

    for spec in node_specs:
        if spec.router is not None:
            if not spec.route_map:
                raise ValueError(f"Node '{spec.name}' has a router but no route_map.")
            mapping = {key: _resolve(target) for key, target in spec.route_map.items()}
            workflow.add_conditional_edges(spec.name, spec.router, mapping)
        elif spec.static_route is not None:
            workflow.add_edge(spec.name, _resolve(spec.static_route))

    return workflow.compile(checkpointer=checkpointer)


__all__ = ["NodeSpec", "build_graph"]
