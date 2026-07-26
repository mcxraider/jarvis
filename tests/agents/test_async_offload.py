"""Bounded off-loop compatibility wrapper tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.agent_api.app import async_offload
from agents.agent_api.app.user_context import resolver


def setup_function() -> None:
    async_offload.reset_offload_limiters()


def teardown_function() -> None:
    async_offload.reset_offload_limiters()


def test_bounded_to_thread_limits_concurrent_submissions() -> None:
    async def run() -> None:
        entered = 0
        peak = 0
        both_entered = asyncio.Event()
        release = asyncio.Event()

        async def fake_to_thread(function, *args, **kwargs):
            nonlocal entered, peak
            entered += 1
            peak = max(peak, entered)
            if entered == 2:
                both_entered.set()
            await release.wait()
            entered -= 1
            return function(*args, **kwargs)

        with patch.object(
            async_offload,
            "settings",
            SimpleNamespace(executor_max_workers=2),
        ), patch("asyncio.to_thread", side_effect=fake_to_thread):
            tasks = [
                asyncio.create_task(async_offload.bounded_to_thread(lambda value: value, i))
                for i in range(4)
            ]
            await asyncio.wait_for(both_entered.wait(), timeout=1)
            await asyncio.sleep(0)
            assert peak == 2
            release.set()
            assert await asyncio.gather(*tasks) == [0, 1, 2, 3]

    asyncio.run(run())


def test_cancelled_waiter_keeps_permit_until_worker_finishes() -> None:
    async def run() -> None:
        entered = 0
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def fake_to_thread(function, label):
            nonlocal entered
            entered += 1
            if label == "first":
                first_entered.set()
                await release_first.wait()
            else:
                second_entered.set()
            return function(label)

        with patch.object(
            async_offload,
            "settings",
            SimpleNamespace(executor_max_workers=1),
        ), patch("asyncio.to_thread", side_effect=fake_to_thread):
            first = asyncio.create_task(
                async_offload.bounded_to_thread(lambda value: value, "first")
            )
            await first_entered.wait()
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            second = asyncio.create_task(
                async_offload.bounded_to_thread(lambda value: value, "second")
            )
            await asyncio.sleep(0)
            assert not first.done()
            assert not second_entered.is_set()
            assert not await async_offload.drain_offloads(0)

            release_first.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert await second == "second"
            assert second_entered.is_set()
            assert entered == 2
            assert await async_offload.drain_offloads(0)

    asyncio.run(run())


def test_runtime_context_async_wrappers_use_bounded_offload() -> None:
    identity = object()
    snapshot = object()
    resolved = object()

    async def run() -> None:
        with patch.object(
            resolver,
            "bounded_to_thread",
            new_callable=AsyncMock,
            side_effect=[resolved, None, resolved],
        ) as offload:
            assert await resolver.resolve_runtime_context_async(identity) is resolved
            assert (
                await resolver.store_thread_context_async("thread", "prompt", snapshot)
                is None
            )
            assert (
                await resolver.load_thread_runtime_context_async("thread", identity)
                is resolved
            )

        assert offload.await_args_list[0].args == (
            resolver.resolve_runtime_context,
            identity,
        )
        assert offload.await_args_list[1].args == (
            resolver.store_thread_context,
            "thread",
            "prompt",
            snapshot,
        )
        assert offload.await_args_list[2].args == (
            resolver.load_thread_runtime_context,
            "thread",
            identity,
        )

    asyncio.run(run())
