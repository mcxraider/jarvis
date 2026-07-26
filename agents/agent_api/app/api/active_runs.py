"""Identity-safe registry for accepted API producers and their deadlines."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from agents.agent_api.app.graph.run_control import (
    CancelOutcome,
    RunControl,
    RunPhase,
)

FINISHED_RUN_TTL_SECONDS = 300.0
FINISHED_RUN_MAX = 2048


@dataclass
class ActiveRun:
    user_id: str
    request_id: Optional[str]
    thread_id: Optional[str]
    deadline: float
    task: asyncio.Task
    control: RunControl
    internal_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    deadline_task: Optional[asyncio.Task[None]] = field(
        default=None,
        repr=False,
    )

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining <= 0.0


class ActiveRunRegistry:
    """Process-local registry with atomic cancellation and identity-aware cleanup."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active_by_id: dict[str, ActiveRun] = {}
        self._active_by_key: dict[tuple[str, str], ActiveRun] = {}
        self._finished: OrderedDict[
            tuple[str, str], tuple[float, CancelOutcome]
        ] = OrderedDict()
        # A cancel can reach this endpoint after Telegram has allocated a
        # request id but just before the invoke route registers its producer.
        # Retain that intent briefly so registration cannot race past it.
        self._pending_cancels: OrderedDict[tuple[str, str], float] = OrderedDict()

    def register(
        self,
        *,
        user_id: str,
        request_id: Optional[str],
        thread_id: Optional[str],
        deadline: float,
        task: asyncio.Task,
        control: RunControl,
    ) -> ActiveRun:
        run = ActiveRun(
            user_id=user_id,
            request_id=request_id,
            thread_id=thread_id,
            deadline=deadline,
            task=task,
            control=control,
        )
        key = self._public_key(user_id, request_id)
        cancel_on_register = False
        with self._lock:
            self._purge_finished_locked()
            if key is not None and key in self._active_by_key:
                raise RuntimeError("An active run already owns this request id.")
            self._active_by_id[run.internal_id] = run
            if key is not None:
                cancel_on_register = self._pending_cancels.pop(key, None) is not None
                self._finished.pop(key, None)
                self._active_by_key[key] = run

        try:
            deadline_task = task.get_loop().create_task(self._enforce_deadline(run))
        except BaseException:
            self.finish(run)
            raise
        run.deadline_task = deadline_task
        task.add_done_callback(lambda _completed: self.finish(run))
        if cancel_on_register:
            decision = control.request_cancel("cancelled")
            if decision.cancel_task:
                task.get_loop().call_soon(self._cancel_if_current, run)
        return run

    def get(self, user_id: str, request_id: str) -> Optional[ActiveRun]:
        with self._lock:
            return self._active_by_key.get((user_id, request_id))

    def cancel(
        self,
        user_id: str,
        request_id: str,
        *,
        reason: str = "cancelled",
    ) -> CancelOutcome:
        key = (user_id, request_id)
        with self._lock:
            self._purge_finished_locked()
            run = self._active_by_key.get(key)
            if run is None:
                finished = self._finished.get(key)
                if finished is not None:
                    return finished[1]
                self._pending_cancels[key] = time.monotonic()
                self._pending_cancels.move_to_end(key)
                self._purge_finished_locked()
                return CancelOutcome.NOT_FOUND
            if run.task.done():
                self._finish_locked(run)
                finished = self._finished.get(key)
                return (
                    finished[1]
                    if finished is not None
                    else CancelOutcome.ALREADY_FINISHED
                )
            decision = run.control.request_cancel(reason)

        if decision.cancel_task:
            run.task.get_loop().call_soon_threadsafe(
                self._cancel_if_current,
                run,
            )
        return decision.outcome

    def finish(self, run: ActiveRun) -> bool:
        """Remove exactly ``run``; stale callbacks cannot delete a replacement."""

        with self._lock:
            return self._finish_locked(run)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._active_by_id)

    def active_keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._active_by_key)

    def reset(self) -> None:
        """Clear terminal/test state after accepted producers have drained."""

        with self._lock:
            if self._active_by_id:
                raise RuntimeError("Cannot reset the active-run registry while runs exist.")
            self._active_by_key.clear()
            self._finished.clear()
            self._pending_cancels.clear()

    async def _enforce_deadline(self, run: ActiveRun) -> None:
        try:
            await asyncio.sleep(run.remaining)
            self._cancel_active(run, reason="deadline")
        except asyncio.CancelledError:
            raise

    def _cancel_active(self, run: ActiveRun, *, reason: str) -> CancelOutcome:
        with self._lock:
            if self._active_by_id.get(run.internal_id) is not run:
                return CancelOutcome.ALREADY_FINISHED
            if run.task.done():
                self._finish_locked(run)
                return CancelOutcome.ALREADY_FINISHED
            decision = run.control.request_cancel(reason)
        if decision.cancel_task:
            run.task.get_loop().call_soon_threadsafe(
                self._cancel_if_current,
                run,
            )
        return decision.outcome

    def _cancel_if_current(self, run: ActiveRun) -> None:
        with self._lock:
            if self._active_by_id.get(run.internal_id) is not run:
                return
            if run.control.phase is RunPhase.FINISHED:
                return
        if not run.task.done():
            run.task.cancel()

    def _finish_locked(self, run: ActiveRun) -> bool:
        if self._active_by_id.get(run.internal_id) is not run:
            return False
        self._active_by_id.pop(run.internal_id, None)
        key = self._public_key(run.user_id, run.request_id)
        if key is not None and self._active_by_key.get(key) is run:
            self._active_by_key.pop(key, None)
            outcome = (
                CancelOutcome.CANCELLED
                if run.control.cancel_reason == "cancelled"
                else CancelOutcome.ALREADY_FINISHED
            )
            self._finished[key] = (time.monotonic(), outcome)
            self._finished.move_to_end(key)
            self._purge_finished_locked()
        deadline_task = run.deadline_task
        if deadline_task is not None and not deadline_task.done():
            loop = deadline_task.get_loop()
            try:
                loop.call_soon_threadsafe(deadline_task.cancel)
            except RuntimeError:
                # A closed owner loop has already made the deadline task inert.
                pass
        return True

    def _purge_finished_locked(self) -> None:
        cutoff = time.monotonic() - FINISHED_RUN_TTL_SECONDS
        while self._finished:
            key, (finished_at, _outcome) = next(iter(self._finished.items()))
            if finished_at >= cutoff and len(self._finished) <= FINISHED_RUN_MAX:
                break
            self._finished.pop(key, None)
        while self._pending_cancels:
            key, created_at = next(iter(self._pending_cancels.items()))
            if created_at >= cutoff and len(self._pending_cancels) <= FINISHED_RUN_MAX:
                break
            self._pending_cancels.pop(key, None)

    @staticmethod
    def _public_key(
        user_id: str,
        request_id: Optional[str],
    ) -> Optional[tuple[str, str]]:
        return (user_id, request_id) if request_id else None


_registry = ActiveRunRegistry()


def get_active_run_registry() -> ActiveRunRegistry:
    return _registry


def reset_active_run_registry() -> None:
    _registry.reset()


__all__ = [
    "ActiveRun",
    "ActiveRunRegistry",
    "FINISHED_RUN_MAX",
    "FINISHED_RUN_TTL_SECONDS",
    "get_active_run_registry",
    "reset_active_run_registry",
]
