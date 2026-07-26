"""Deterministic risk classification for tool calls.

Classifies tool calls by risk level *before* execution so risky actions can be
routed to the confirm gate instead of executing immediately. Classification is
based purely on the call signature and current batch size — no model call.
"""

from typing import Any, Dict, List, Tuple

from agents.agent_api.app.constants import CONFIRM_BULK_THRESHOLD
from agents.agent_api.app.tools.base import tool_call_name
from agents.agent_api.app.tools.google_calendar.schemas import MUTATING_CALENDAR_TOOLS
from agents.agent_api.app.tools.metadata import always_risky_tools
from agents.agent_api.app.tools.todoist.schemas import MUTATING_TOOL_NAMES

RISKY_TOOLS = always_risky_tools()

MUTATING_TOOLS = frozenset(MUTATING_TOOL_NAMES | MUTATING_CALENDAR_TOOLS)

BULK_THRESHOLD = CONFIRM_BULK_THRESHOLD


def classify_risk(tool_call: Dict[str, Any]) -> str:
    """Classify a single tool call as "risky", "low", or "read".

    - "risky": always-risky (e.g. delete) → confirm gate
    - "low": single reversible mutation → execute normally
    - "read": non-mutating → execute normally

    Note: batch-level bulk gating is handled by partition_tool_calls, not here.
    """
    name = tool_call_name(tool_call)

    if name in RISKY_TOOLS:
        return "risky"

    if name in MUTATING_TOOLS:
        return "low"

    return "read"


def partition_tool_calls(
    tool_calls: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split tool calls into (risky, safe) based on risk classification.

    Batch-aware: if the current batch contains >= BULK_THRESHOLD mutations,
    ALL mutations in the batch are classified as risky regardless of history.
    """
    risky: List[Dict[str, Any]] = []
    safe: List[Dict[str, Any]] = []

    current_mutation_count = sum(
        1 for call in tool_calls if tool_call_name(call) in MUTATING_TOOLS
    )
    batch_is_bulk = current_mutation_count >= BULK_THRESHOLD

    for tool_call in tool_calls:
        name = tool_call_name(tool_call)
        if name in RISKY_TOOLS:
            risky.append(tool_call)
        elif batch_is_bulk and name in MUTATING_TOOLS:
            risky.append(tool_call)
        else:
            safe.append(tool_call)

    return risky, safe


__all__ = ["BULK_THRESHOLD", "RISKY_TOOLS", "classify_risk", "partition_tool_calls"]
