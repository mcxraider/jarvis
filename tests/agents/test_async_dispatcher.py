"""Tests for native async dispatch without weakening tool safety policy."""

import asyncio
import threading

import pytest

from agents.agent_api.app.idempotency.store import MemoryIdempotencyStore
from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.dispatcher import (
    ToolDispatcher,
    async_execute_tool_calls,
    tool_idempotency_context,
)


def _call(name: str, call_id: str, arguments: dict | None = None) -> dict:
    return {
        "id": call_id,
        "function": {"name": name, "arguments": arguments or {}},
    }


def test_native_async_handler_is_used_without_sync_fallback() -> None:
    calls: list[dict] = []

    def sync_handler(_arguments: dict) -> None:
        raise AssertionError("sync handler should not run")

    async def async_handler(arguments: dict) -> dict:
        await asyncio.sleep(0)
        calls.append(arguments)
        return {"id": "task-1"}

    registry = ToolRegistry().register(
        [
            ToolSpec(
                name="get_task",
                openai_schema={"type": "function", "function": {"name": "get_task"}},
                handler=sync_handler,
                async_handler=async_handler,
            )
        ]
    )
    dispatcher = ToolDispatcher(registry)

    result = asyncio.run(
        dispatcher.async_execute_tool_call(
            _call("get_task", "call-1", {"id": "task-1"})
        )
    )

    assert result["success"] is True
    assert result["content"] == {"id": "task-1"}
    assert calls == [{"id": "task-1"}]


def test_async_batch_preserves_order_and_is_sequential_in_stage_five() -> None:
    active = 0
    max_active = 0

    async def handler(arguments: dict) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return arguments["value"]

    registry = ToolRegistry().register(
        [
            ToolSpec(
                name="read",
                openai_schema={"type": "function", "function": {"name": "read"}},
                handler=lambda arguments: arguments["value"],
                async_handler=handler,
            )
        ]
    )
    dispatcher = ToolDispatcher(registry)

    results = asyncio.run(
        async_execute_tool_calls(
            [_call("read", f"call-{index}", {"value": index}) for index in range(3)],
            dispatcher,
        )
    )

    assert [result["content"] for result in results] == [0, 1, 2]
    assert max_active == 1


def test_async_mutation_keeps_idempotency_deduplication() -> None:
    executions = 0

    async def mutate(_arguments: dict) -> dict:
        nonlocal executions
        executions += 1
        return {"id": "created"}

    registry = ToolRegistry().register(
        [
            ToolSpec(
                name="create",
                openai_schema={"type": "function", "function": {"name": "create"}},
                handler=lambda _arguments: {"id": "created"},
                async_handler=mutate,
                mutating=True,
            )
        ]
    )
    dispatcher = ToolDispatcher(
        registry,
        allow_mutations=True,
        idempotency_store=MemoryIdempotencyStore(),
    )

    async def execute_twice() -> tuple[dict, dict]:
        with tool_idempotency_context("thread-1", 2, {"call-a": 0, "call-b": 0}):
            first = await dispatcher.async_execute_tool_call(
                _call("create", "call-a", {"name": "same"})
            )
            second = await dispatcher.async_execute_tool_call(
                _call("create", "call-b", {"name": "same"})
            )
        return first, second

    first, second = asyncio.run(execute_twice())

    assert first["success"] is True
    assert second["success"] is True
    assert second["idempotency_deduplicated"] is True
    assert executions == 1


def test_cancelled_native_mutation_settles_and_caches_before_propagating() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        executions = 0

        async def mutate(_arguments: dict) -> dict:
            nonlocal executions
            executions += 1
            started.set()
            await release.wait()
            return {"id": "created"}

        registry = ToolRegistry().register(
            [
                ToolSpec(
                    name="create",
                    openai_schema={"type": "function", "function": {"name": "create"}},
                    handler=lambda _arguments: {"id": "sync"},
                    async_handler=mutate,
                    mutating=True,
                )
            ]
        )
        dispatcher = ToolDispatcher(
            registry,
            allow_mutations=True,
            idempotency_store=MemoryIdempotencyStore(),
        )
        with tool_idempotency_context("thread-cancel", 1, {"first": 0, "retry": 0}):
            task = asyncio.create_task(
                dispatcher.async_execute_tool_call(
                    _call("create", "first", {"name": "same"})
                )
            )
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancellation should propagate after settlement")
            retry = await dispatcher.async_execute_tool_call(
                _call("create", "retry", {"name": "same"})
            )

        assert retry["idempotency_deduplicated"] is True
        assert executions == 1

    asyncio.run(scenario())


