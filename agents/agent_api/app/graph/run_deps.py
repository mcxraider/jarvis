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
    forced_model: Optional[str] = None
    forced_reasoning_effort: Optional[str] = None
    run_control: Any = None
    images: tuple[dict[str, str], ...] = field(
        default_factory=tuple, repr=False, compare=False
    )
    prior_image_batches: tuple[tuple[dict[str, str], ...], ...] | None = field(
        default=None, repr=False, compare=False
    )


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
