"""Focused tests for bounded durable thread memory."""

import asyncio
import base64
import functools
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from agents.agent_api.app.api.schemas import InvokeRequest, MemoryResetRequest
from agents.agent_api.app.graph import builder as builder_module
from agents.agent_api.app.thread_memory import (
    PREVIOUS_THREAD_TEXT_LIMIT,
    _download_image,
    _load_historical_images,
    build_memory_snapshot,
    canonical_memory_messages,
    persist_thread_memory_async,
    prepare_previous_thread_memory_async,
    render_previous_thread_context,
)
from agents.agent_api.app.user_context.identity import TelegramIdentity


IDENTITY = TelegramIdentity(telegram_id=123456, username="tester")
CONVERSATION_KEY = "telegram-chat:" + "a" * 32


def async_test(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def _tool_call(call_id: str, *, secret: bool = False) -> dict:
    arguments = {"query": "today"}
    if secret:
        arguments["api_key"] = "must-not-survive"
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "get_tasks", "arguments": json.dumps(arguments)},
    }


def test_contract_accepts_scoped_memory_and_requires_scope_for_reset() -> None:
    request = InvokeRequest.model_validate(
        {
            "message": "hello",
            "user_id": "user",
            "telegram_identity": {"telegram_id": 123456},
            "conversation_key": CONVERSATION_KEY,
            "reset_memory": True,
        }
    )
    assert request.conversation_key == CONVERSATION_KEY
    assert request.reset_memory is True

    with pytest.raises(ValueError, match="conversation_key"):
        InvokeRequest.model_validate(
            {"message": "hello", "user_id": "user", "reset_memory": True}
        )
    with pytest.raises(ValueError, match="telegram_identity"):
        InvokeRequest.model_validate(
            {
                "message": "hello",
                "user_id": "user",
                "conversation_key": CONVERSATION_KEY,
                "reset_memory": True,
            }
        )
    with pytest.raises(ValueError, match="telegram_identity"):
        MemoryResetRequest.model_validate(
            {"user_id": "user", "conversation_key": CONVERSATION_KEY}
        )


def test_canonical_messages_round_trip_visible_payload_and_strip_secrets() -> None:
    messages = [
        {"role": "system", "content": "private system prompt"},
        {"role": "user", "content": "show today"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("call-1", secret=True)],
            "reasoning_content": "hidden",
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": json.dumps(
                {"tasks": [{"id": "1", "title": "Visible"}], "access_token": "no"}
            ),
        },
        {"role": "assistant", "content": "Visible answer"},
    ]

    result = canonical_memory_messages(messages)

    assert [message["role"] for message in result] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert result[0] == {"role": "user", "content": "show today"}
    assert "must-not-survive" not in json.dumps(result)
    assert "hidden" not in json.dumps(result)
    assert "private system prompt" not in json.dumps(result)
    assert "Visible" in json.dumps(result)
    assert result[1]["tool_calls"][0]["id"] == "call-1"
    assert result[2]["tool_call_id"] == "call-1"


def test_context_limit_never_splits_assistant_tool_group() -> None:
    rows = [
        {"sequence": 0, "kind": "user", "payload": {"role": "user", "content": "old"}},
        {
            "sequence": 1,
            "kind": "assistant",
            "payload": {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call-1")],
            },
        },
        {
            "sequence": 2,
            "kind": "tool",
            "payload": {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "x" * 600,
            },
        },
        {"sequence": 3, "kind": "user", "payload": {"role": "user", "content": "new"}},
    ]

    text, retained = render_previous_thread_context(
        rows,
        "previous",
        "failed",
        limit=430,
    )

    assert len(text) <= 430
    assert "new" in text
    assert "call-1" not in text
    assert "[older previous-thread content omitted]" in text
    assert retained == {3}


def test_snapshot_is_idempotent_and_never_contains_raw_image_bytes() -> None:
    image_bytes = b"\xff\xd8\xff\xd9"
    image = {
        "image_url": "data:image/jpeg;base64,"
        + base64.b64encode(image_bytes).decode("ascii"),
        "detail": "high",
    }
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "inspect this"},
        {"role": "assistant", "content": "done"},
    ]

    first = build_memory_snapshot(
        messages, [image], None, canonical_user_id="user", thread_id="thread"
    )
    second = build_memory_snapshot(
        messages, [image], None, canonical_user_id="user", thread_id="thread"
    )

    assert first[0] == second[0]
    assert first[1][0].payload == second[1][0].payload
    payload = first[1][0].payload
    assert payload["object_path"] == (
        f"user/thread/{hashlib.sha256(image_bytes).hexdigest()}.jpg"
    )
    assert payload["uploaded"] is False
    assert "base64" not in json.dumps(first[0]).lower()


