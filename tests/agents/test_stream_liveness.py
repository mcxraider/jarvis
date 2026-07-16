"""Focused liveness coverage for native async streaming producers."""

import asyncio
import json
from unittest.mock import patch

from agents.agent_api.app.api.routes.invoke import stream_agent_run


async def _run_and_collect(run_callable) -> list[dict]:
    response = await stream_agent_run(run_callable)
    return [json.loads(chunk) async for chunk in response.body_iterator]


def test_normal_stream_delivers_final_event() -> None:
    def run_callable(_tracer):
        return {
            "final_response": "Done!",
            "thread_id": "t1",
            "error": None,
        }

    events = asyncio.run(_run_and_collect(run_callable))

    assert events[-1]["type"] == "final"
    assert events[-1]["response"]["status"] == "completed"


def test_producer_failure_delivers_failed_final_event() -> None:
    async def run_callable(_tracer):
        await asyncio.sleep(0)
        raise RuntimeError("producer failed")

    events = asyncio.run(_run_and_collect(run_callable))

    assert events == [
        {
            "type": "final",
            "response": {
                "status": "failed",
                "thread_id": "",
                "response": (
                    "Jarvis is temporarily unavailable. Please try again in a moment."
                ),
                "tool_results": [],
                "error": "producer failed",
            },
        }
    ]


def test_slow_async_producer_completes_without_liveness_polling() -> None:
    async def run_callable(_tracer):
        await asyncio.sleep(0.05)
        return {
            "final_response": "Slow but done",
            "thread_id": "t2",
            "error": None,
        }

    events = asyncio.run(_run_and_collect(run_callable))

    assert events[-1]["response"]["status"] == "completed"
    assert events[-1]["response"]["response"] == "Slow but done"


def test_final_event_has_priority_when_progress_queue_is_saturated() -> None:
    def run_callable(tracer):
        for index in range(10):
            tracer.progress({"phase": "tool", "index": index})
        return {
            "final_response": "Done",
            "thread_id": "t3",
            "error": None,
        }

    with patch("agents.agent_api.app.api.routes.invoke.STREAM_QUEUE_MAX", 2):
        events = asyncio.run(_run_and_collect(run_callable))

    assert events[-1]["type"] == "final"
    assert events[-1]["response"]["status"] == "completed"
    assert len([event for event in events if event["type"] == "progress"]) < 10
