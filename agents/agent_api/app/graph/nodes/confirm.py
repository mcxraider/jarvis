"""Confirm node — interrupts the graph for human approval of risky actions.

This node does NOT execute anything. It shows the frozen held_calls to the user,
records their approve/decline decision, and lets the downstream router dispatch
to the executor (on approve) or END (on decline).
"""

from typing import List, Optional

from langgraph.types import interrupt

from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter

APPROVE_TOKENS = frozenset({"approve", "yes", "confirm", "ok", "y"})


def parse_decision(reply: str) -> str:
    """Normalize a human reply to 'approve' or 'decline'."""
    normalized = str(reply).strip().lower()
    if normalized in APPROVE_TOKENS:
        return "approve"
    return "decline"


def render_action_summary(held_call: dict) -> str:
    """Produce a human-readable summary of a single frozen action."""
    tool_name = held_call.get("tool_name", "unknown")
    args = held_call.get("args", {})

    if tool_name == "delete_todoist_task":
        task_id = args.get("task_id", "unknown")
        context = held_call.get("context", {})
        task_content = context.get("task_content")
        if task_content:
            return f"Delete task \"{task_content}\" — this is irreversible."
        return f"Delete task (id={task_id}) — this is irreversible."

    arg_summary = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:4])
    return f"{tool_name}({arg_summary})"


def render_batch_summary(held_calls: List[dict]) -> str:
    """Produce a human-readable summary for a batch of frozen actions."""
    if len(held_calls) == 1:
        return render_action_summary(held_calls[0])

    lines = [f"Batch of {len(held_calls)} actions:"]
    for i, held in enumerate(held_calls, 1):
        lines.append(f"  {i}. {render_action_summary(held)}")
    return "\n".join(lines)


def create_confirm_node(tracer: Optional[TracePrinter] = None):
    """Create the graph node that pauses for user approval of risky actions."""

    tracer = tracer or NULL_TRACE

    def confirm_node(state: JarvisState) -> JarvisState:
        held_calls = state.get("held_calls") or []
        # Migration shim: support old singular held_call field
        if not held_calls and state.get("held_call"):
            held_calls = [state["held_call"]]

        if not held_calls:
            error = "Confirm node reached without held_calls in state."
            tracer.event("graph.confirm", "Missing held_calls.", error=error)
            return {
                "error": error,
                "final_response": error,
                "next": "end",
            }

        summary = render_batch_summary(held_calls)
        payload = {
            "type": "confirm",
            "held_call_ids": [h["id"] for h in held_calls],
            "summary": summary,
            "count": len(held_calls),
            "tool_names": list({h["tool_name"] for h in held_calls}),
        }

        tracer.event(
            "graph.confirm",
            "Interrupting for user confirmation.",
            count=len(held_calls),
            tool_names=payload["tool_names"],
            summary=summary,
        )

        human_reply = interrupt(payload)
        decision = parse_decision(human_reply)

        tracer.event(
            "graph.confirm",
            "Resumed from user confirmation.",
            decision=decision,
            count=len(held_calls),
        )

        if decision == "decline":
            return {
                "pending_interrupt": None,
                "confirm_decision": decision,
                "final_response": "Action declined — no changes were made.",
            }

        return {
            "pending_interrupt": None,
            "confirm_decision": decision,
        }

    return confirm_node


__all__ = ["create_confirm_node", "parse_decision", "render_action_summary", "render_batch_summary"]
