"""Batch-scoped resilience primitives for the executor node.

Both BatchThrottle and BatchCircuitBreaker are instantiated fresh per executor
batch — they carry no state between graph invocations.
"""

import json
import threading
import time
from typing import Any, Dict, Optional


class BatchThrottle:
    """Shared pause signal for rate-limit coordination across concurrent calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pause_until: float = 0.0

    def signal_backoff(self, retry_after_seconds: float) -> None:
        with self._lock:
            target = time.monotonic() + retry_after_seconds
            if target > self._pause_until:
                self._pause_until = target

    def wait_if_paused(self, timeout: float) -> None:
        with self._lock:
            pause_until = self._pause_until

        if pause_until <= time.monotonic():
            return

        sleep_duration = min(pause_until - time.monotonic(), timeout)
        if sleep_duration > 0:
            time.sleep(sleep_duration)


class BatchCircuitBreaker:
    """Consecutive-failure latch that opens when threshold is met.

    Once tripped, stays open for the batch lifetime (latch semantics).
    """

    def __init__(self, threshold: int = 2) -> None:
        self._lock = threading.Lock()
        self._threshold = threshold
        self._consecutive_count: int = 0
        self._last_kind: Optional[str] = None
        self._open = False
        self._open_reason: Optional[str] = None

    def is_open(self) -> bool:
        with self._lock:
            return self._open

    def open_reason(self) -> Optional[str]:
        with self._lock:
            return self._open_reason

    def record_failure(self, kind: str) -> None:
        with self._lock:
            if self._open:
                return
            self._consecutive_count += 1
            self._last_kind = kind
            if self._consecutive_count >= self._threshold:
                self._open = True
                self._open_reason = kind

    def record_success(self) -> None:
        with self._lock:
            if self._open:
                return
            self._consecutive_count = 0
            self._last_kind = None


def timeout_error_envelope(held: Dict[str, Any], timeout_seconds: float) -> Dict[str, Any]:
    return {
        "tool_call_id": held["origin_tool_call_id"],
        "tool_name": held["tool_name"],
        "success": False,
        "content": None,
        "error": (
            f"Execution timed out after {timeout_seconds:.0f}s. "
            "The upstream service may be unresponsive."
        ),
    }


def circuit_breaker_error_envelope(held: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "tool_call_id": held["origin_tool_call_id"],
        "tool_name": held["tool_name"],
        "success": False,
        "content": None,
        "error": (
            f"Execution skipped: upstream service unavailable "
            f"(circuit breaker tripped after repeated {reason} errors)."
        ),
    }


__all__ = [
    "BatchCircuitBreaker",
    "BatchThrottle",
    "circuit_breaker_error_envelope",
    "timeout_error_envelope",
]
