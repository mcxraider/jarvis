"""Bounded offloading for synchronous SDK and database compatibility paths."""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import Callable
from typing import Any, TypeVar

from agents.agent_api.app.config import settings

T = TypeVar("T")

_semaphores: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()
_active_tasks: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, set[asyncio.Task[Any]]
] = weakref.WeakKeyDictionary()
_semaphores_lock = threading.Lock()


def _semaphore_for_running_loop() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _semaphores_lock:
        semaphore = _semaphores.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(max(1, settings.executor_max_workers))
            _semaphores[loop] = semaphore
        return semaphore


async def bounded_to_thread(
    function: Callable[..., T],
    /,
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run blocking work off-loop under a per-event-loop concurrency bound.

    ``asyncio.to_thread`` preserves context variables, including request tracing.
    The loop-local semaphore avoids binding one asyncio primitive to multiple
    TestClient/event-loop lifetimes while bounding production work to the same
    configured ceiling used by tool execution.
    """

    loop = asyncio.get_running_loop()
    semaphore = _semaphore_for_running_loop()
    await semaphore.acquire()
    try:
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    except BaseException:
        semaphore.release()
        raise

    with _semaphores_lock:
        _active_tasks.setdefault(loop, set()).add(task)

    def release_permit(completed: asyncio.Task[Any]) -> None:
        # ``to_thread`` cannot stop an already-running worker. Keep the permit
        # until that worker actually settles, even if its awaiting request was
        # cancelled in the meantime.
        semaphore.release()
        with _semaphores_lock:
            tasks = _active_tasks.get(loop)
            if tasks is not None:
                tasks.discard(completed)
                if not tasks:
                    _active_tasks.pop(loop, None)
        if not completed.cancelled():
            # Retrieve background exceptions when the outer waiter was cancelled;
            # normal awaiters can still read the same result/exception.
            completed.exception()

    task.add_done_callback(release_permit)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Threads cannot be cancelled safely. Delay propagation until the accepted
        # operation settles so callers never report cancellation while a Calendar
        # mutation or durable context write is still changing external state.
        try:
            await asyncio.shield(task)
        except BaseException:
            pass
        raise


async def drain_offloads(timeout: float) -> bool:
    """Wait up to ``timeout`` seconds for this loop's accepted work to settle."""

    loop = asyncio.get_running_loop()
    with _semaphores_lock:
        tasks = set(_active_tasks.get(loop, ()))
    if not tasks:
        return True
    _done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout))
    return not pending


def reset_offload_limiters() -> None:
    """Drop loop-local limiters after lifespans/tests have drained their work."""

    with _semaphores_lock:
        _semaphores.clear()
        _active_tasks.clear()


__all__ = ["bounded_to_thread", "drain_offloads", "reset_offload_limiters"]
