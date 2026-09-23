"""Lazy image recall: on-demand fetch + promotion, caps, fail-open, provider gating."""

import asyncio
import base64
import functools
import json
from types import SimpleNamespace

from agents.agent_api.app.graph.nodes import tools as tools_node
from agents.agent_api.app.graph.run_deps import RunDeps
from agents.agent_api.app.tools import registry_factory


def async_test(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


_JPEG = b"\xff\xd8\xff\xd9"
_IMAGE = {
    "image_url": "data:image/jpeg;base64," + base64.b64encode(_JPEG).decode("ascii"),
    "detail": "auto",
}


def _recall_call(recall_id: str) -> dict:
    return {
        "id": "call-1",
        "type": "function",
        "function": {
            "name": "recall_previous_image",
            "arguments": json.dumps({"recall_id": recall_id}),
        },
    }


def _deps(**overrides) -> RunDeps:
    deps = RunDeps()
    deps.recallable_images = {"abc": {"sha256": "abc", "bytes": len(_JPEG)}}
    for key, value in overrides.items():
        setattr(deps, key, value)
    return deps


@async_test
async def test_recall_fetches_and_promotes_to_image_batch(monkeypatch) -> None:
    async def fetch(ref):
        assert ref["sha256"] == "abc"
        return dict(_IMAGE)

    monkeypatch.setattr(tools_node, "fetch_previous_image_by_reference", fetch)
    deps = _deps()

    result = await tools_node._resolve_recall_call(
        _recall_call("abc"), deps, tools_node.NULL_TRACE
    )

    assert result["success"] is True
    assert deps.images == (_IMAGE,)


@async_test
async def test_recall_unknown_id_leaves_images_untouched(monkeypatch) -> None:
    async def fetch(_ref):
        raise AssertionError("unknown id must not fetch")

    monkeypatch.setattr(tools_node, "fetch_previous_image_by_reference", fetch)
    deps = _deps()

    result = await tools_node._resolve_recall_call(
        _recall_call("nope"), deps, tools_node.NULL_TRACE
    )

    assert result["success"] is False
    assert deps.images == ()


@async_test
async def test_recall_fetch_failure_fails_open(monkeypatch) -> None:
    async def fetch(_ref):
        return None

    monkeypatch.setattr(tools_node, "fetch_previous_image_by_reference", fetch)
    deps = _deps()

    result = await tools_node._resolve_recall_call(
        _recall_call("abc"), deps, tools_node.NULL_TRACE
    )

    assert result["success"] is False
    assert deps.images == ()


@async_test
async def test_recall_respects_count_cap(monkeypatch) -> None:
    async def fetch(_ref):
        raise AssertionError("count cap must be checked before fetching")

    monkeypatch.setattr(tools_node, "fetch_previous_image_by_reference", fetch)
    monkeypatch.setattr(tools_node, "MAX_IMAGE_COUNT", 1)
    deps = _deps(images=(_IMAGE,))

    result = await tools_node._resolve_recall_call(
        _recall_call("abc"), deps, tools_node.NULL_TRACE
    )

    assert result["success"] is False
    assert deps.images == (_IMAGE,)


@async_test
async def test_recall_respects_byte_cap(monkeypatch) -> None:
    async def fetch(_ref):
        return dict(_IMAGE)

    monkeypatch.setattr(tools_node, "fetch_previous_image_by_reference", fetch)
    monkeypatch.setattr(tools_node, "MAX_IMAGE_BYTES", len(_JPEG) - 1)
    deps = _deps()

    result = await tools_node._resolve_recall_call(
        _recall_call("abc"), deps, tools_node.NULL_TRACE
    )

    assert result["success"] is False
    assert deps.images == ()


def test_recall_tool_registered_only_on_vision_provider(monkeypatch) -> None:
    class FakeVision:
        pass

    monkeypatch.setattr(registry_factory, "OpenAIResponsesProfile", FakeVision)

    monkeypatch.setattr(
        registry_factory, "settings", SimpleNamespace(orchestrator_llm=FakeVision())
    )
    assert "recall_previous_image" in registry_factory.build_registry_from_clients()

    monkeypatch.setattr(
        registry_factory, "settings", SimpleNamespace(orchestrator_llm=object())
    )
    assert "recall_previous_image" not in registry_factory.build_registry_from_clients()
