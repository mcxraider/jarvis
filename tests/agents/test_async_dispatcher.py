"""Tests for native async dispatch without weakening tool safety policy."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.agent_api.app.idempotency.store import ClaimState, MemoryIdempotencyStore
from agents.agent_api.app.graph.run_control import CancelOutcome, RunControl, RunPhase
from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.access_policy import ResourceAccessPolicy
from agents.agent_api.app.tools.todoist.client import TodoistApiError
from agents.agent_api.app.tools.dispatcher import (
    ToolDispatcher,
    async_execute_tool_calls,
    tool_idempotency_context,
)
from agents.agent_api.app.user_context.preferences import AccessPreferences


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


def test_native_async_result_is_filtered_before_dispatch_result() -> None:
    async def async_handler(_arguments: dict) -> list[dict]:
        await asyncio.sleep(0)
        return [
            {"id": "visible", "project_id": "public", "content": "Visible"},
            {"id": "secret", "project_id": "private", "content": "Secret"},
        ]

    registry = ToolRegistry().register(
        [
            ToolSpec(
                name="get_tasks",
                openai_schema={"type": "function", "function": {"name": "get_tasks"}},
                handler=lambda _arguments: [],
                async_handler=async_handler,
            )
        ]
    )
    policy = ResourceAccessPolicy(
        AccessPreferences.model_validate(
            {
                "restricted_todoist_projects": [
                    {"id": "private", "label": "Private"}
                ]
            }
        )
    )
    dispatcher = ToolDispatcher(registry, access_policy=policy)

    result = asyncio.run(
        dispatcher.async_execute_tool_call(_call("get_tasks", "call-filter"))
    )

    assert result["success"] is True
    assert result["content"] == [
        {"id": "visible", "project_id": "public", "content": "Visible"}
    ]


def test_ambiguous_async_mutation_error_is_cached_without_reexecution() -> None:
    calls = 0

    async def mutate(_arguments: dict) -> dict:
        nonlocal calls
        calls += 1
        raise TodoistApiError(
            kind="transient",
            message="Check Todoist before trying again.",
            retryable=False,
            operation="todoist.request",
            method="POST",
            ambiguous_commit=True,
        )

    registry = ToolRegistry().register([
        ToolSpec(
            name="create_calendar_event",
            openai_schema={
                "type": "function",
                "function": {"name": "create_calendar_event"},
            },
            handler=lambda _arguments: None,
            async_handler=mutate,
            mutating=True,
        )
    ])
    dispatcher = ToolDispatcher(
        registry,
        allow_mutations=True,
        idempotency_store=MemoryIdempotencyStore(),
        idempotency_operation_ttl_seconds=60,
        idempotency_lease_seconds=5,
    )

    async def scenario() -> tuple[dict, dict]:
        first = await dispatcher.async_execute_tool(
            "call-1", "create_calendar_event", {}, "calendar-key",
        )
        second = await dispatcher.async_execute_tool(
            "call-2", "create_calendar_event", {}, "calendar-key",
        )
        return first, second

    first, second = asyncio.run(scenario())

    assert calls == 1
    assert first["success"] is second["success"] is False
    assert first["mutation_blocked"] is second["mutation_blocked"] is True
    assert first["classified_error"]["ambiguous_commit"] is True
    assert second["tool_call_id"] == "call-2"


def test_async_batch_runs_reads_concurrently_and_preserves_order() -> None:
    active = 0
    max_active = 0
    all_started: asyncio.Event
    release: asyncio.Event

    async def handler(arguments: dict) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 3:
            all_started.set()
        await release.wait()
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

    async def scenario() -> list[dict]:
        nonlocal all_started, release
        all_started = asyncio.Event()
        release = asyncio.Event()
        task = asyncio.create_task(
            async_execute_tool_calls(
                [_call("read", f"call-{index}", {"value": index}) for index in range(3)],
                dispatcher,
            )
        )
        await asyncio.wait_for(all_started.wait(), timeout=1)
        release.set()
        return await task

    results = asyncio.run(scenario())

    assert [result["content"] for result in results] == [0, 1, 2]
    assert max_active == 3


def test_async_batch_serializes_mutations_between_read_groups() -> None:
    order: list[str] = []
    active_mutations = 0
    max_active_mutations = 0

    async def read(arguments: dict) -> int:
        order.append(f"read-{arguments['value']}")
        await asyncio.sleep(0)
        return arguments["value"]

    async def mutate(arguments: dict) -> int:
        nonlocal active_mutations, max_active_mutations
        active_mutations += 1
        max_active_mutations = max(max_active_mutations, active_mutations)
        order.append(f"mutate-{arguments['value']}")
        await asyncio.sleep(0)
        active_mutations -= 1
        return arguments["value"]

    registry = ToolRegistry().register(
        [
            ToolSpec(
                name="read",
                openai_schema={"type": "function", "function": {"name": "read"}},
                handler=lambda arguments: arguments["value"],
                async_handler=read,
            ),
            ToolSpec(
                name="mutate",
                openai_schema={"type": "function", "function": {"name": "mutate"}},
                handler=lambda arguments: arguments["value"],
                async_handler=mutate,
                mutating=True,
            ),
        ]
    )
    dispatcher = ToolDispatcher(registry, allow_mutations=True)

    results = asyncio.run(
        async_execute_tool_calls(
            [
                _call("read", "call-0", {"value": 0}),
                _call("read", "call-1", {"value": 1}),
                _call("mutate", "call-2", {"value": 2}),
                _call("mutate", "call-3", {"value": 3}),
                _call("read", "call-4", {"value": 4}),
            ],
            dispatcher,
        )
    )

    assert [result["content"] for result in results] == [0, 1, 2, 3, 4]
    assert set(order[:2]) == {"read-0", "read-1"}
    assert order[2:] == ["mutate-2", "mutate-3", "read-4"]
    assert max_active_mutations == 1


def test_read_concurrency_is_bounded_by_executor_limit() -> None:
    async def scenario() -> None:
        active = 0
        peak = 0
        two_started = asyncio.Event()
        release = asyncio.Event()

        async def read(arguments: dict) -> int:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
            await release.wait()
            active -= 1
            return arguments["value"]

        registry = ToolRegistry().register(
            [
                ToolSpec(
                    name="read",
                    openai_schema={"type": "function", "function": {"name": "read"}},
                    handler=lambda arguments: arguments["value"],
                    async_handler=read,
                )
            ]
        )
        dispatcher = ToolDispatcher(registry)
        with patch(
            "agents.agent_api.app.tools.dispatcher.settings",
            SimpleNamespace(executor_max_workers=2),
        ):
            task = asyncio.create_task(
                async_execute_tool_calls(
                    [
                        _call("read", f"call-{index}", {"value": index})
                        for index in range(4)
                    ],
                    dispatcher,
                )
            )
            await asyncio.wait_for(two_started.wait(), timeout=1)
            await asyncio.sleep(0)
            assert peak == 2
            release.set()
            results = await task

        assert [result["content"] for result in results] == [0, 1, 2, 3]
        assert peak == 2

    asyncio.run(scenario())


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


def test_async_completion_failure_persists_ambiguous_terminal_result() -> None:
    async def scenario() -> None:
        executions = 0
        abandon_calls = 0
        complete_calls = 0

        async def mutate(_arguments: dict) -> dict:
            nonlocal executions
            executions += 1
            return {"id": "created"}

        registry = ToolRegistry().register(
            [
                ToolSpec(
                    name="create",
                    openai_schema={
                        "type": "function",
                        "function": {"name": "create"},
                    },
                    handler=lambda _arguments: {"id": "sync"},
                    async_handler=mutate,
                    mutating=True,
                )
            ]
        )
        store = MemoryIdempotencyStore()
        dispatcher = ToolDispatcher(
            registry,
            allow_mutations=True,
            idempotency_store=store,
        )
        original_complete = dispatcher._complete_operation

        def complete(*args, **kwargs) -> bool:
            nonlocal complete_calls
            complete_calls += 1
            if complete_calls == 1:
                return False
            return original_complete(*args, **kwargs)

        dispatcher._complete_operation = complete

        def record_abandon(*_args, **_kwargs) -> None:
            nonlocal abandon_calls
            abandon_calls += 1

        dispatcher._abandon_operation = record_abandon
        first = await dispatcher.async_execute_tool(
            "call-ambiguous-async",
            "create",
            {},
            idempotency_key="ambiguous-async",
        )
        second = await dispatcher.async_execute_tool(
            "call-ambiguous-async-retry",
            "create",
            {},
            idempotency_key="ambiguous-async",
        )

        assert first["success"] is False
        assert first["content"] is None
        assert first["mutation_blocked"] is True
        assert "may have completed" in first["error"]
        assert "Do not retry automatically" in first["error"]
        assert second["success"] is False
        assert second["mutation_blocked"] is True
        assert second["idempotency_deduplicated"] is True
        assert complete_calls == 2
        assert executions == 1
        assert abandon_calls == 0

    asyncio.run(scenario())


def test_async_double_completion_failure_extends_claim_without_abandon() -> None:
    async def scenario() -> None:
        now = [100.0]
        executions = 0
        abandon_calls = 0
        complete_calls = 0
        renew_calls = 0

        async def mutate(_arguments: dict) -> dict:
            nonlocal executions
            executions += 1
            return {"id": "created"}

        registry = ToolRegistry().register(
            [
                ToolSpec(
                    name="create",
                    openai_schema={
                        "type": "function",
                        "function": {"name": "create"},
                    },
                    handler=lambda _arguments: {"id": "sync"},
                    async_handler=mutate,
                    mutating=True,
                )
            ]
        )
        store = MemoryIdempotencyStore(clock=lambda: now[0])
        dispatcher = ToolDispatcher(
            registry,
            allow_mutations=True,
            idempotency_store=store,
            idempotency_operation_ttl_seconds=60,
            idempotency_lease_seconds=5,
        )
        original_renew = dispatcher._renew_operation

        def fail_complete(*_args, **_kwargs) -> bool:
            nonlocal complete_calls
            complete_calls += 1
            return False

        def renew(*args, **kwargs) -> bool:
            nonlocal renew_calls
            renew_calls += 1
            return original_renew(*args, **kwargs)

        def record_abandon(*_args, **_kwargs) -> None:
            nonlocal abandon_calls
            abandon_calls += 1

        dispatcher._complete_operation = fail_complete
        dispatcher._renew_operation = renew
        dispatcher._abandon_operation = record_abandon
        result = await dispatcher.async_execute_tool(
            "call-ambiguous-async-retained",
            "create",
            {},
            idempotency_key="ambiguous-async-retained",
        )

        assert result["success"] is False
        assert result["mutation_blocked"] is True
        assert complete_calls == 2
        assert renew_calls == 1
        assert executions == 1
        assert abandon_calls == 0

        now[0] += 6
        assert store.claim(
            "ambiguous-async-retained",
            "operation",
            ttl_seconds=60,
            lease_seconds=5,
            tool_name="create",
        ).state is ClaimState.IN_PROGRESS

    asyncio.run(scenario())


def test_sync_completion_failure_persists_ambiguous_terminal_result() -> None:
    executions = 0
    abandon_calls = 0
    complete_calls = 0

    def mutate(_arguments: dict) -> dict:
        nonlocal executions
        executions += 1
        return {"id": "created"}

    registry = ToolRegistry().register(
        [
            ToolSpec(
                name="create",
                openai_schema={
                    "type": "function",
                    "function": {"name": "create"},
                },
                handler=mutate,
                mutating=True,
            )
        ]
    )
    store = MemoryIdempotencyStore()
    dispatcher = ToolDispatcher(
        registry,
        allow_mutations=True,
        idempotency_store=store,
    )
    original_complete = dispatcher._complete_operation

    def complete(*args, **kwargs) -> bool:
        nonlocal complete_calls
        complete_calls += 1
        if complete_calls == 1:
            return False
        return original_complete(*args, **kwargs)

    dispatcher._complete_operation = complete

    def record_abandon(*_args, **_kwargs) -> None:
        nonlocal abandon_calls
        abandon_calls += 1

    dispatcher._abandon_operation = record_abandon
    first = dispatcher.execute_tool(
        "call-ambiguous-sync",
        "create",
        {},
        idempotency_key="ambiguous-sync",
    )
    second = dispatcher.execute_tool(
        "call-ambiguous-sync-retry",
        "create",
        {},
        idempotency_key="ambiguous-sync",
    )

    assert first["success"] is False
    assert first["content"] is None
    assert first["mutation_blocked"] is True
    assert "may have completed" in first["error"]
    assert "Do not retry automatically" in first["error"]
    assert second["success"] is False
    assert second["mutation_blocked"] is True
    assert second["idempotency_deduplicated"] is True
    assert complete_calls == 2
    assert executions == 1
    assert abandon_calls == 0


def test_sync_double_completion_failure_extends_claim_without_abandon() -> None:
    now = [100.0]
    executions = 0
    abandon_calls = 0
    complete_calls = 0
    renew_calls = 0

    def mutate(_arguments: dict) -> dict:
        nonlocal executions
        executions += 1
        return {"id": "created"}

    registry = ToolRegistry().register(
        [
            ToolSpec(
                name="create",
                openai_schema={
                    "type": "function",
                    "function": {"name": "create"},
                },
                handler=mutate,
                mutating=True,
            )
        ]
    )
    store = MemoryIdempotencyStore(clock=lambda: now[0])
    dispatcher = ToolDispatcher(
        registry,
        allow_mutations=True,
        idempotency_store=store,
        idempotency_operation_ttl_seconds=60,
        idempotency_lease_seconds=5,
    )
    original_renew = dispatcher._renew_operation

    def fail_complete(*_args, **_kwargs) -> bool:
        nonlocal complete_calls
        complete_calls += 1
        return False

    def renew(*args, **kwargs) -> bool:
        nonlocal renew_calls
        renew_calls += 1
        return original_renew(*args, **kwargs)

    def record_abandon(*_args, **_kwargs) -> None:
        nonlocal abandon_calls
        abandon_calls += 1

    dispatcher._complete_operation = fail_complete
    dispatcher._renew_operation = renew
    dispatcher._abandon_operation = record_abandon
    result = dispatcher.execute_tool(
        "call-ambiguous-sync-retained",
        "create",
        {},
        idempotency_key="ambiguous-sync-retained",
    )

    assert result["success"] is False
    assert result["mutation_blocked"] is True
    assert complete_calls == 2
    assert renew_calls == 1
    assert executions == 1
    assert abandon_calls == 0

    now[0] += 6
    assert store.claim(
        "ambiguous-sync-retained",
        "operation",
        ttl_seconds=60,
        lease_seconds=5,
        tool_name="create",
    ).state is ClaimState.IN_PROGRESS


def test_control_cancellation_before_dispatch_abandons_claim() -> None:
    async def scenario() -> None:
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
        store = MemoryIdempotencyStore()
        cancelled_control = RunControl()
        cancelled_control.request_cancel()
        cancelled_dispatcher = ToolDispatcher(
            registry,
            allow_mutations=True,
            run_control=cancelled_control,
            idempotency_store=store,
        )
        with tool_idempotency_context(
            "thread-before-dispatch",
            1,
            {"first": 0},
        ):
            with pytest.raises(asyncio.CancelledError):
                await cancelled_dispatcher.async_execute_tool_call(
                    _call("create", "first", {"name": "same"})
                )
        assert executions == 0

        retry_dispatcher = ToolDispatcher(
            registry,
            allow_mutations=True,
            run_control=RunControl(),
            idempotency_store=store,
        )
        with tool_idempotency_context(
            "thread-before-dispatch",
            1,
            {"retry": 0},
        ):
            retry = await retry_dispatcher.async_execute_tool_call(
                _call("create", "retry", {"name": "same"})
            )
        assert retry["success"] is True
        assert executions == 1

    asyncio.run(scenario())


def test_mutation_task_creation_failure_restores_cancellable_phase() -> None:
    async def scenario() -> None:
        control = RunControl()

        async def mutate(arguments: dict) -> dict:
            return arguments

        registry = ToolRegistry().register(
            [
                ToolSpec(
                    name="create",
                    openai_schema={
                        "type": "function",
                        "function": {"name": "create"},
                    },
                    handler=lambda arguments: arguments,
                    async_handler=mutate,
                    mutating=True,
                )
            ]
        )
        dispatcher = ToolDispatcher(
            registry,
            allow_mutations=True,
            run_control=control,
        )

        original_create_task = asyncio.create_task

        def fail_handler_task(coro, *args, **kwargs):
            if getattr(getattr(coro, "cr_code", None), "co_name", "") == (
                "execute_and_finalize"
            ):
                raise RuntimeError("task creation failed")
            return original_create_task(coro, *args, **kwargs)

        with patch(
            "agents.agent_api.app.tools.dispatcher.asyncio.create_task",
            side_effect=fail_handler_task,
        ):
            result = await dispatcher.async_execute_tool_call(
                _call("create", "call-create", {"name": "test"})
            )

        assert result["success"] is False
        assert "task creation failed" in result["error"]
        assert control.phase is RunPhase.CANCELLABLE

    asyncio.run(scenario())


def test_control_cancel_during_mutation_settles_then_skips_later_mutations() -> None:
    async def scenario() -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        executions: list[int] = []

        async def mutate(arguments: dict) -> dict:
            executions.append(arguments["value"])
            if arguments["value"] == 1:
                first_started.set()
                await release_first.wait()
            return {"value": arguments["value"]}

        registry = ToolRegistry().register(
            [
                ToolSpec(
                    name="create",
                    openai_schema={"type": "function", "function": {"name": "create"}},
                    handler=lambda arguments: arguments,
                    async_handler=mutate,
                    mutating=True,
                )
            ]
        )
        store = MemoryIdempotencyStore()
        control = RunControl()
        dispatcher = ToolDispatcher(
            registry,
            allow_mutations=True,
            run_control=control,
            idempotency_store=store,
        )
        with tool_idempotency_context(
            "thread-deferred-cancel",
            1,
            {"first": 0, "second": 1},
        ):
            task = asyncio.create_task(
                async_execute_tool_calls(
                    [
                        _call("create", "first", {"value": 1}),
                        _call("create", "second", {"value": 2}),
                    ],
                    dispatcher,
                )
            )
            await first_started.wait()
            decision = control.request_cancel()
            assert decision.outcome is CancelOutcome.MUTATION_IN_FLIGHT
            assert not task.done()
            release_first.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert executions == [1]

        retry_dispatcher = ToolDispatcher(
            registry,
            allow_mutations=True,
            run_control=RunControl(),
            idempotency_store=store,
        )
        with tool_idempotency_context(
            "thread-deferred-cancel",
            1,
            {"retry": 0},
        ):
            retry = await retry_dispatcher.async_execute_tool_call(
                _call("create", "retry", {"value": 1})
            )
        assert retry["idempotency_deduplicated"] is True
        assert executions == [1]

    asyncio.run(scenario())


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


def test_mutation_phase_covers_operation_claim_completion() -> None:
    async def scenario() -> None:
        completion_started = threading.Event()
        release_completion = threading.Event()

        async def mutate(_arguments: dict) -> dict:
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
        control = RunControl()
        dispatcher = ToolDispatcher(
            registry,
            allow_mutations=True,
            run_control=control,
            idempotency_store=MemoryIdempotencyStore(),
        )
        original_complete = dispatcher._complete_operation

        def complete(*args, **kwargs):
            completion_started.set()
            assert release_completion.wait(timeout=2)
            return original_complete(*args, **kwargs)

        dispatcher._complete_operation = complete
        with tool_idempotency_context(
            "thread-phase-finalize",
            1,
            {"first": 0},
        ):
            task = asyncio.create_task(
                dispatcher.async_execute_tool_call(
                    _call("create", "first", {"name": "same"})
                )
            )
            while not completion_started.is_set():
                await asyncio.sleep(0)

            assert control.phase is RunPhase.MUTATION_IN_FLIGHT
            decision = control.request_cancel()
            assert decision.outcome is CancelOutcome.MUTATION_IN_FLIGHT
            assert not task.done()

            release_completion.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert control.phase is RunPhase.CANCELLED

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
