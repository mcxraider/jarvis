"""Deterministic risk classification for tool calls.

Classifies tool calls by risk level *before* execution so risky actions can be
routed to the confirm gate instead of executing immediately. Classification is
based purely on the call signature and cumulative mutation count — no model call.
"""

from typing import Any, Dict, List, Tuple

from agents.agent_api.app.constants import CONFIRM_BULK_THRESHOLD
from agents.agent_api.app.tools.base import tool_call_name
from agents.agent_api.app.tools.metadata import always_risky_tools
from agents.agent_api.app.tools.todoist.schemas import MUTATING_TOOL_NAMES

RISKY_TOOLS = always_risky_tools()

MUTATING_TOOLS = frozenset(MUTATING_TOOL_NAMES)

BULK_THRESHOLD = CONFIRM_BULK_THRESHOLD


def classify_risk(tool_call: Dict[str, Any], state: Dict[str, Any]) -> str:
    """Classify a single tool call as "risky", "low", or "read".

    - "risky": irreversible or bulk-threshold-exceeding mutation → confirm gate
    - "low": single reversible mutation → execute normally
    - "read": non-mutating → execute normally
    """
    name = tool_call_name(tool_call)

    if name in RISKY_TOOLS:
        return "risky"

    if name in MUTATING_TOOLS and _mutation_count_this_turn(state) >= BULK_THRESHOLD:
        return "risky"

    if name in MUTATING_TOOLS:
        return "low"

    return "read"


def partition_tool_calls(
    tool_calls: List[Dict[str, Any]],
    state: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split tool calls into (risky, safe) based on risk classification."""
    risky: List[Dict[str, Any]] = []
    safe: List[Dict[str, Any]] = []

    for tool_call in tool_calls:
        if classify_risk(tool_call, state) == "risky":
            risky.append(tool_call)
        else:
            safe.append(tool_call)

    return risky, safe


def _mutation_count_this_turn(state: Dict[str, Any]) -> int:
    """Count mutating tool results accumulated so far in this turn."""
    tool_results = state.get("tool_results") or []
    return sum(
        1
        for result in tool_results
        if result.get("tool_name") in MUTATING_TOOLS
    )


__all__ = ["BULK_THRESHOLD", "RISKY_TOOLS", "classify_risk", "partition_tool_calls"]