@async_test
async def test_prepare_timeout_fails_open_but_reset_fails_closed(monkeypatch) -> None:
    cancelled = asyncio.Event()

    async def slow_prepare(*_args, **_kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    monkeypatch.setattr(
        "agents.agent_api.app.thread_memory._prepare_rows", slow_prepare
    )
    monkeypatch.setattr(
        "agents.agent_api.app.thread_memory.PREVIOUS_THREAD_DB_TIMEOUT_SECONDS",
        0.01,
    )
    memory = await prepare_previous_thread_memory_async(
        identity=IDENTITY,
        conversation_key=CONVERSATION_KEY,
        current_thread_id="thread",
        user_prompt="hello",
    )
    assert memory.outcome == "db_timeout"
    assert cancelled.is_set()

    with pytest.raises(TimeoutError):
        await prepare_previous_thread_memory_async(
            identity=IDENTITY,
            conversation_key=CONVERSATION_KEY,
            current_thread_id="thread",
            user_prompt="hello",
            reset_memory=True,
        )


@async_test
async def test_prepare_uses_one_database_call(monkeypatch) -> None:
    class Cursor:
        description = [
            SimpleNamespace(name=name)
            for name in (
                "user_id",
                "lineage_id",
                "previous_thread_id",
                "previous_status",
                "sequence",
                "kind",
                "payload",
            )
        ]

        def __init__(self):
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, query, params):
            self.calls += 1
            self.query = query
            self.params = params

        async def fetchall(self):
            return [("user", "lineage", None, None, None, None, None)]

    cursor = Cursor()

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def cursor(self):
            return cursor

    class Pool:
        def connection(self):
            return Connection()

    monkeypatch.setattr(
        "agents.agent_api.app.thread_memory.get_async_pool", lambda: Pool()
    )
    memory = await prepare_previous_thread_memory_async(
        identity=IDENTITY,
        conversation_key=CONVERSATION_KEY,
        current_thread_id="thread",
        user_prompt="hello",
    )

    assert cursor.calls == 1
    assert "prepare_thread_memory" in cursor.query
    assert memory.canonical_user_id == "user"
    assert memory.outcome == "no_predecessor"


@async_test
async def test_malformed_history_fails_open(monkeypatch) -> None:
    async def malformed_rows(*_args, **_kwargs):
        return [
            {
                "user_id": "user",
                "lineage_id": "lineage",
                "previous_thread_id": "previous",
                "previous_status": "completed",
                "sequence": 0,
                "kind": "user",
                "payload": {"role": "user", "content": object()},
            }
        ]

    monkeypatch.setattr(
        "agents.agent_api.app.thread_memory._prepare_rows", malformed_rows
    )
    memory = await prepare_previous_thread_memory_async(
        identity=IDENTITY,
        conversation_key=CONVERSATION_KEY,
        current_thread_id="thread",
        user_prompt="hello",
    )
    assert memory.outcome == "memory_error"
    assert memory.text == ""


