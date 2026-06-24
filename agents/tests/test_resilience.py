"""Unit tests for BatchThrottle and BatchCircuitBreaker primitives."""

import threading
import time

import pytest

from agents.agent_api.app.graph.resilience import (
    BatchCircuitBreaker,
    BatchThrottle,
    circuit_breaker_error_envelope,
    timeout_error_envelope,
)


class TestBatchThrottle:
    def test_no_signal_no_wait(self):
        throttle = BatchThrottle()
        start = time.monotonic()
        throttle.wait_if_paused(timeout=5.0)
        elapsed = time.monotonic() - start
        assert elapsed < 0.05

    def test_signal_and_wait(self):
        throttle = BatchThrottle()
        throttle.signal_backoff(0.15)
        start = time.monotonic()
        throttle.wait_if_paused(timeout=5.0)
        elapsed = time.monotonic() - start
        assert 0.10 < elapsed < 0.40

    def test_max_of_multiple_signals(self):
        throttle = BatchThrottle()
        throttle.signal_backoff(0.05)
        throttle.signal_backoff(0.20)
        start = time.monotonic()
        throttle.wait_if_paused(timeout=5.0)
        elapsed = time.monotonic() - start
        assert 0.15 < elapsed < 0.45

    def test_respects_timeout_ceiling(self):
        throttle = BatchThrottle()
        throttle.signal_backoff(10.0)
        start = time.monotonic()
        throttle.wait_if_paused(timeout=0.1)
        elapsed = time.monotonic() - start
        assert elapsed < 0.25

    def test_thread_safety(self):
        throttle = BatchThrottle()
        errors = []

        def signal():
            try:
                for _ in range(100):
                    throttle.signal_backoff(0.001)
            except Exception as e:
                errors.append(e)

        def wait():
            try:
                for _ in range(100):
                    throttle.wait_if_paused(timeout=0.001)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=signal) for _ in range(3)]
        threads += [threading.Thread(target=wait) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


class TestBatchCircuitBreaker:
    def test_starts_closed(self):
        breaker = BatchCircuitBreaker(threshold=2)
        assert not breaker.is_open()
        assert breaker.open_reason() is None

    def test_threshold_trips(self):
        breaker = BatchCircuitBreaker(threshold=2)
        breaker.record_failure("transient")
        assert not breaker.is_open()
        breaker.record_failure("transient")
        assert breaker.is_open()
        assert breaker.open_reason() == "transient"

    def test_different_kinds_still_accumulate(self):
        breaker = BatchCircuitBreaker(threshold=2)
        breaker.record_failure("transient")
        breaker.record_failure("rate-limit")
        assert breaker.is_open()
        assert breaker.open_reason() == "rate-limit"

    def test_success_resets_counter(self):
        breaker = BatchCircuitBreaker(threshold=2)
        breaker.record_failure("transient")
        breaker.record_success()
        breaker.record_failure("transient")
        assert not breaker.is_open()

    def test_success_between_different_kinds_resets(self):
        breaker = BatchCircuitBreaker(threshold=2)
        breaker.record_failure("transient")
        breaker.record_success()
        breaker.record_failure("rate-limit")
        assert not breaker.is_open()

    def test_latch_stays_open(self):
        breaker = BatchCircuitBreaker(threshold=2)
        breaker.record_failure("transient")
        breaker.record_failure("transient")
        assert breaker.is_open()
        breaker.record_success()
        assert breaker.is_open()

    def test_threshold_of_one(self):
        breaker = BatchCircuitBreaker(threshold=1)
        breaker.record_failure("transient")
        assert breaker.is_open()

    def test_thread_safety(self):
        breaker = BatchCircuitBreaker(threshold=100)
        errors = []

        def fail():
            try:
                for _ in range(50):
                    breaker.record_failure("transient")
            except Exception as e:
                errors.append(e)

        def succeed():
            try:
                for _ in range(50):
                    breaker.record_success()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=fail) for _ in range(3)]
        threads += [threading.Thread(target=succeed) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


class TestEnvelopeHelpers:
    def _held(self):
        return {
            "id": "held-1",
            "origin_tool_call_id": "tc-1",
            "tool_name": "delete_todoist_task",
            "args": {"task_id": "123"},
            "hash": "abc",
        }

    def test_timeout_envelope(self):
        result = timeout_error_envelope(self._held(), 45.0)
        assert result["success"] is False
        assert result["tool_call_id"] == "tc-1"
        assert result["tool_name"] == "delete_todoist_task"
        assert "timed out" in result["error"]
        assert "45" in result["error"]

    def test_circuit_breaker_envelope(self):
        result = circuit_breaker_error_envelope(self._held(), "transient")
        assert result["success"] is False
        assert result["tool_call_id"] == "tc-1"
        assert "circuit breaker" in result["error"]
        assert "transient" in result["error"]
