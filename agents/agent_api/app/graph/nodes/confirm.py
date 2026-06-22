"""Confirm node — interrupts the graph for human approval of a risky action.

This node does NOT execute anything. It shows the frozen held_call to the user,
records their approve/decline decision, and lets the downstream router dispatch
to the executor (on approve) or back to the agent (on decline).
"""

from typing import Optional

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
    """Produce a human-readable summary of the frozen action."""
    tool_name = held_call.get("tool_name", "unknown")
    args = held_call.get("args", {})

    if tool_name == "delete_todoist_task":
        task_id = args.get("task_id", "unknown")
        return f"Delete task (id={task_id}) — this is irreversible."

    arg_summary = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:4])
    return f"{tool_name}({arg_summary})"


def create_confirm_node(tracer: Optional[TracePrinter] = None):
    """Create the graph node that pauses for user approval of a risky action."""

    tracer = tracer or NULL_TRACE

    def confirm_node(state: JarvisState) -> JarvisState:
        held = state.get("held_call")
        if not held:
            error = "Confirm node reached without a held_call in state."
            tracer.event("graph.confirm", "Missing held_call.", error=error)
            return {
                "error": error,
                "final_response": error,
                "next": "end",
            }

        payload = {
            "type": "confirm",
            "held_call_id": held["id"],
            "summary": render_action_summary(held),
            "tool_name": held["tool_name"],
            "args": held["args"],
        }

        tracer.event(
            "graph.confirm",
            "Interrupting for user confirmation.",
            tool_name=held["tool_name"],
            held_call_id=held["id"],
            summary=payload["summary"],
        )

        human_reply = interrupt(payload)
        decision = parse_decision(human_reply)

        tracer.event(
            "graph.confirm",
            "Resumed from user confirmation.",
            decision=decision,
            held_call_id=held["id"],
        )

        return {
            "pending_interrupt": None,
            "confirm_decision": decision,
        }

    return confirm_node


__all__ = ["create_confirm_node", "parse_decision", "render_action_summary"]