@async_test
async def test_builder_starts_lookup_before_context_and_cancels_it_on_setup_error(
    monkeypatch,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def lookup(**_kwargs):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    async def fail_context(_identity):
        await asyncio.wait_for(started.wait(), timeout=0.2)
        raise RuntimeError("context failed")

    monkeypatch.setattr(
        builder_module, "settings", SimpleNamespace(postgres_dsn="postgresql://test")
    )
    monkeypatch.setattr(builder_module, "prepare_previous_thread_memory_async", lookup)
    monkeypatch.setattr(builder_module, "resolve_runtime_context_async", fail_context)

    with pytest.raises(RuntimeError, match="context failed"):
        await builder_module.run_jarvis_async(
            user_prompt="hello",
            thread_id="thread",
            identity=IDENTITY,
            conversation_key=CONVERSATION_KEY,
            checkpointer=object(),
        )

    assert cancelled.is_set()


@async_test
async def test_builder_does_not_start_predecessor_lookup_for_resume(monkeypatch) -> None:
    lookup_calls = 0

    async def lookup(**_kwargs):
        nonlocal lookup_calls
        lookup_calls += 1
        return None

    async def fail_context(_thread_id, _identity):
        raise RuntimeError("resume context failed")

    monkeypatch.setattr(
        builder_module, "settings", SimpleNamespace(postgres_dsn="postgresql://test")
    )
    monkeypatch.setattr(builder_module, "prepare_previous_thread_memory_async", lookup)
    monkeypatch.setattr(builder_module, "load_thread_runtime_context_async", fail_context)

    with pytest.raises(RuntimeError, match="resume context failed"):
        await builder_module.run_jarvis_async(
            user_prompt="yes",
            thread_id="thread",
            clarification_reply="yes",
            identity=IDENTITY,
            conversation_key=CONVERSATION_KEY,
            checkpointer=object(),
        )

    assert lookup_calls == 0


@async_test
async def test_historical_image_timeout_keeps_completed_downloads(monkeypatch) -> None:
    slow_cancelled = asyncio.Event()

    async def download(payload):
        if payload["sha256"] == "fast":
            return {"image_url": "fast", "detail": "high"}
        try:
            await asyncio.sleep(60)
        finally:
            slow_cancelled.set()

    monkeypatch.setattr(
        "agents.agent_api.app.thread_memory._download_image", download
    )
    monkeypatch.setattr(
        "agents.agent_api.app.thread_memory.PREVIOUS_THREAD_IMAGE_TIMEOUT_SECONDS",
        0.01,
    )
    rows = [
        {
            "sequence": index,
            "kind": "image",
            "payload": {
                "uploaded": True,
                "user_message_sequence": 0,
                "bytes": 1,
                "sha256": digest,
            },
        }
        for index, digest in enumerate(("fast", "slow"), start=1)
    ]

    images = await _load_historical_images(
        rows,
        {0},
        current_image_count=0,
        current_image_bytes=0,
    )

    assert images == ({"image_url": "fast", "detail": "high"},)
    assert slow_cancelled.is_set()


@async_test
async def test_current_images_exhaust_capacity_before_history(monkeypatch) -> None:
    async def unexpected_download(_payload):
        raise AssertionError("historical image should not be downloaded")

    monkeypatch.setattr(
        "agents.agent_api.app.thread_memory._download_image", unexpected_download
    )
    images = await _load_historical_images(
        [
            {
                "sequence": 1,
                "kind": "image",
                "payload": {
                    "uploaded": True,
                    "user_message_sequence": 0,
                    "bytes": 1,
                    "sha256": "unused",
                },
            }
        ],
        {0},
        current_image_count=10,
        current_image_bytes=1,
    )
    assert images == ()


@async_test
async def test_download_validates_mime_size_and_hash(monkeypatch) -> None:
    data = b"\xff\xd8\xff\xd9"

    class Client:
        async def get(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=data,
                request=httpx.Request("GET", "https://example.test/image"),
            )

    monkeypatch.setattr(
        "agents.agent_api.app.thread_memory._storage_configured", lambda: True
    )
    monkeypatch.setattr(
        "agents.agent_api.app.thread_memory.get_thread_memory_storage_client",
        lambda: Client(),
    )
    digest = hashlib.sha256(data).hexdigest()
    payload = {
        "uploaded": True,
        "object_path": f"user/thread/{digest}.jpg",
        "mime_type": "image/jpeg",
        "bytes": len(data),
        "sha256": digest,
        "detail": "auto",
    }
    downloaded = await _download_image(payload)
    assert downloaded == {
        "image_url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"),
        "detail": "auto",
    }

    payload["sha256"] = "0" * 64
    assert await _download_image(payload) is None


@async_test
async def test_repeated_persistence_replaces_rows_and_marks_complete(monkeypatch) -> None:
    statements = []

    class Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, query, params):
            statements.append((" ".join(query.split()), params))

        async def executemany(self, query, params):
            statements.append((" ".join(query.split()), list(params)))

    class Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

        def transaction(self):
            return Context()

    class Pool:
        def connection(self):
            return Context()

    monkeypatch.setattr(
        "agents.agent_api.app.thread_memory.get_async_pool", lambda: Pool()
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    kwargs = dict(
        thread_id="thread",
        canonical_user_id="user",
        conversation_key=CONVERSATION_KEY,
        terminal_status="completed",
        messages=messages,
    )
    assert await persist_thread_memory_async(**kwargs) is True
    assert await persist_thread_memory_async(**kwargs) is True

    deletes = [query for query, _params in statements if query.startswith("DELETE")]
    inserts = [query for query, _params in statements if query.startswith("INSERT")]
    completes = [
        params
        for query, params in statements
        if "SET memory_status = %s" in query and params[0] == "complete"
    ]
    assert len(deletes) == 2
    assert len(inserts) == 2
    assert len(completes) == 2

    image = {
        "image_url": "data:image/jpeg;base64,"
        + base64.b64encode(b"\xff\xd8\xff\xd9").decode("ascii"),
        "detail": "high",
    }
    monkeypatch.setattr(
        "agents.agent_api.app.thread_memory._storage_configured", lambda: False
    )
    assert await persist_thread_memory_async(**kwargs, images=[image]) is False
    assert any(
        params[0] == "incomplete"
        for query, params in statements
        if "SET memory_status = %s" in query
    )


def test_default_context_budget_is_fixed() -> None:
    assert PREVIOUS_THREAD_TEXT_LIMIT == 40_000
