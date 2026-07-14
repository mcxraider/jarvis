"""Tests for the stream liveness timeout in invoke.py's stream_agent_run."""

import asyncio
import json
import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agents.agent_api.app.api.routes.invoke import stream_agent_run


async def _collect_stream(response) -> list:
    """Collect all events from a StreamingResponse's async body iterator."""
    events = []
    async for chunk in response.body_iterator:
        events.append(chunk)
    return events


def collect_stream(response) -> list:
    """Synchronous wrapper to collect async stream events."""
    return asyncio.run(_collect_stream(response))


class TestNormalStreamDelivery:
    def test_delivers_events_and_sentinel(self):
        """Happy path: worker produces progress + final + sentinel."""
        def run_callable(tracer):
            return {
                "final_response": "Done!",
                "thread_id": "t1",
                "error": None,
            }

        response = stream_agent_run(run_callable)
        events = collect_stream(response)
        parsed = [json.loads(e) for e in events]

        assert any(ev["type"] == "final" for ev in parsed)
        final = next(ev for ev in parsed if ev["type"] == "final")
        assert final["response"]["status"] == "completed"


class TestDeadThreadDetection:
    @patch(
        "agents.agent_api.app.api.routes.invoke.STREAM_LIVENESS_TIMEOUT_SECONDS",
        0.1,
    )
    def test_dead_thread_emits_error_and_stops(self):
        """If worker dies without sentinel, iterator emits error instead of hanging.

        We simulate this by patching the queue so put(None) is swallowed — emulating
        a scenario where the finally block can't deliver the sentinel (e.g. the queue
        itself is broken, or the thread was killed at the OS level).
        """
        original_queue_cls = queue.Queue

        class SentinelSwallowingQueue(original_queue_cls):
            def put(self, item, *a, **kw):
                if item is None:
                    return  # swallow sentinel
                super().put(item, *a, **kw)

        def run_callable(tracer):
            return {
                "final_response": "Done",
                "thread_id": "t1",
                "error": None,
            }

        with patch("agents.agent_api.app.api.routes.invoke.queue.Queue", SentinelSwallowingQueue):
            response = stream_agent_run(run_callable)
            start = time.monotonic()
            events = collect_stream(response)
            elapsed = time.monotonic() - start

        # Should detect dead thread within a few timeout cycles, not hang
        assert elapsed < 2.0

        parsed = [json.loads(e) for e in events]
        assert len(parsed) >= 1
        final = parsed[-1]
        assert final["type"] == "final"
        assert final["response"]["status"] == "failed"


class TestSlowWorkerNoFalseAlarm:
    @patch(
        "agents.agent_api.app.api.routes.invoke.STREAM_LIVENESS_TIMEOUT_SECONDS",
        0.2,
    )
    def test_slow_worker_completes_normally(self):
        """Worker that takes longer than one timeout cycle still succeeds."""
        def run_callable(tracer):
            time.sleep(0.3)
            return {
                "final_response": "Slow but done",
                "thread_id": "t2",
                "error": None,
            }

        response = stream_agent_run(run_callable)
        events = collect_stream(response)
        parsed = [json.loads(e) for e in events]

        final = next(ev for ev in parsed if ev["type"] == "final")
        assert final["response"]["status"] == "completed"
        assert "Slow but done" in final["response"]["response"]
