"""Bounded post-run persistence worker tests."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from unittest.mock import patch

from agents.agent_api.app import post_run
from agents.agent_api.app.graph import builder


def test_post_run_queue_is_fifo_bounded_and_non_blocking() -> None:
    async def run() -> None:
        entered = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def first() -> None:
            entered.set()
            release.wait()
            order.append("first")

        try:
            assert post_run.submit_post_run_job(first, queue_max=1)
            while not entered.is_set():
                await asyncio.sleep(0)
            assert post_run.submit_post_run_job(
                order.append,
                "second",
                queue_max=1,
            )
            assert not post_run.submit_post_run_job(
                order.append,
                "dropped",
                queue_max=1,
            )
            assert post_run.get_post_run_stats().accepted == 2
            assert post_run.get_post_run_stats().dropped == 1
        finally:
            release.set()

        assert await post_run.shutdown_post_run_jobs(1)
        assert order == ["first", "second"]

    asyncio.run(run())


def test_post_run_jobs_preserve_each_submission_context() -> None:
    async def run() -> None:
        request_id = contextvars.ContextVar("request_id", default="missing")
        observed: list[str] = []

        request_id.set("request-a")
        assert post_run.submit_post_run_job(
            lambda: observed.append(request_id.get())
        )
        request_id.set("request-b")
        assert post_run.submit_post_run_job(
            lambda: observed.append(request_id.get())
        )

        assert await post_run.shutdown_post_run_jobs(1)
        assert observed == ["request-a", "request-b"]

    asyncio.run(run())


def test_failed_job_does_not_stop_fifo_worker() -> None:
    async def run() -> None:
        completed = threading.Event()

        def fail() -> None:
            raise RuntimeError("non-critical write failed")

        assert post_run.submit_post_run_job(fail)
        assert post_run.submit_post_run_job(completed.set)
        while not completed.is_set():
            await asyncio.sleep(0)
        while post_run.get_post_run_stats().completed != 1:
            await asyncio.sleep(0)

        assert post_run.get_post_run_stats().failed == 1
        assert await post_run.shutdown_post_run_jobs(1)

    asyncio.run(run())


def test_zero_timeout_retains_active_worker_for_later_drain() -> None:
    async def run() -> None:
        assert post_run.submit_post_run_job(lambda: None)
        assert not await post_run.shutdown_post_run_jobs(0)
        assert await post_run.shutdown_post_run_jobs(1)

    asyncio.run(run())


def test_post_run_metadata_keeps_registration_before_usage() -> None:
    calls: list[str] = []
    with patch.object(
        builder,
        "_register_thread",
        side_effect=lambda *_args: calls.append("thread"),
    ), patch.object(
        builder,
        "_log_usage",
        side_effect=lambda *_args: calls.append("usage"),
    ):
        builder._persist_post_run_metadata(
            "thread-1",
            None,
            "hello",
            "completed",
            False,
            object(),
            10,
            "model",
        )

    assert calls == ["thread", "usage"]
