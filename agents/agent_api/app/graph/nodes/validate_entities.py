"""Prior-read ID validation node.

Runs between the agent router and execution. It blocks mutations whose entity IDs
(``task_id`` today) were never surfaced by a prior read in the thread — the common
failure mode where the model hallucinates a confident-but-wrong ID.

Policy (see plan): if *any* call in the assistant turn references an unseen required
ID, the **whole batch** is deferred — one synthetic tool-result message per call — and
the graph loops back to ``agent``, which re-reads or asks the user. This keeps the
chat-message contract consistent (every tool_call gets exactly one tool result) and
never executes a hallucinated mutation. When all refs are verified, the node performs
the normal risk split and hands off to ``tools`` or ``prepare_confirm`` via
``state["next"]`` (read by ``route_by_next``).

Same-turn protection is structural: this node runs *before* the tools node, so a read
issued in the same assistant turn is not yet in ``state["tool_results"]`` and its IDs
are correctly treated as unseen.
"""

import json
from typing import Any, Dict, List, Optional

from agents.agent_api.app.graph.entity_index import SeenEntityIndex
from agents.agent_api.app.graph.nodes.hitl import deferred_tool_message
from agents.agent_api.app.graph.risk import partition_tool_calls
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.base import tool_call_name
from agents.agent_api.app.tools.dispatcher import build_tool_result, tool_result_to_message
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


def _unverified_message(
    tool_call: Dict[str, Any], call_violations: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Synthetic tool result telling the agent one or more entity IDs could not be verified."""
    name = tool_call_name(tool_call)
    call_id = tool_call.get("id", "missing_tool_call_id")
    if len(call_violations) == 1:
        v = call_violations[0]
        error_text = (
            f"{v['arg']} '{v['value']}' was not returned by any prior read in this "
            f"conversation. Fetch it first (e.g. get_tasks / get_tasks_by_filter / "
            f"get_todoist_task) or ask the user, then retry."
        )
    else:
        parts = [f"{v['arg']} '{v['value']}'" for v in call_violations]
        error_text = (
            f"{', '.join(parts)} were not returned by any prior read in this "
            f"conversation. Fetch them first or ask the user, then retry."
        )
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": json.dumps(
            {
                "tool_call_id": call_id,
                "tool_name": name,
                "success": False,
                "content": None,
                "error": error_text,
                "unverified_entity": True,
            },
            default=str,
        ),
    }


def _out_of_route_message(tool_call: Dict[str, Any], allowed: List[str]) -> Dict[str, Any]:
    """Synthetic tool result for a call outside the selected per-turn tool set."""

    name = tool_call_name(tool_call)
    call_id = tool_call.get("id", "missing_tool_call_id")
    result = build_tool_result(
        call_id,
        name,
        success=False,
        error=(
            f"Tool '{name}' was not selected for this turn. "
            f"Allowed tools: {', '.join(allowed) if allowed else 'none'}."
        ),
    )
    result["out_of_route_tool"] = True
    return tool_result_to_message(result)


def create_validate_entities_node(tracer: Optional[TracePrinter] = None):
    """Create the node that blocks mutations on unverified prior-read entity IDs."""

    tracer = tracer or NULL_TRACE

    def validate_entities_node(state: JarvisState) -> JarvisState:
        state_messages = state.get("messages", [])
        latest_message = state_messages[-1] if state_messages else {}
        tool_calls = latest_message.get("tool_calls") or []

        # Defensive: the agent router only sends non-empty tool-call turns here.
        if not tool_calls:
            return {"next": "agent"}

        selected_tool_names = state.get("selected_tool_names") or []
        if selected_tool_names:
            allowed = set(selected_tool_names)
            out_of_route = [
                call for call in tool_calls if tool_call_name(call) not in allowed
            ]
            if out_of_route:
                messages = list(state_messages)
                synthetic = []
                out_of_route_ids = {
                    call.get("id", "missing_tool_call_id") for call in out_of_route
                }
                for call in tool_calls:
                    call_id = call.get("id", "missing_tool_call_id")
                    if call_id in out_of_route_ids:
                        synthetic.append(_out_of_route_message(call, selected_tool_names))
                    else:
                        synthetic.append(
                            deferred_tool_message(
                                call,
                                "Deferred — a sibling call used a tool outside the selected route; retry with selected tools only.",
                            )
                        )
                tracer.event(
                    "graph.tools.rejected",
                    "Rejected tool calls outside the selected route.",
                    requested=sorted({tool_call_name(call) for call in tool_calls}),
                    allowed=selected_tool_names,
                    rejected=sorted({tool_call_name(call) for call in out_of_route}),
                    next="agent",
                )
                return {"messages": messages + synthetic, "next": "agent"}

        index = SeenEntityIndex(state.get("tool_results", []))
        violations = index.violations(tool_calls)

        if not violations:
            risky, _safe = partition_tool_calls(tool_calls, state)
            next_node = "confirm" if risky else "tools"
            tracer.event(
                "graph.validate",
                "All entity references verified.",
                tool_calls=len(tool_calls),
                next=next_node,
            )
            return {"next": next_node}

        # Block the whole batch: one synthetic result per call, then loop to agent.
        # Accumulate all violations per call — a call may have multiple entity refs.
        violating_by_id: Dict[str, List[Dict[str, Any]]] = {}
        for v in violations:
            violating_by_id.setdefault(v["tool_call_id"], []).append(v)

        messages = list(state_messages)
        synthetic = []
        for call in tool_calls:
            call_id = call.get("id", "missing_tool_call_id")
            if call_id in violating_by_id:
                synthetic.append(_unverified_message(call, violating_by_id[call_id]))
            else:
                synthetic.append(
                    deferred_tool_message(
                        call,
                        "Deferred — a sibling call referenced an unverified id; "
                        "retry after resolving.",
                    )
                )

        tracer.event(
            "graph.validate",
            "Blocked unverified entity references.",
            violations=len(violations),
            batch=len(tool_calls),
            tool_names=sorted({v["tool_name"] for v in violations}),
            next="agent",
        )
        return {"messages": messages + synthetic, "next": "agent"}

    return validate_entities_node


__all__ = ["create_validate_entities_node"]
