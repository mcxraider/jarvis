"""Process-wide admission control for Jarvis graph executions.

Sync and async callers intentionally share one ``threading.BoundedSemaphore``
so the configured limit applies to the process as a whole. Health endpoints do
not acquire a slot and therefore remain observable while run capacity is full.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from fastapi import HTTPException

from agents.agent_api.app.config import settings

RETRY_AFTER_SECONDS = 5


class RunSlot:
    """An admission permit that can safely be released more than once."""

    __slots__ = ("_release_fn", "_released", "_lock")

    def __init__(self, release_fn: Callable[[], None]) -> None:
        self._release_fn = release_fn
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        """Return this permit exactly once, even across ownership hand-offs."""

        with self._lock:
            if self._released:
                return
            self._released = True
        self._release_fn()


class RunAdmission:
    """Bound concurrent runs admitted by synchronous and asynchronous routes."""

    def __init__(self, max_runs: int) -> None:
        self.max_runs = max(1, int(max_runs))
        self._semaphore = threading.BoundedSemaphore(self.max_runs)

    def try_acquire(self) -> Optional[RunSlot]:
        """Acquire a permit without queueing the request."""

        if not self._semaphore.acquire(blocking=False):
            return None
        return RunSlot(self._semaphore.release)

    async def try_acquire_async(self) -> Optional[RunSlot]:
        """Acquire from the same pool used by sync callers without blocking."""

        return self.try_acquire()


_admission = RunAdmission(settings.max_concurrent_runs)


def try_acquire_run_slot() -> Optional[RunSlot]:
    """Try to acquire one process-wide run slot."""

    return _admission.try_acquire()


async def try_acquire_run_slot_async() -> Optional[RunSlot]:
    """Async-compatible acquisition from the process-wide run pool."""

    return await _admission.try_acquire_async()


def capacity_exceeded() -> HTTPException:
    """Build the retryable response used when all run slots are occupied."""

    return HTTPException(
        status_code=429,
        detail="Jarvis is at capacity. Please retry in a few seconds.",
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


__all__ = [
    "RETRY_AFTER_SECONDS",
    "RunAdmission",
    "RunSlot",
    "capacity_exceeded",
    "try_acquire_run_slot",
    "try_acquire_run_slot_async",
]
