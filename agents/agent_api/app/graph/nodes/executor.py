"""Executor node — runs the frozen held_call after user approval.

This node is deterministic: it never calls the LLM. It applies four independent
guards (global mutation gate, approval check, hash binding, single-use token)
and dispatches the exact frozen payload on success.
"""

import json
from typing import Optional

from agents.agent_api.app.graph.canonicalize import verify_hash
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.dispatcher import ToolDispatcher, tool_result_to_message
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


def _build_decline_state(state: JarvisState, held: dict) -> JarvisState:
    """Return state with a synthetic 'declined' tool message for the LLM."""
    decline_message = {
        "role": "tool",
        "tool_call_id": held["origin_tool_call_id"],
        "name": held["tool_name"],
        "content": json.dumps({
            "tool_call_id": held["origin_tool_call_id"],
            "tool_name": held["tool_name"],
            "success": False,
            "content": None,
            "error": "Action declined by user. Please suggest an alternative or acknowledge.",
            "user_declined": True,
        }, default=str),
    }
    return {
        "held_call": None,
        "confirm_decision": None,
        "messages": state.get("messages", []) + [decline_message],
        "next": "agent",
    }


def _build_abort_state(state: JarvisState, held: dict, reason: str) -> JarvisState:
    """Return state with a synthetic error tool message when a guard fails."""
    abort_message = {
        "role": "tool",
        "tool_call_id": held["origin_tool_call_id"],
        "name": held["tool_name"],
        "content": json.dumps({
            "tool_call_id": held["origin_tool_call_id"],
            "tool_name": held["tool_name"],
            "success": False,
            "content": None,
            "error": f"Execution aborted: {reason}",
            "guard_failure": True,
        }, default=str),
    }
    return {
        "held_call": None,
        "confirm_decision": None,
        "messages": state.get("messages", []) + [abort_message],
        "next": "agent",
    }


def create_executor_node(
    tool_dispatcher: ToolDispatcher,
    tracer: Optional[TracePrinter] = None,
):
    """Create the graph node that executes a confirmed held_call."""

    tracer = tracer or NULL_TRACE

    def executor_node(state: JarvisState) -> JarvisState:
        held = state.get("held_call")
        decision = state.get("confirm_decision")

        if not held:
            error = "Executor node reached without a held_call in state."
            tracer.event("graph.executor", "Missing held_call.", error=error)
            return {"error": error, "final_response": error, "next": "end"}

        tracer.event(
            "graph.executor",
            "Evaluating guards.",
            held_call_id=held["id"],
            decision=decision,
            tool_name=held["tool_name"],
        )

        # Guard 0: global mutation gate
        if not tool_dispatcher.allow_mutations:
            tracer.event("graph.executor", "Blocked by ALLOW_MUTATIONS.", held_call_id=held["id"])
            return _build_abort_state(state, held, "mutations globally disabled")

        # Guard 1: approval
        if decision != "approve":
            tracer.event("graph.executor", "Declined by user.", held_call_id=held["id"])
            return _build_decline_state(state, held)

        # Guard 2: hash binding
        if not verify_hash(held):
            tracer.event("graph.executor", "Hash mismatch.", held_call_id=held["id"])
            return _build_abort_state(state, held, "hash mismatch — payload was tampered")

        # Guard 3: single-use token
        consumed = state.get("consumed_call_ids") or []
        if held["id"] in consumed:
            tracer.event("graph.executor", "Already consumed.", held_call_id=held["id"])
            return _build_abort_state(state, held, "already executed (replay protection)")

        # All guards passed — execute the frozen call
        tracer.event(
            "graph.executor",
            "Executing confirmed action.",
            held_call_id=held["id"],
            tool_name=held["tool_name"],
        )

        result = tool_dispatcher.execute_tool(
            held["origin_tool_call_id"],
            held["tool_name"],
            held["args"],
        )

        tracer.event(
            "graph.executor",
            "Execution completed.",
            held_call_id=held["id"],
            success=result.get("success"),
        )

        return {
            "consumed_call_ids": [held["id"]],
            "held_call": None,
            "confirm_decision": None,
            "messages": state.get("messages", []) + [tool_result_to_message(result)],
            "tool_results": state.get("tool_results", []) + [result],
            "next": "agent",
        }

    return executor_node


__all__ = ["create_executor_node"]
