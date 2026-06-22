"""Prepare-confirm node — freezes the risky call and defers siblings.

This thin node runs between the agent router's "confirm" edge and the confirm
node. It partitions tool calls, builds the held_call artifact, sets
pending_interrupt, and creates synthetic deferred messages for sibling calls.
"""

from typing import Optional

from agents.agent_api.app.graph.canonicalize import build_held_call
from agents.agent_api.app.graph.nodes.hitl import deferred_tool_message
from agents.agent_api.app.graph.risk import partition_tool_calls
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


def create_prepare_confirm_node(tracer: Optional[TracePrinter] = None):
    """Create the node that freezes a risky action into state before confirmation."""

    tracer = tracer or NULL_TRACE

    def prepare_confirm_node(state: JarvisState) -> JarvisState:
        messages = list(state.get("messages", []))
        latest_message = messages[-1] if messages else {}
        tool_calls = latest_message.get("tool_calls") or []

        risky, safe = partition_tool_calls(tool_calls, state)

        if not risky:
            error = "prepare_confirm reached without any risky tool calls."
            tracer.event("graph.prepare_confirm", "No risky calls found.", error=error)
            return {"error": error, "final_response": error, "next": "end"}

        primary_risky = risky[0]
        held = build_held_call(
            primary_risky,
            state.get("thread_id", ""),
            state.get("turn_count", 0),
        )

        # v1 policy: hold entire batch if any call is risky.
        # Defer all sibling calls (remaining risky + all safe).
        deferred_calls = risky[1:] + safe
        deferred_messages = [
            deferred_tool_message(tc, "Deferred pending confirmation of risky action.")
            for tc in deferred_calls
        ]

        tracer.event(
            "graph.prepare_confirm",
            "Froze risky action into held_call.",
            held_call_id=held["id"],
            tool_name=held["tool_name"],
            deferred_count=len(deferred_calls),
        )

        return {
            "held_call": held,
            "pending_interrupt": "confirm",
            "messages": messages + deferred_messages,
        }

    return prepare_confirm_node


__all__ = ["create_prepare_confirm_node"]
