"""Executor node — runs the frozen held_calls after user approval.

This node is deterministic: it never calls the LLM. It applies guards
(global mutation gate, approval check, hash binding, single-use token)
and dispatches the exact frozen payloads on success.

Approved mutations execute sequentially so confirmation, idempotency, and external
side effects retain a single deterministic order. Guard checks are also sequential
(they are instant set-membership tests).

Resilience: the batch is protected by a configurable timeout, a shared
rate-limit throttle, and a circuit breaker that short-circuits remaining
calls when the upstream service is consistently failing.
"""

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.runnables import RunnableConfig

from agents.agent_api.app.async_offload import bounded_to_thread
from agents.agent_api.app.constants import (
    EXECUTOR_BATCH_TIMEOUT_SECONDS,
    EXECUTOR_CIRCUIT_BREAKER_THRESHOLD,
    EXECUTOR_THROTTLE_ENABLED,
)
from agents.agent_api.app.graph.canonicalize import verify_hash
from agents.agent_api.app.graph.resilience import (
    BatchCircuitBreaker,
    BatchThrottle,
    circuit_breaker_error_envelope,
    timeout_error_envelope,
)
from agents.agent_api.app.graph.run_deps import RunDeps, deps_from_config
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.dispatcher import ToolDispatcher, tool_result_to_message
from agents.agent_api.app.tools.metadata import get_service
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
    tool_dispatcher: Optional[ToolDispatcher] = None,
    tracer: Optional[TracePrinter] = None,
):
    """Create the graph node that executes confirmed held_calls."""

    _captured = RunDeps(dispatcher=tool_dispatcher, tracer=tracer or NULL_TRACE)

    async def _execute_one(
        held: dict,
        tool_dispatcher: ToolDispatcher,
        throttle: BatchThrottle,
        breaker: BatchCircuitBreaker,
        batch_deadline: float,
    ) -> Dict[str, Any]:
        """Execute a single held call with resilience checks."""
        if breaker.is_open():
            return circuit_breaker_error_envelope(held, breaker.open_reason() or "transient")

        remaining = max(0.0, batch_deadline - time.monotonic())
        if remaining <= 0:
            return timeout_error_envelope(held, EXECUTOR_BATCH_TIMEOUT_SECONDS)
        if EXECUTOR_THROTTLE_ENABLED:
            await bounded_to_thread(throttle.wait_if_paused, timeout=remaining)

        if breaker.is_open():
            return circuit_breaker_error_envelope(held, breaker.open_reason() or "transient")

        result = await tool_dispatcher.async_execute_tool(
            held["origin_tool_call_id"],
            held["tool_name"],
            held["args"],
            idempotency_key=held.get("idempotency_key"),
        )

        classified = result.get("classified_error")
        if classified:
            kind = classified.get("kind", "")
            if kind in ("transient", "rate-limit"):
                breaker.record_failure(kind)
            if kind == "rate-limit" and EXECUTOR_THROTTLE_ENABLED:
                retry_after = classified.get("retry_after_seconds")
                if retry_after and retry_after > 0:
                    throttle.signal_backoff(retry_after)
        elif result.get("success"):
            breaker.record_success()

        return result

    async def executor_node(
        state: JarvisState,
        config: RunnableConfig | None = None,
    ) -> JarvisState:
        deps = deps_from_config(config)
        tool_dispatcher = (
            deps.dispatcher
            if deps is not None and deps.dispatcher is not None
            else _captured.dispatcher
        )
        tracer = (
            deps.tracer
            if deps is not None and deps.tracer is not None
            else _captured.tracer
        )
        if tool_dispatcher is None:
            raise RuntimeError(
                "Executor node requires a dispatcher from RunDeps or captured fallbacks."
            )
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
        if decision == "approve":
            tracer.progress({"phase": "applying_change", "action": "started", "intent": "mutation"})

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

        # Phase 1: Sequential guard checks (instant — no I/O)
        # Partition held_calls into those that pass guards vs those that fail.
        all_consumed = set(state.get("consumed_call_ids") or [])
        ready_to_execute: List[Tuple[int, dict]] = []
        guard_failures: List[Tuple[int, dict]] = []

        for idx, held in enumerate(held_calls):
            # Guard 2: hash binding
            if not verify_hash(held):
                tracer.event("graph.executor", "Hash mismatch.", held_call_id=held["id"])
                guard_failures.append((idx, _abort_message(held, "hash mismatch — payload was tampered")))
                continue

            # Guard 3: single-use token
            if held["id"] in all_consumed:
                tracer.event("graph.executor", "Already consumed.", held_call_id=held["id"])
                guard_failures.append((idx, _abort_message(held, "already executed (replay protection)")))
                continue

            all_consumed.add(held["id"])
            ready_to_execute.append((idx, held))

        # Phase 2: Sequential mutation execution with batch-scoped resilience.
        # Once a confirmed mutation starts it is allowed to settle even if the
        # batch deadline elapses; cancelling it would make the external outcome
        # ambiguous. The elapsed deadline prevents later mutations from starting.
        execution_results: Dict[int, Dict[str, Any]] = {}

        if ready_to_execute:
            batch_deadline = time.monotonic() + EXECUTOR_BATCH_TIMEOUT_SECONDS
            throttle = BatchThrottle()
            breaker = BatchCircuitBreaker(threshold=EXECUTOR_CIRCUIT_BREAKER_THRESHOLD)

            tracer.event(
                "graph.executor",
                "Dispatching serialized confirmed mutations.",
                count=len(ready_to_execute),
                timeout=EXECUTOR_BATCH_TIMEOUT_SECONDS,
            )

            completed = 0
            for position, (idx, held) in enumerate(ready_to_execute):
                remaining_timeout = max(0.0, batch_deadline - time.monotonic())
                if remaining_timeout <= 0:
                    for timeout_idx, timeout_held in ready_to_execute[position:]:
                        execution_results[timeout_idx] = timeout_error_envelope(
                            timeout_held,
                            EXECUTOR_BATCH_TIMEOUT_SECONDS,
                        )
                    tracer.event(
                        "graph.executor",
                        "Batch timeout reached.",
                        timeout=EXECUTOR_BATCH_TIMEOUT_SECONDS,
                        completed=completed,
                        total=len(ready_to_execute),
                    )
                    break
                tracer.event(
                    "graph.executor",
                    "Executing confirmed action.",
                    held_call_id=held["id"],
                    tool_name=held["tool_name"],
                )
                mutation_task = asyncio.create_task(
                    _execute_one(
                        held,
                        tool_dispatcher,
                        throttle,
                        breaker,
                        batch_deadline,
                    )
                )
                try:
                    done, _pending = await asyncio.wait(
                        {mutation_task},
                        timeout=max(0.001, remaining_timeout),
                    )
                    deadline_elapsed = not done
                    # Provider-level request timeouts bound this wait. Shielding
                    # ensures the batch timeout never cancels an in-flight write.
                    result = await asyncio.shield(mutation_task)
                except asyncio.CancelledError:
                    # A request cancellation must likewise wait for a dispatched
                    # mutation to settle; otherwise its external outcome and
                    # idempotency claim would be left ambiguous.
                    while not mutation_task.done():
                        try:
                            await asyncio.shield(mutation_task)
                        except asyncio.CancelledError:
                            continue
                        except BaseException:
                            break
                    raise
                except Exception as exc:
                    result = {
                        "tool_call_id": held["origin_tool_call_id"],
                        "tool_name": held["tool_name"],
                        "success": False,
                        "content": None,
                        "error": f"Execution error: {exc}",
                    }
                completed += 1
                tracer.event(
                    "graph.executor",
                    "Execution completed.",
                    held_call_id=held["id"],
                    success=result.get("success"),
                )
                execution_results[idx] = result

                if deadline_elapsed:
                    for timeout_idx, timeout_held in ready_to_execute[position + 1 :]:
                        execution_results[timeout_idx] = timeout_error_envelope(
                            timeout_held,
                            EXECUTOR_BATCH_TIMEOUT_SECONDS,
                        )
                    tracer.event(
                        "graph.executor",
                        "Batch timeout reached after in-flight mutation settled.",
                        timeout=EXECUTOR_BATCH_TIMEOUT_SECONDS,
                        completed=completed,
                        total=len(ready_to_execute),
                    )
                    break

        # Phase 3: Reassemble results in original order
        result_messages: List[dict] = []
        tool_results: List[dict] = []
        consumed_ids: List[str] = []
        guard_failure_map = dict(guard_failures)

        for idx in range(len(held_calls)):
            if idx in execution_results:
                result = execution_results[idx]
                result_messages.append(tool_result_to_message(result))
                tool_results.append(result)
                consumed_ids.append(held_calls[idx]["id"])
            elif idx in guard_failure_map:
                result_messages.append(guard_failure_map[idx])
            else:
                held = held_calls[idx]
                tracer.event("graph.executor", "BUG: idx in neither partition.", idx=idx)
                result_messages.append(_abort_message(held, "internal error — no result produced"))

        existing_results = state.get("tool_results", [])
        existing_batches = {r.get("batch_index") for r in existing_results if r.get("batch_index") is not None}
        current_batch = max(existing_batches, default=-1) + 1
        for result in tool_results:
            result["batch_index"] = current_batch
            result["service"] = result.get("service") or get_service(result.get("tool_name", ""))

        return {
            "consumed_call_ids": consumed_ids,
            "held_calls": None,
            "confirm_decision": None,
            "messages": state.get("messages", []) + result_messages,
            "tool_results": existing_results + tool_results,
            "next": "agent",
        }

    return executor_node


__all__ = ["create_executor_node"]
