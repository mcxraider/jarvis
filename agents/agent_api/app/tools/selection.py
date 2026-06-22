"""Tool selection layer.

Sits between the :class:`ToolRegistry` (the *full* catalogue of every tool Jarvis
can run) and the agent node (which sends a tool list to the LLM on each turn).

Today this is a deliberate **pass-through**: the model sees every registered tool,
exactly as before this layer existed. The point of the abstraction is the seam,
not the behaviour — the agent node now depends only on the ``ToolSelector``
interface, so a smarter strategy can be dropped in without touching the graph.

Future direction (NOT implemented here):
    A retrieval-based selector will narrow the candidate set from the user's
    query using something cheap and local — regex keyword matching and/or BM25
    over each tool's name + description. As the Todoist surface expands (and new
    domains like Gmail/Calendar come online), sending the whole catalogue every
    turn wastes tokens and confuses the model; scoring tools against the query
    and returning only the top-k keeps the prompt tight and the choice sharp.

    Two rules a future selector MUST honour:
      1. Always retain "always-on" control tools (e.g. ``ask_user``) regardless of
         the query — the graph relies on them being callable on every turn.
      2. Only narrow what the model *sees*. Execution still runs against the full
         registry (see ToolDispatcher), so narrowing is a prompting optimisation,
         never a correctness gate.

    Each :class:`ToolSpec` in ``registry.specs`` carries both ``name`` and the
    full ``openai_schema`` (which includes the description), so a ranker has
    everything it needs without reaching into any domain package.
"""

from typing import Any, Dict, List, Protocol, runtime_checkable

from agents.agent_api.app.tools.base import ToolRegistry


@runtime_checkable
class ToolSelector(Protocol):
    """Chooses which tool schemas the LLM sees for a given user query.

    Implementations receive the user's query and the full tool registry and
    return the OpenAI/DeepSeek function schemas to expose for this turn. The
    returned order is the order the model sees the tools in.
    """

    def select_schemas(self, query: str, registry: ToolRegistry) -> List[Dict[str, Any]]:
        ...


class StaticToolSelector:
    """Default pass-through selector: expose every registered tool.

    Preserves the pre-selection-layer behaviour (the model sees the full
    catalogue each turn) and is the baseline a future retrieval-based selector
    will replace.
    """

    def select_schemas(self, query: str, registry: ToolRegistry) -> List[Dict[str, Any]]:
        # ``query`` is intentionally unused for now. A future regex/BM25 selector
        # will rank ``registry.specs`` against it (using each spec's name +
        # description) and return only the relevant subset — while always keeping
        # control tools such as ``ask_user``. Keeping the parameter here means
        # callers and the agent node are already wired for that change.
        del query
        return registry.openai_schemas()


# The selector used when callers don't inject their own. Swap this assignment (or
# pass a different selector into ``run_jarvis``) to change the strategy globally.
DEFAULT_TOOL_SELECTOR: ToolSelector = StaticToolSelector()


__all__ = ["ToolSelector", "StaticToolSelector", "DEFAULT_TOOL_SELECTOR"]
