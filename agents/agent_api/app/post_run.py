"""Bounded FIFO execution for non-critical post-run persistence."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import weakref
from dataclasses import dataclass
from typing import Any, Callable

from agents.agent_api.app.async_offload import bounded_to_thread

POST_RUN_QUEUE_MAX = 256
_STOP = object()


@dataclass(frozen=True)
class PostRunStats:
    accepted: int = 0
    dropped: int = 0
    completed: int = 0
    failed: int = 0


@dataclass(frozen=True)
class _PostRunJob:
    callback: Callable[[], Any]
    context: contextvars.Context

    def run(self) -> None:
        self.context.run(self.callback)


class _PostRunWorker:
    def __init__(self, queue_max: int) -> None:
        if queue_max <= 0:
            raise ValueError("queue_max must be greater than zero")
        self.queue: asyncio.Queue[_PostRunJob | object] = asyncio.Queue(
            maxsize=queue_max
        )
        self.accepted = 0
        self.dropped = 0
        self.completed = 0
        self.failed = 0
        self.task = asyncio.create_task(self._run())

    def submit(self, job: _PostRunJob) -> bool:
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        self.accepted += 1
        return True

    async def _run(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                if job is _STOP:
                    return
                assert isinstance(job, _PostRunJob)
                try:
                    await bounded_to_thread(job.run)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.failed += 1
                else:
                    self.completed += 1
            finally:
                self.queue.task_done()

    @property
    def stats(self) -> PostRunStats:
        return PostRunStats(
            accepted=self.accepted,
            dropped=self.dropped,
            completed=self.completed,
            failed=self.failed,
        )


_workers: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    _PostRunWorker,
] = weakref.WeakKeyDictionary()


def _remove_finished_worker(
    loop: asyncio.AbstractEventLoop,
    worker: _PostRunWorker,
) -> None:
    if _workers.get(loop) is worker:
        _workers.pop(loop, None)
    if not worker.task.cancelled():
        worker.task.exception()


def _worker_for_loop(queue_max: int) -> _PostRunWorker:
    loop = asyncio.get_running_loop()
    worker = _workers.get(loop)
    if worker is not None:
        if worker.queue.maxsize != queue_max:
            raise ValueError("queue_max cannot change while a post-run worker is active")
        return worker
    worker = _PostRunWorker(queue_max)
    _workers[loop] = worker
    worker.task.add_done_callback(
        lambda _task: _remove_finished_worker(loop, worker)
    )
    return worker


def submit_post_run_job(
    function: Callable[..., Any],
    /,
    *args: Any,
    queue_max: int = POST_RUN_QUEUE_MAX,
    **kwargs: Any,
) -> bool:
    """Queue one context-isolated job without waiting or blocking on saturation."""

    callback = functools.partial(function, *args, **kwargs)
    job = _PostRunJob(callback=callback, context=contextvars.copy_context())
    return _worker_for_loop(queue_max).submit(job)


def get_post_run_stats() -> PostRunStats:
    worker = _workers.get(asyncio.get_running_loop())
    return worker.stats if worker is not None else PostRunStats()


async def shutdown_post_run_jobs(timeout: float) -> bool:
    """Drain and stop this loop's worker, retaining resources on timeout."""

    loop = asyncio.get_running_loop()
    worker = _workers.get(loop)
    if worker is None:
        return True
    if timeout <= 0:
        return False

    deadline = loop.time() + timeout
    join_task = asyncio.create_task(worker.queue.join())
    try:
        await asyncio.wait_for(join_task, timeout=timeout)
    except asyncio.TimeoutError:
        join_task.cancel()
        try:
            await join_task
        except asyncio.CancelledError:
            pass
        return False

    # queue.join() guarantees the queue is empty and the active job settled, so
    # the non-blocking sentinel cannot deadlock on a full queue.
    worker.queue.put_nowait(_STOP)
    remaining = max(0.0, deadline - loop.time())
    try:
        await asyncio.wait_for(asyncio.shield(worker.task), timeout=remaining)
    except asyncio.TimeoutError:
        return False
    _workers.pop(loop, None)
    return True


__all__ = [
    "POST_RUN_QUEUE_MAX",
    "PostRunStats",
    "get_post_run_stats",
    "shutdown_post_run_jobs",
    "submit_post_run_job",
]
