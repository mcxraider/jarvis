"""Barrier-based Stage 7 tests for bounded native async summarization."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest

from agents.agent_api.app.config import load_settings
from agents.agent_api.app.constants import SUMMARIZE_THRESHOLD
from agents.agent_api.app.graph.nodes.summarize import create_summarize_node


def _items(prefix: str) -> list[dict[str, str]]:
    return [
        {"id": f"{prefix}-{index}", "content": f"{prefix} task {index}"}
        for index in range(SUMMARIZE_THRESHOLD + 5)
    ]


def _tool_message(prefix: str, *, content_as_dict: bool = False) -> dict[str, Any]:
    envelope = {"content": _items(prefix), "tool_name": f"tool_{prefix}"}
    return {
        "role": "tool",
        "name": f"tool_{prefix}",
        "tool_call_id": f"call_{prefix}",
        "content": envelope if content_as_dict else json.dumps(envelope),
    }


def _state(*prefixes: str, content_as_dict: bool = False) -> dict[str, Any]:
    return {
        "user_prompt": "show the relevant tasks",
        "messages": [
            {"role": "assistant", "content": "checking"},
            *[
                _tool_message(prefix, content_as_dict=content_as_dict)
                for prefix in prefixes
            ],
        ],
    }


def _ids_from_request(kwargs: dict[str, Any]) -> list[str]:
    content = "\n".join(message["content"] for message in kwargs["messages"])
    return re.findall(r'"id":\s*"([^"]+)"', content)


def _response(summary: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=summary))]
    )


class _AsyncClient:
    def __init__(self, create):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=create),
        )


def _envelope(result: dict[str, Any], index: int) -> dict[str, Any]:
    return json.loads(result["messages"][index]["content"])


def test_parallel_async_summaries_overlap_and_preserve_message_mapping() -> None:
    async def run() -> None:
        active = 0
        peak = 0
        all_started = asyncio.Event()
        release = asyncio.Event()

        async def create(**kwargs):
            nonlocal active, peak
            ids = _ids_from_request(kwargs)
            active += 1
            peak = max(peak, active)
            if active == 3:
                all_started.set()
            try:
                await release.wait()
                return _response("summary " + " ".join(ids))
            finally:
                active -= 1

        node = create_summarize_node(
            client=_AsyncClient(create),
            max_concurrency=3,
        )
        task = asyncio.create_task(node(_state("alpha", "beta", "gamma")))
        await asyncio.wait_for(all_started.wait(), timeout=1)
        assert peak == 3
        release.set()
        result = await task

        for index, prefix in enumerate(("alpha", "beta", "gamma"), start=1):
            envelope = _envelope(result, index)
            assert envelope["summarized"] is True
            assert envelope["original_item_count"] == SUMMARIZE_THRESHOLD + 5
            assert f"{prefix}-0" in envelope["content"]
            assert all(
                f"{other}-0" not in envelope["content"]
                for other in {"alpha", "beta", "gamma"} - {prefix}
            )

    asyncio.run(run())


def test_parallel_async_summaries_share_limit_across_runs() -> None:
    async def run() -> None:
        active = 0
        peak = 0
        started = 0
        two_started = asyncio.Event()
        release = asyncio.Event()

        async def create(**kwargs):
            nonlocal active, peak, started
            ids = _ids_from_request(kwargs)
            active += 1
            started += 1
            peak = max(peak, active)
            if started == 2:
                two_started.set()
            try:
                await release.wait()
                return _response("summary " + " ".join(ids))
            finally:
                active -= 1

        client = _AsyncClient(create)
        first_node = create_summarize_node(client=client, max_concurrency=2)
        second_node = create_summarize_node(client=client, max_concurrency=2)
        first = asyncio.create_task(first_node(_state("a", "b")))
        second = asyncio.create_task(second_node(_state("c", "d")))

        await asyncio.wait_for(two_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert started == 2
        assert peak == 2
        release.set()
        await asyncio.gather(first, second)
        assert started == 4
        assert peak == 2

    asyncio.run(run())


def test_parallel_validation_failure_isolated_and_input_envelope_unchanged() -> None:
    async def run() -> None:
        attempts: Counter[str] = Counter()

        async def create(**kwargs):
            ids = _ids_from_request(kwargs)
            prefix = ids[0].split("-", 1)[0]
            attempts[prefix] += 1
            if prefix == "bad":
                return _response("missing ids")
            return _response("summary " + " ".join(ids))

        state = _state("bad", "good", content_as_dict=True)
        original_bad_envelope = state["messages"][1]["content"]
        node = create_summarize_node(
            client=_AsyncClient(create),
            max_concurrency=2,
        )

        result = await node(state)

        bad = _envelope(result, 1)
        good = _envelope(result, 2)
        assert attempts == Counter({"bad": 2, "good": 1})
        assert "showing first" in bad["content"]
        assert "good-0" in good["content"]
        assert "summarized" not in original_bad_envelope

    asyncio.run(run())


def test_parallel_cancellation_releases_shared_permit() -> None:
    async def run() -> None:
        entered = asyncio.Event()
        blocker = asyncio.Event()

        async def blocked_create(**_kwargs):
            entered.set()
            await blocker.wait()
            raise AssertionError("cancelled provider call should not resume")

        blocked_node = create_summarize_node(
            client=_AsyncClient(blocked_create),
            max_concurrency=1,
        )
        task = asyncio.create_task(blocked_node(_state("blocked")))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        async def immediate_create(**kwargs):
            ids = _ids_from_request(kwargs)
            return _response("summary " + " ".join(ids))

        next_node = create_summarize_node(
            client=_AsyncClient(immediate_create),
            max_concurrency=1,
        )
        result = await asyncio.wait_for(next_node(_state("next")), timeout=1)
        assert "next-0" in _envelope(result, 1)["content"]

    asyncio.run(run())


def test_summarizer_max_concurrency_defaults_to_four(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_SUMMARIZER_MAX_CONCURRENCY", raising=False)
    assert load_settings().summarizer_max_concurrency == 4


def test_summarizer_max_concurrency_must_be_positive(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SUMMARIZER_MAX_CONCURRENCY", "0")
    with pytest.raises(ValueError, match="must be greater than zero"):
        load_settings()