def test_cancelled_sync_fallback_mutation_waits_for_worker_and_caches() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        executions = 0

        def mutate(_arguments: dict) -> dict:
            nonlocal executions
            executions += 1
            started.set()
            assert release.wait(timeout=2)
            return {"id": "created"}

        registry = ToolRegistry().register(
            [
                ToolSpec(
                    name="create",
                    openai_schema={"type": "function", "function": {"name": "create"}},
                    handler=mutate,
                    mutating=True,
                )
            ]
        )
        dispatcher = ToolDispatcher(
            registry,
            allow_mutations=True,
            idempotency_store=MemoryIdempotencyStore(),
        )
        with tool_idempotency_context("thread-sync-cancel", 1, {"first": 0, "retry": 0}):
            task = asyncio.create_task(
                dispatcher.async_execute_tool_call(
                    _call("create", "first", {"name": "same"})
                )
            )
            while not started.is_set():
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancellation should propagate after settlement")
            retry = await dispatcher.async_execute_tool_call(
                _call("create", "retry", {"name": "same"})
            )

        assert retry["idempotency_deduplicated"] is True
        assert executions == 1

    asyncio.run(scenario())


def test_cancellation_after_provider_success_waits_for_claim_completion() -> None:
    async def scenario() -> None:
        completion_started = threading.Event()
        release_completion = threading.Event()
        executions = 0

        async def mutate(_arguments: dict) -> dict:
            nonlocal executions
            executions += 1
            return {"id": "created"}

        registry = ToolRegistry().register(
            [
                ToolSpec(
                    name="create",
                    openai_schema={"type": "function", "function": {"name": "create"}},
                    handler=lambda _arguments: {"id": "sync"},
                    async_handler=mutate,
                    mutating=True,
                )
            ]
        )
        dispatcher = ToolDispatcher(
            registry,
            allow_mutations=True,
            idempotency_store=MemoryIdempotencyStore(),
        )
        original_complete = dispatcher._complete_operation

        def complete(*args, **kwargs):
            completion_started.set()
            assert release_completion.wait(timeout=2)
            return original_complete(*args, **kwargs)

        dispatcher._complete_operation = complete
        with tool_idempotency_context(
            "thread-finalize-cancel",
            1,
            {"first": 0, "retry": 0},
        ):
            task = asyncio.create_task(
                dispatcher.async_execute_tool_call(
                    _call("create", "first", {"name": "same"})
                )
            )
            while not completion_started.is_set():
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release_completion.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            retry = await dispatcher.async_execute_tool_call(
                _call("create", "retry", {"name": "same"})
            )

        assert retry["idempotency_deduplicated"] is True
        assert executions == 1

    asyncio.run(scenario())


def test_cancellation_during_claim_acquisition_abandons_acquired_claim() -> None:
    async def scenario() -> None:
        claim_acquired = threading.Event()
        release_claim = threading.Event()
        executions = 0

        async def mutate(_arguments: dict) -> dict:
            nonlocal executions
            executions += 1
            return {"id": "created"}

        registry = ToolRegistry().register(
            [
                ToolSpec(
                    name="create",
                    openai_schema={"type": "function", "function": {"name": "create"}},
                    handler=lambda _arguments: {"id": "sync"},
                    async_handler=mutate,
                    mutating=True,
                )
            ]
        )
        dispatcher = ToolDispatcher(
            registry,
            allow_mutations=True,
            idempotency_store=MemoryIdempotencyStore(),
        )
        original_claim = dispatcher._claim_operation

        def claim(*args, **kwargs):
            result = original_claim(*args, **kwargs)
            claim_acquired.set()
            assert release_claim.wait(timeout=2)
            return result

        dispatcher._claim_operation = claim
        with tool_idempotency_context(
            "thread-claim-cancel",
            1,
            {"first": 0, "retry": 0},
        ):
            task = asyncio.create_task(
                dispatcher.async_execute_tool_call(
                    _call("create", "first", {"name": "same"})
                )
            )
            while not claim_acquired.is_set():
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release_claim.set()
            with pytest.raises(asyncio.CancelledError):
                await task

            dispatcher._claim_operation = original_claim
            retry = await dispatcher.async_execute_tool_call(
                _call("create", "retry", {"name": "same"})
            )

        assert retry["success"] is True
        assert executions == 1

    asyncio.run(scenario())
