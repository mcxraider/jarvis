"""Thread-safe cancellation state shared by one API request and graph run."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RunPhase(str, Enum):
    CANCELLABLE = "cancellable"
    MUTATION_IN_FLIGHT = "mutation_in_flight"
    CANCELLED = "cancelled"
    FINISHED = "finished"


class CancelOutcome(str, Enum):
    CANCELLED = "cancelled"
    MUTATION_IN_FLIGHT = "mutation_in_flight"
    ALREADY_FINISHED = "already_finished"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class CancelDecision:
    outcome: CancelOutcome
    cancel_task: bool = False


class RunControl:
    """Linearizable phase transitions for cancellation versus mutation dispatch."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phase = RunPhase.CANCELLABLE
        self._cancel_reason: Optional[str] = None
        self._deferred_cancel = False

    @property
    def phase(self) -> RunPhase:
        with self._lock:
            return self._phase

    @property
    def cancel_reason(self) -> Optional[str]:
        with self._lock:
            return self._cancel_reason

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_reason is not None

    def request_cancel(self, reason: str = "cancelled") -> CancelDecision:
        """Request cancellation without crossing an in-flight mutation boundary."""

        with self._lock:
            if self._phase is RunPhase.FINISHED:
                return CancelDecision(CancelOutcome.ALREADY_FINISHED)
            if self._phase is RunPhase.MUTATION_IN_FLIGHT:
                self._cancel_reason = self._cancel_reason or reason
                self._deferred_cancel = True
                return CancelDecision(CancelOutcome.MUTATION_IN_FLIGHT)
            if self._phase is RunPhase.CANCELLED:
                return CancelDecision(CancelOutcome.CANCELLED)

            self._phase = RunPhase.CANCELLED
            self._cancel_reason = self._cancel_reason or reason
            return CancelDecision(CancelOutcome.CANCELLED, cancel_task=True)

    def begin_mutation(self) -> bool:
        """Atomically cross the last cancellable point before external dispatch."""

        with self._lock:
            if self._phase is not RunPhase.CANCELLABLE:
                return False
            self._phase = RunPhase.MUTATION_IN_FLIGHT
            return True

    def finish_mutation(self) -> bool:
        """Leave mutation mode and report whether cancellation was deferred."""

        with self._lock:
            if self._phase is not RunPhase.MUTATION_IN_FLIGHT:
                return self._phase is RunPhase.CANCELLED
            if self._deferred_cancel:
                self._phase = RunPhase.CANCELLED
                return True
            self._phase = RunPhase.CANCELLABLE
            return False

    def try_mark_finished(self) -> bool:
        """Atomically publish normal completion if cancellation has not won."""

        with self._lock:
            if self._phase is RunPhase.FINISHED:
                return True
            if self._phase is not RunPhase.CANCELLABLE:
                return False
            self._phase = RunPhase.FINISHED
            return True

    def mark_cancelled_finished(self) -> None:
        """Publish settlement after a cancellation outcome is already fixed."""

        with self._lock:
            if self._phase is RunPhase.CANCELLED:
                self._phase = RunPhase.FINISHED


__all__ = [
    "CancelDecision",
    "CancelOutcome",
    "RunControl",
    "RunPhase",
]
