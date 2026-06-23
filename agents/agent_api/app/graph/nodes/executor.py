"""Executor node — runs the frozen held_calls after user approval.

This node is deterministic: it never calls the LLM. It applies guards
(global mutation gate, approval check, hash binding, single-use token)
and dispatches the exact frozen payloads on success.
"""

import json
from typing import Dict, List, Optional

from agents.agent_api.app.graph.canonicalize import verify_hash
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.dispatcher import ToolDispatcher, tool_result_to_message
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


def _decline_message(held: dict) -> dict:
    """Build a synthetic 'declined' tool message for one held call."""
    return {
        "role": "tool",
        "tool_call_id": held["origin_tool_call_id"],
        "name": held["tool_name"],
        "content": json.dumps({
            "tool_call_id": held["origin_tool_call_id"],
            "tool_name": held["tool_name"],
            "success": False,
            "content": None,
            "error": "Action declined by user.",
            "user_declined": True,
        }, default=str),
    }


def _abort_message(held: dict, reason: str) -> dict:
    """Build a synthetic error tool message when a guard fails."""
    return {
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


def create_executor_node(
    tool_dispatcher: ToolDispatcher,
    tracer: Optional[TracePrinter] = None,
):
    """Create the graph node that executes confirmed held_calls."""

    tracer = tracer or NULL_TRACE

    def executor_node(state: JarvisState) -> JarvisState:
        held_calls = state.get("held_calls") or []
        # Migration shim: support old singular held_call field
        if not held_calls and state.get("held_call"):
            held_calls = [state["held_call"]]

        decision = state.get("confirm_decision")

        if not held_calls:
            error = "Executor node reached without held_calls in state."
            tracer.event("graph.executor", "Missing held_calls.", error=error)
            return {"error": error, "final_response": error, "next": "end"}

        tracer.event(
            "graph.executor",
            "Evaluating guards.",
            count=len(held_calls),
            decision=decision,
        )

        # Guard 0: global mutation gate
        if not tool_dispatcher.allow_mutations:
            tracer.event("graph.executor", "Blocked by ALLOW_MUTATIONS.", count=len(held_calls))
            messages = [_abort_message(h, "mutations globally disabled") for h in held_calls]
            return {
                "held_calls": None,
                "confirm_decision": None,
                "messages": state.get("messages", []) + messages,
                "next": "agent",
            }

        # Guard 1: approval
        if decision != "approve":
            tracer.event("graph.executor", "Declined by user.", count=len(held_calls))
            messages = [_decline_message(h) for h in held_calls]
            return {
                "held_calls": None,
                "confirm_decision": None,
                "messages": state.get("messages", []) + messages,
                "next": "agent",
            }

        # Execute each held call with per-call guards
        all_consumed = set(state.get("consumed_call_ids") or [])
        result_messages: List[dict] = []
        tool_results: List[dict] = []
        consumed_ids: List[str] = []

        for held in held_calls:
            # Guard 2: hash binding
            if not verify_hash(held):
                tracer.event("graph.executor", "Hash mismatch.", held_call_id=held["id"])
                result_messages.append(_abort_message(held, "hash mismatch — payload was tampered"))
                continue

            # Guard 3: single-use token
            if held["id"] in all_consumed:
                tracer.event("graph.executor", "Already consumed.", held_call_id=held["id"])
                result_messages.append(_abort_message(held, "already executed (replay protection)"))
                continue

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

            result_messages.append(tool_result_to_message(result))
            tool_results.append(result)
            consumed_ids.append(held["id"])
            all_consumed.add(held["id"])

        return {
            "consumed_call_ids": consumed_ids,
            "held_calls": None,
            "confirm_decision": None,
            "messages": state.get("messages", []) + result_messages,
            "tool_results": state.get("tool_results", []) + tool_results,
            "next": "agent",
        }

    return executor_node


__all__ = ["create_executor_node"]
