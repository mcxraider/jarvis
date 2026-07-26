"""Tests for the async executor node — guards, safety, and resilience."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.agent_api.app.graph.canonicalize import build_held_call
from agents.agent_api.app.graph.nodes.executor import create_executor_node


def _make_held_call(task_id="42"):
    tc = {"id": "call_1", "function": {"name": "delete_todoist_task", "arguments": json.dumps({"task_id": task_id})}}
    return build_held_call(tc, "thread_1", 1)


def _make_dispatcher(allow=True, result=None):
    dispatcher = MagicMock()
    dispatcher.allow_mutations = allow
    dispatcher.async_execute_tool = AsyncMock(return_value=result or {
        "tool_call_id": "call_1",
        "tool_name": "delete_todoist_task",
        "success": True,
        "content": {"deleted": True},
        "error": None,
    })
    return dispatcher


def _make_state(held_call, decision="approve", consumed=None):
    return {
        "held_calls": [held_call],
        "confirm_decision": decision,
        "consumed_call_ids": consumed or [],
        "messages": [],
        "tool_results": [],
    }


def _run(node, state):
    return asyncio.run(node(state))


class TestGuard0MutationsDisabled:
    def test_blocks_when_mutations_disabled(self):
        held = _make_held_call()
        dispatcher = _make_dispatcher(allow=False)
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="approve")
        result = _run(node, state)
        assert result["held_calls"] is None
        assert "mutations globally disabled" in result["messages"][-1]["content"]
        dispatcher.async_execute_tool.assert_not_awaited()


class TestGuard1Approval:
    def test_decline_produces_decline_message(self):
        held = _make_held_call()
        dispatcher = _make_dispatcher()
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="decline")
        result = _run(node, state)
        assert result["held_calls"] is None
        msg_content = json.loads(result["messages"][-1]["content"])
        assert msg_content["user_declined"] is True
        assert msg_content["success"] is False
        dispatcher.async_execute_tool.assert_not_awaited()

    def test_none_decision_treated_as_decline(self):
        held = _make_held_call()
        dispatcher = _make_dispatcher()
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision=None)
        result = _run(node, state)
        msg_content = json.loads(result["messages"][-1]["content"])
        assert msg_content["user_declined"] is True
        dispatcher.async_execute_tool.assert_not_awaited()


class TestGuard2HashBinding:
    def test_tampered_args_blocked(self):
        held = _make_held_call()
        held["args"]["task_id"] = "TAMPERED"
        dispatcher = _make_dispatcher()
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="approve")
        result = _run(node, state)
        assert "hash mismatch" in result["messages"][-1]["content"]
        dispatcher.async_execute_tool.assert_not_awaited()


class TestGuard3SingleUse:
    def test_replay_blocked(self):
        held = _make_held_call()
        dispatcher = _make_dispatcher()
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="approve", consumed=[held["id"]])
        result = _run(node, state)
        assert "already executed" in result["messages"][-1]["content"]
        dispatcher.async_execute_tool.assert_not_awaited()


class TestSuccessfulExecution:
    def test_executes_frozen_call(self):
        held = _make_held_call()
        dispatcher = _make_dispatcher()
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="approve")
        result = _run(node, state)
        dispatcher.async_execute_tool.assert_awaited_once_with(
            held["origin_tool_call_id"],
            held["tool_name"],
            held["args"],
            idempotency_key=held["idempotency_key"],
        )
        assert result["held_calls"] is None
        assert result["confirm_decision"] is None
        assert result["consumed_call_ids"] == [held["id"]]
        assert result["next"] == "agent"
        assert len(result["messages"]) == 1
        assert len(result["tool_results"]) == 1

    def test_result_appended_to_tool_results(self):
        held = _make_held_call()
        expected_result = {
            "tool_call_id": "call_1",
            "tool_name": "delete_todoist_task",
            "success": True,
            "content": {"deleted": True},
            "error": None,
        }
        dispatcher = _make_dispatcher(result=expected_result)
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="approve")
        result = _run(node, state)
        assert result["tool_results"][-1] == expected_result


class TestSerializedExecution:
    def test_multiple_calls_execute_serially_in_original_order(self):
        """Confirmed mutations execute once each in their frozen order."""
        held_calls = []
        for i in range(5):
            tc = {"id": f"call_{i}", "function": {"name": "add_todoist_task", "arguments": json.dumps({"content": f"task {i}"})}}
            held_calls.append(build_held_call(tc, "thread_1", 1))

        dispatcher = MagicMock()
        dispatcher.allow_mutations = True
        dispatcher.async_execute_tool = AsyncMock(side_effect=lambda call_id, name, args, **_kwargs: {
            "tool_call_id": call_id,
            "tool_name": name,
            "success": True,
            "content": {"created": True, "content": args.get("content")},
            "error": None,
        })

        node = create_executor_node(dispatcher)
        state = {
            "held_calls": held_calls,
            "confirm_decision": "approve",
            "consumed_call_ids": [],
            "messages": [],
            "tool_results": [],
        }
        result = _run(node, state)

        assert dispatcher.async_execute_tool.await_count == 5
        assert [call.args[0] for call in dispatcher.async_execute_tool.await_args_list] == [
            f"call_{i}" for i in range(5)
        ]
        assert len(result["messages"]) == 5
        assert len(result["tool_results"]) == 5
        assert len(result["consumed_call_ids"]) == 5
        # Results are in original order
        for i, tool_result in enumerate(result["tool_results"]):
            assert tool_result["tool_call_id"] == f"call_{i}"
            assert tool_result["content"]["content"] == f"task {i}"

    def test_mixed_guard_failures_and_successes(self):
        """Guard failures and successes should both appear in order."""
        held_calls = []
        for i in range(3):
            tc = {"id": f"call_{i}", "function": {"name": "add_todoist_task", "arguments": json.dumps({"content": f"task {i}"})}}
            held_calls.append(build_held_call(tc, "thread_1", 1))

        # Tamper with the second call's args to trigger hash mismatch
        held_calls[1]["args"]["content"] = "TAMPERED"

        dispatcher = MagicMock()
        dispatcher.allow_mutations = True
        dispatcher.async_execute_tool = AsyncMock(return_value={
            "tool_call_id": "any",
            "tool_name": "add_todoist_task",
            "success": True,
            "content": {"created": True},
            "error": None,
        })

        node = create_executor_node(dispatcher)
        state = {
            "held_calls": held_calls,
            "confirm_decision": "approve",
            "consumed_call_ids": [],
            "messages": [],
            "tool_results": [],
        }
        result = _run(node, state)

        # 2 successful + 1 guard failure = 3 messages total
        assert len(result["messages"]) == 3
        assert dispatcher.async_execute_tool.await_count == 2
        assert len(result["consumed_call_ids"]) == 2
        # The middle message should be the guard failure
        middle_content = json.loads(result["messages"][1]["content"])
        assert middle_content["guard_failure"] is True
        assert "hash mismatch" in middle_content["error"]


class TestMissingHeldCall:
    def test_missing_held_call_errors(self):
        dispatcher = _make_dispatcher()
        node = create_executor_node(dispatcher)
        state = {"held_calls": None, "confirm_decision": "approve", "consumed_call_ids": [], "messages": [], "tool_results": []}
        result = _run(node, state)
        assert result.get("error")
        assert result["next"] == "end"


class TestBatchTimeout:
    @patch("agents.agent_api.app.graph.nodes.executor.EXECUTOR_BATCH_TIMEOUT_SECONDS", 0.3)
    def test_in_flight_mutation_settles_after_batch_deadline(self):
        """A mutation already sent is not cancelled when the batch deadline elapses."""
        held = _make_held_call()
        dispatcher = MagicMock()
        dispatcher.allow_mutations = True

        async def slow_success(call_id, name, *_args, **_kwargs):
            await asyncio.sleep(0.35)
            return {
                "tool_call_id": call_id,
                "tool_name": name,
                "success": True,
                "content": {"deleted": True},
                "error": None,
            }

        dispatcher.async_execute_tool = AsyncMock(side_effect=slow_success)

        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="approve")
        start = time.monotonic()
        result = _run(node, state)
        elapsed = time.monotonic() - start

        assert elapsed < 2.0
        assert elapsed >= 0.3
        msg_content = json.loads(result["messages"][-1]["content"])
        assert msg_content["success"] is True
        assert msg_content["content"] == {"deleted": True}

    @patch("agents.agent_api.app.graph.nodes.executor.EXECUTOR_BATCH_TIMEOUT_SECONDS", 0.5)
    def test_partial_timeout(self):
        """Fast calls succeed, slow calls get timeout envelopes."""
        held_calls = []
        for i in range(3):
            tc = {"id": f"call_{i}", "function": {"name": "add_todoist_task", "arguments": json.dumps({"content": f"task {i}"})}}
            held_calls.append(build_held_call(tc, "thread_1", 1))

        call_count = {"n": 0}

        async def execute_side_effect(call_id, name, args, **_kwargs):
            call_count["n"] += 1
            idx = int(call_id.split("_")[1])
            if idx == 1:
                await asyncio.sleep(0.55)
            return {
                "tool_call_id": call_id,
                "tool_name": name,
                "success": True,
                "content": {"created": True},
                "error": None,
            }

        dispatcher = MagicMock()
        dispatcher.allow_mutations = True
        dispatcher.async_execute_tool = AsyncMock(side_effect=execute_side_effect)

        node = create_executor_node(dispatcher)
        state = {
            "held_calls": held_calls,
            "confirm_decision": "approve",
            "consumed_call_ids": [],
            "messages": [],
            "tool_results": [],
        }
        result = _run(node, state)

        assert len(result["messages"]) == 3
        # The first succeeds; the already-started second settles successfully.
        for i in range(2):
            content = json.loads(result["messages"][i]["content"])
            assert content["success"] is True
        # The deadline prevents the third mutation from starting.
        content = json.loads(result["messages"][2]["content"])
        assert content["success"] is False
        assert "timed out" in content["error"]
        assert dispatcher.async_execute_tool.await_count == 2


class TestCircuitBreakerIntegration:
    @patch("agents.agent_api.app.graph.nodes.executor.EXECUTOR_BATCH_TIMEOUT_SECONDS", 5.0)
    @patch("agents.agent_api.app.graph.nodes.executor.EXECUTOR_CIRCUIT_BREAKER_THRESHOLD", 2)
    def test_breaker_opens_after_consecutive_transient_errors(self):
        """After threshold transient failures, remaining calls get circuit breaker error."""
        held_calls = []
        for i in range(4):
            tc = {"id": f"call_{i}", "function": {"name": "add_todoist_task", "arguments": json.dumps({"content": f"task {i}"})}}
            held_calls.append(build_held_call(tc, "thread_1", 1))

        def execute_side_effect(call_id, name, args, **_kwargs):
            return {
                "tool_call_id": call_id,
                "tool_name": name,
                "success": False,
                "content": None,
                "error": "Todoist is temporarily unavailable.",
                "classified_error": {
                    "kind": "transient",
                    "retryable": True,
                    "status_code": 503,
                    "attempts": 3,
                },
            }

        dispatcher = MagicMock()
        dispatcher.allow_mutations = True
        dispatcher.async_execute_tool = AsyncMock(side_effect=execute_side_effect)

        node = create_executor_node(dispatcher)
        state = {
            "held_calls": held_calls,
            "confirm_decision": "approve",
            "consumed_call_ids": [],
            "messages": [],
            "tool_results": [],
        }
        result = _run(node, state)

        assert len(result["messages"]) == 4
        # At least one should have "circuit breaker" error (calls 3+ after breaker trips)
        breaker_msgs = [
            m for m in result["messages"]
            if "circuit breaker" in json.loads(m["content"]).get("error", "")
        ]
        assert len(breaker_msgs) >= 1
        # Dispatcher should NOT have been called for circuit-broken calls
        assert dispatcher.async_execute_tool.await_count < 4


class TestThrottleIntegration:
    @patch("agents.agent_api.app.graph.nodes.executor.EXECUTOR_BATCH_TIMEOUT_SECONDS", 5.0)
    @patch("agents.agent_api.app.graph.nodes.executor.EXECUTOR_THROTTLE_ENABLED", True)
    def test_429_signals_backoff(self):
        """A 429 with retry_after should delay subsequent calls."""
        held_calls = []
        for i in range(2):
            tc = {"id": f"call_{i}", "function": {"name": "add_todoist_task", "arguments": json.dumps({"content": f"task {i}"})}}
            held_calls.append(build_held_call(tc, "thread_1", 1))

        call_times = []

        def execute_side_effect(call_id, name, args, **_kwargs):
            call_times.append(time.monotonic())
            idx = int(call_id.split("_")[1])
            if idx == 0:
                return {
                    "tool_call_id": call_id,
                    "tool_name": name,
                    "success": False,
                    "content": None,
                    "error": "Rate limited",
                    "classified_error": {
                        "kind": "rate-limit",
                        "retryable": True,
                        "status_code": 429,
                        "attempts": 1,
                        "retry_after_seconds": 0.2,
                    },
                }
            return {
                "tool_call_id": call_id,
                "tool_name": name,
                "success": True,
                "content": {"created": True},
                "error": None,
            }

        dispatcher = MagicMock()
        dispatcher.allow_mutations = True
        dispatcher.async_execute_tool = AsyncMock(side_effect=execute_side_effect)

        node = create_executor_node(dispatcher)
        state = {
            "held_calls": held_calls,
            "confirm_decision": "approve",
            "consumed_call_ids": [],
            "messages": [],
            "tool_results": [],
        }
        result = _run(node, state)

        assert dispatcher.async_execute_tool.await_count == 2
        # Second call should have been delayed by ~0.2s
        if len(call_times) == 2:
            delay = call_times[1] - call_times[0]
            assert delay >= 0.15


class TestExecutionErrorHandling:
    @patch("agents.agent_api.app.graph.nodes.executor.EXECUTOR_BATCH_TIMEOUT_SECONDS", 5.0)
    def test_exception_in_execute_does_not_crash_with_unbound_local(self):
        """If _execute_one raises, deadline_elapsed must not be unbound."""
        held = _make_held_call()
        dispatcher = MagicMock()
        dispatcher.allow_mutations = True
        dispatcher.async_execute_tool = AsyncMock(
            side_effect=RuntimeError("simulated provider failure")
        )

        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="approve")
        result = _run(node, state)

        assert len(result["messages"]) == 1
        content = json.loads(result["messages"][0]["content"])
        assert content["success"] is False
        assert "Execution error" in content["error"]


class TestBatchDeadlineSettlement:
    @patch("agents.agent_api.app.graph.nodes.executor.EXECUTOR_BATCH_TIMEOUT_SECONDS", 0.05)
    def test_timeout_waits_for_current_call_and_does_not_start_later_mutations(self):
        """The async timeout leaves no orphan and prevents later writes from starting."""
        held_calls = []
        for i in range(3):
            tc = {"id": f"call_{i}", "function": {"name": "add_todoist_task", "arguments": json.dumps({"content": f"task {i}"})}}
            held_calls.append(build_held_call(tc, "thread_1", 1))

        started = []
        async def execute_side_effect(call_id, name, *_args, **_kwargs):
            started.append(call_id)
            await asyncio.sleep(0.08)
            return {
                "tool_call_id": call_id,
                "tool_name": name,
                "success": True,
                "content": {"created": True},
                "error": None,
            }

        dispatcher = MagicMock()
        dispatcher.allow_mutations = True
        dispatcher.async_execute_tool = AsyncMock(side_effect=execute_side_effect)

        node = create_executor_node(dispatcher)
        state = {
            "held_calls": held_calls,
            "confirm_decision": "approve",
            "consumed_call_ids": [],
            "messages": [],
            "tool_results": [],
        }
        result = _run(node, state)

        assert started == ["call_0"]
        assert len(result["messages"]) == 3
        assert json.loads(result["messages"][0]["content"])["success"] is True
        assert all(
            "timed out" in json.loads(message["content"])["error"]
            for message in result["messages"][1:]
        )

    def test_request_cancellation_waits_for_dispatched_mutation_to_settle(self):
        """Cancelling the graph task cannot orphan an already-started write."""
        held = _make_held_call()

        async def scenario():
            started = asyncio.Event()
            finish = asyncio.Event()
            settled = asyncio.Event()

            async def execute(call_id, name, *_args, **_kwargs):
                started.set()
                await finish.wait()
                settled.set()
                return {
                    "tool_call_id": call_id,
                    "tool_name": name,
                    "success": True,
                    "content": {"deleted": True},
                    "error": None,
                }

            dispatcher = MagicMock()
            dispatcher.allow_mutations = True
            dispatcher.async_execute_tool = AsyncMock(side_effect=execute)
            node = create_executor_node(dispatcher)
            task = asyncio.create_task(node(_make_state(held, decision="approve")))

            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()

            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert settled.is_set()

        asyncio.run(scenario())
