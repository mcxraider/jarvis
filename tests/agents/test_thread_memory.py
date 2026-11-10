"""Focused tests for bounded durable thread memory."""

import asyncio
import base64
import functools
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from agents.agent_api.app import thread_memory as thread_memory_module
from agents.agent_api.app.api.schemas import InvokeRequest, MemoryResetRequest
from agents.agent_api.app.graph import builder as builder_module
from agents.agent_api.app.thread_memory import (
    PREVIOUS_THREAD_COUNT,
    PREVIOUS_THREAD_TEXT_LIMIT,
    ROUTER_HISTORY_TEXT_LIMIT,
    _HISTORY_PREAMBLE,
    _OMISSION_MARKER,
    _build_image_references,
    _download_image,
    build_memory_snapshot,
    canonical_memory_messages,
    load_resumed_thread_memory_async,
    persist_thread_memory_async,
    prepare_previous_thread_memory_async,
    render_previous_thread_context,
    render_router_history,
)
from agents.agent_api.app.user_context.identity import TelegramIdentity


IDENTITY = TelegramIdentity(telegram_id=123456, username="tester")
CONVERSATION_KEY = "telegram-chat:" + "a" * 32


def _history_rows(rows, thread_id="previous", rank=1):
    return [
        {**row, "source_thread_id": thread_id, "history_rank": rank}
        for row in rows
    ]


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


def test_context_drops_tool_calls_and_results() -> None:
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
        {
            "sequence": 3,
            "kind": "assistant",
            "payload": {"role": "assistant", "content": "done"},
        },
        {"sequence": 4, "kind": "user", "payload": {"role": "user", "content": "new"}},
    ]

    text, retained = render_previous_thread_context(_history_rows(rows))

    # Only {role, content} user/assistant turns survive.
    assert "new" in text
    assert "done" in text
    assert "old" in text
    # Tool scaffolding and metadata/label are gone.
    assert "call-1" not in text
    assert "tool_call_id" not in text
    assert "tool_calls" not in text
    assert "Previous thread:" not in text
    assert "Messages:" not in text
    assert retained == {("previous", 0), ("previous", 3), ("previous", 4)}


def test_context_keeps_image_reference_for_retained_user() -> None:
    rows = [
        {"sequence": 0, "kind": "user", "payload": {"role": "user", "content": "hi"}},
        {
            "sequence": 1,
            "kind": "image",
            "payload": {
                "user_message_sequence": 0,
                "mime_type": "image/jpeg",
                "sha256": "abc",
                "bytes": 4,
                "uploaded": True,
            },
        },
    ]

    text, retained = render_previous_thread_context(_history_rows(rows))

    assert "image_reference" in text
    assert '"sha256":"abc"' in text
    assert retained == {("previous", 0)}


def test_context_renders_two_threads_oldest_first_with_one_global_budget() -> None:
    rows = [
        *_history_rows(
            [
                {
                    "sequence": 0,
                    "kind": "user",
                    "payload": {"role": "user", "content": "newest old"},
                },
                {
                    "sequence": 1,
                    "kind": "assistant",
                    "payload": {"role": "assistant", "content": "newest answer"},
                },
            ],
            "thread-b",
            1,
        ),
        *_history_rows(
            [
                {
                    "sequence": 0,
                    "kind": "user",
                    "payload": {"role": "user", "content": "older old"},
                }
            ],
            "thread-a",
            2,
        ),
    ]

    text, retained = render_previous_thread_context(rows)

    assert text.index("Previous thread 2") < text.index("Previous thread 1")
    assert "newest answer" in text
    assert "older old" in text
    assert retained == {("thread-b", 0), ("thread-b", 1), ("thread-a", 0)}
    assert len(text) <= PREVIOUS_THREAD_TEXT_LIMIT


def test_context_renders_three_threads_from_one_count_change(monkeypatch) -> None:
    monkeypatch.setattr(thread_memory_module, "PREVIOUS_THREAD_COUNT", 3)
    rows = [
        *_history_rows(
            [
                {
                    "sequence": 0,
                    "kind": "user",
                    "payload": {"role": "user", "content": "newest"},
                }
            ],
            "thread-c",
            1,
        ),
        *_history_rows(
            [
                {
                    "sequence": 0,
                    "kind": "user",
                    "payload": {"role": "user", "content": "middle"},
                }
            ],
            "thread-b",
            2,
        ),
        *_history_rows(
            [
                {
                    "sequence": 0,
                    "kind": "user",
                    "payload": {"role": "user", "content": "oldest"},
                }
            ],
            "thread-a",
            3,
        ),
    ]

    text, _retained = render_previous_thread_context(rows)

    assert text.index("Previous thread 3") < text.index("Previous thread 2")
    assert text.index("Previous thread 2") < text.index("Previous thread 1")


def test_context_budget_prioritizes_newest_thread_and_marks_omission() -> None:
    rows = [
        *_history_rows(
            [
                {
                    "sequence": sequence,
                    "kind": "user",
                    "payload": {"role": "user", "content": f"new-{sequence}-" + "x" * 80},
                }
                for sequence in range(4)
            ],
            "thread-b",
            1,
        ),
        *_history_rows(
            [
                {
                    "sequence": 0,
                    "kind": "user",
                    "payload": {"role": "user", "content": "older-thread"},
                }
            ],
            "thread-a",
            2,
        ),
    ]

    text, retained = render_previous_thread_context(rows, limit=420)

    assert len(text) <= 420
    assert "older content from this thread omitted" in text
    assert ("thread-b", 3) in retained
    assert not any(thread_id == "thread-a" for thread_id, _sequence in retained)


def test_image_references_do_not_collide_on_cross_thread_sequence() -> None:
    rows = [
        *_history_rows(
            [
                {
                    "sequence": 0,
                    "kind": "user",
                    "payload": {"role": "user", "content": "retained"},
                },
                {
                    "sequence": 1,
                    "kind": "image",
                    "payload": {
                        "user_message_sequence": 0,
                        "uploaded": True,
                        "sha256": "new-image",
                    },
                },
            ],
            "thread-b",
            1,
        ),
        *_history_rows(
            [
                {
                    "sequence": 1,
                    "kind": "image",
                    "payload": {
                        "user_message_sequence": 0,
                        "uploaded": True,
                        "sha256": "old-image",
                    },
                }
            ],
            "thread-a",
            2,
        ),
    ]

    references = _build_image_references(rows, {("thread-b", 0)})

    assert [reference["sha256"] for reference in references] == ["new-image"]


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
                "source_thread_id",
                "history_rank",
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
    assert cursor.params[-1] == PREVIOUS_THREAD_COUNT
    assert memory.canonical_user_id == "user"
    assert memory.outcome == "no_predecessor"


@async_test
async def test_malformed_history_fails_open(monkeypatch) -> None:
    async def malformed_rows(*_args, **_kwargs):
        return [
            {
                "user_id": "user",
                "lineage_id": "lineage",
                "source_thread_id": "previous",
                "history_rank": 1,
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
async def test_non_contiguous_history_ranks_fail_open(monkeypatch) -> None:
    async def malformed_rows(*_args, **_kwargs):
        return _history_rows(
            [
                {
                    "user_id": "user",
                    "lineage_id": "lineage",
                    "sequence": 0,
                    "kind": "user",
                    "payload": {"role": "user", "content": "old"},
                }
            ],
            "previous",
            2,
        )

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

    async def resume_lookup(**_kwargs):
        # The read-only resume loader now runs on resume; keep it off the DB.
        return thread_memory_module.PreviousThreadMemory(outcome="not_registered")

    monkeypatch.setattr(
        builder_module, "settings", SimpleNamespace(postgres_dsn="postgresql://test")
    )
    monkeypatch.setattr(builder_module, "prepare_previous_thread_memory_async", lookup)
    monkeypatch.setattr(
        builder_module, "load_resumed_thread_memory_async", resume_lookup
    )
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


class _CapturedDeps(Exception):
    """Sentinel raised by the fake graph after it captures RunDeps."""


def _patch_builder_for_resume(monkeypatch, *, resume_enabled: bool):
    """Drive run_jarvis_async on the resume path up to graph invocation.

    Returns (resume_calls, captured) lists. The fake graph records the RunDeps
    injected through the LangGraph config, then raises a sentinel.
    """

    import dataclasses

    resume_calls: list[dict] = []
    captured: list = []

    async def prepare_lookup(**_kwargs):
        raise AssertionError("prepare_thread_memory must never run on resume")

    async def resume_lookup(**kwargs):
        resume_calls.append(kwargs)
        return thread_memory_module.PreviousThreadMemory(
            canonical_user_id="user",
            lineage_id="lineage",
            previous_threads=(
                thread_memory_module.PreviousThreadRef("previous", 1),
            ),
            text="FULL_HISTORY_TEXT",
            routing_text="ROUTING_HISTORY_TEXT",
            routing_thread_count=1,
            outcome="loaded",
        )

    async def no_runtime_context(_thread_id, _identity):
        return None

    async def no_persist(**_kwargs):
        return True

    class FakeApp:
        nodes = ()

        async def ainvoke(self, _input, config):
            captured.append(config["configurable"]["deps"])
            raise _CapturedDeps()

    monkeypatch.setattr(
        builder_module,
        "settings",
        dataclasses.replace(
            builder_module.settings,
            postgres_dsn="postgresql://test",
            thread_memory_resume_enabled=resume_enabled,
        ),
    )
    monkeypatch.setattr(
        builder_module, "prepare_previous_thread_memory_async", prepare_lookup
    )
    monkeypatch.setattr(
        builder_module, "load_resumed_thread_memory_async", resume_lookup
    )
    monkeypatch.setattr(
        builder_module, "load_thread_runtime_context_async", no_runtime_context
    )
    monkeypatch.setattr(builder_module, "persist_thread_memory_async", no_persist)
    monkeypatch.setattr(
        builder_module,
        "build_registry_from_clients",
        lambda **_kwargs: builder_module.ToolRegistry(),
    )
    monkeypatch.setattr(
        builder_module, "get_or_compile_graph", lambda _checkpointer: FakeApp()
    )
    return resume_calls, captured


async def _run_resume():
    with pytest.raises(_CapturedDeps):
        await builder_module.run_jarvis_async(
            user_prompt="move it",
            thread_id="resumed-thread",
            clarification_reply="friday",
            identity=IDENTITY,
            conversation_key=CONVERSATION_KEY,
            checkpointer=object(),
            agent_client=object(),
            todoist_client=object(),
        )


@async_test
async def test_builder_loads_read_only_history_for_resume(monkeypatch) -> None:
    resume_calls, captured = _patch_builder_for_resume(
        monkeypatch, resume_enabled=True
    )

    await _run_resume()

    assert resume_calls == [{"identity": IDENTITY, "thread_id": "resumed-thread"}]
    assert len(captured) == 1
    deps = captured[0]
    assert deps.previous_thread_context == "FULL_HISTORY_TEXT"
    assert deps.previous_thread_routing_context == "ROUTING_HISTORY_TEXT"
    assert deps.previous_thread_outcome == "loaded"


@async_test
async def test_builder_skips_resume_history_when_disabled(monkeypatch) -> None:
    resume_calls, captured = _patch_builder_for_resume(
        monkeypatch, resume_enabled=False
    )

    await _run_resume()

    assert resume_calls == []
    deps = captured[0]
    assert deps.previous_thread_context == ""
    assert deps.previous_thread_routing_context == ""
    assert deps.previous_thread_outcome == "disabled"


def _router_rows() -> list[dict]:
    """Two predecessor threads whose combined render far exceeds 8,000 chars."""

    newest = [
        {
            "user_id": "user",
            "lineage_id": "lineage",
            "sequence": sequence,
            "kind": "user" if sequence % 2 == 0 else "assistant",
            "payload": {
                "role": "user" if sequence % 2 == 0 else "assistant",
                "content": f"newest-{sequence}-" + "n" * 400,
            },
        }
        for sequence in range(30)
    ]
    # An image attached to the newest user message (sequence 28).
    newest.append(
        {
            "user_id": "user",
            "lineage_id": "lineage",
            "sequence": 30,
            "kind": "image",
            "payload": {
                "user_message_sequence": 28,
                "mime_type": "image/jpeg",
                "sha256": "f" * 64,
                "bytes": 4,
                "uploaded": True,
                "object_path": "user/thread-b/x.jpg",
            },
        }
    )
    older = [
        {
            "user_id": "user",
            "lineage_id": "lineage",
            "sequence": sequence,
            "kind": "user",
            "payload": {"role": "user", "content": f"older-{sequence}-" + "o" * 400},
        }
        for sequence in range(10)
    ]
    return [
        *_history_rows(newest, "thread-b", 1),
        *_history_rows(older, "thread-a", 2),
    ]


def _assert_router_render_shape(text: str) -> None:
    assert text.startswith(_HISTORY_PREAMBLE)
    for line in text[len(_HISTORY_PREAMBLE) :].split("\n"):
        if not line or line == _OMISSION_MARKER or line.startswith("[Previous thread"):
            continue
        json.loads(line)  # every entry line is a complete JSON object


def test_router_render_is_bounded_entry_aligned_and_keeps_newest() -> None:
    rows = _router_rows()
    full_text, _ = render_previous_thread_context(rows)
    assert len(full_text) > ROUTER_HISTORY_TEXT_LIMIT  # truncation must happen

    history = render_router_history(rows)

    assert history.omitted is True
    assert 0 < len(history.text) <= ROUTER_HISTORY_TEXT_LIMIT
    _assert_router_render_shape(history.text)
    assert "newest-29-" in history.text
    assert "image_reference" not in history.text
    assert history.thread_count >= 1


def test_router_render_image_reference_toggle() -> None:
    rows = _router_rows()
    with_images, _ = render_previous_thread_context(
        rows, limit=ROUTER_HISTORY_TEXT_LIMIT
    )
    without_images, _ = render_previous_thread_context(
        rows, limit=ROUTER_HISTORY_TEXT_LIMIT, include_image_references=False
    )

    for text in (with_images, without_images):
        assert len(text) <= ROUTER_HISTORY_TEXT_LIMIT
        _assert_router_render_shape(text)
        assert "newest-29-" in text
    assert "image_reference" in with_images
    assert "image_reference" not in without_images


def test_router_render_is_empty_when_single_entry_exceeds_budget() -> None:
    rows = _history_rows(
        [
            {
                "user_id": "user",
                "lineage_id": "lineage",
                "sequence": 0,
                "kind": "user",
                "payload": {
                    "role": "user",
                    "content": "x" * (ROUTER_HISTORY_TEXT_LIMIT + 1),
                },
            }
        ]
    )

    history = render_router_history(rows)

    assert history.text == ""
    assert history.thread_count == 0
    assert history.omitted is True


@async_test
async def test_prepare_populates_full_and_routing_text_from_same_rows(
    monkeypatch,
) -> None:
    rows = _router_rows()

    async def fake_rows(*_args, **_kwargs):
        return rows

    monkeypatch.setattr(thread_memory_module, "_prepare_rows", fake_rows)

    memory = await prepare_previous_thread_memory_async(
        identity=IDENTITY,
        conversation_key=CONVERSATION_KEY,
        current_thread_id="thread",
        user_prompt="hello",
    )

    expected_text, _ = render_previous_thread_context(rows)
    expected_routing = render_router_history(rows)
    assert memory.outcome == "loaded"
    assert memory.text == expected_text
    assert memory.routing_text == expected_routing.text
    assert memory.routing_text and memory.routing_text != memory.text
    assert memory.routing_thread_count == expected_routing.thread_count
    assert memory.routing_omitted is True
    assert "image_reference" in memory.text
    assert "image_reference" not in memory.routing_text


def _forbid_prepare(monkeypatch) -> None:
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("resume loader must never call prepare_thread_memory")

    monkeypatch.setattr(thread_memory_module, "_prepare_rows", forbidden)


@async_test
async def test_resume_loader_maps_rows_like_prepare(monkeypatch) -> None:
    rows = _router_rows()

    async def fake_rows(*_args, **_kwargs):
        return rows

    monkeypatch.setattr(thread_memory_module, "_prepare_rows", fake_rows)
    prepared = await prepare_previous_thread_memory_async(
        identity=IDENTITY,
        conversation_key=CONVERSATION_KEY,
        current_thread_id="thread",
        user_prompt="hello",
    )

    resume_args: list[tuple] = []

    async def resumed_rows(identity, thread_id):
        resume_args.append((identity, thread_id))
        return rows

    _forbid_prepare(monkeypatch)
    monkeypatch.setattr(thread_memory_module, "_resumed_rows", resumed_rows)
    resumed = await load_resumed_thread_memory_async(
        identity=IDENTITY, thread_id="thread"
    )

    assert resume_args == [(IDENTITY, "thread")]
    assert resumed.outcome == prepared.outcome == "loaded"
    assert resumed.text == prepared.text
    assert resumed.routing_text == prepared.routing_text
    assert resumed.routing_thread_count == prepared.routing_thread_count
    assert resumed.routing_omitted == prepared.routing_omitted
    assert resumed.image_references == prepared.image_references
    assert resumed.previous_threads == prepared.previous_threads
    assert resumed.canonical_user_id == prepared.canonical_user_id == "user"
    assert resumed.lineage_id == prepared.lineage_id == "lineage"
    assert resumed.row_count == prepared.row_count


@async_test
async def test_resume_loader_without_rows_is_not_registered(monkeypatch) -> None:
    async def no_rows(*_args, **_kwargs):
        return []

    _forbid_prepare(monkeypatch)
    monkeypatch.setattr(thread_memory_module, "_resumed_rows", no_rows)

    memory = await load_resumed_thread_memory_async(
        identity=IDENTITY, thread_id="thread"
    )

    assert memory.outcome == "not_registered"
    assert memory.text == "" and memory.routing_text == ""


@async_test
async def test_resume_loader_without_history_is_no_predecessor(monkeypatch) -> None:
    async def thread_only(*_args, **_kwargs):
        return [
            {
                "user_id": "user",
                "lineage_id": "lineage",
                "source_thread_id": None,
                "history_rank": None,
                "sequence": None,
                "kind": None,
                "payload": None,
            }
        ]

    _forbid_prepare(monkeypatch)
    monkeypatch.setattr(thread_memory_module, "_resumed_rows", thread_only)

    memory = await load_resumed_thread_memory_async(
        identity=IDENTITY, thread_id="thread"
    )

    assert memory.outcome == "no_predecessor"
    assert memory.canonical_user_id == "user"
    assert memory.text == "" and memory.routing_text == ""


@async_test
async def test_resume_loader_timeout_fails_open(monkeypatch) -> None:
    cancelled = asyncio.Event()

    async def slow_rows(*_args, **_kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    _forbid_prepare(monkeypatch)
    monkeypatch.setattr(thread_memory_module, "_resumed_rows", slow_rows)
    monkeypatch.setattr(
        thread_memory_module, "PREVIOUS_THREAD_DB_TIMEOUT_SECONDS", 0.01
    )

    memory = await load_resumed_thread_memory_async(
        identity=IDENTITY, thread_id="thread"
    )

    assert memory.outcome == "db_timeout"
    assert cancelled.is_set()


@async_test
async def test_resume_loader_database_error_fails_open(monkeypatch) -> None:
    import psycopg

    async def broken_rows(*_args, **_kwargs):
        raise psycopg.errors.CheckViolation("secret database detail must not escape")

    _forbid_prepare(monkeypatch)
    monkeypatch.setattr(thread_memory_module, "_resumed_rows", broken_rows)

    memory = await load_resumed_thread_memory_async(
        identity=IDENTITY, thread_id="thread"
    )

    assert memory.outcome == "db_error"
    assert memory.error_type == "CheckViolation"
    assert memory.sqlstate == "23514"
    assert "secret database detail" not in repr(memory)


@async_test
async def test_resume_loader_inactive_user_is_not_registered(monkeypatch) -> None:
    import psycopg

    async def no_active_user(*_args, **_kwargs):
        raise psycopg.errors.NoDataFound("no active user found for telegram_user_id=1")

    _forbid_prepare(monkeypatch)
    monkeypatch.setattr(thread_memory_module, "_resumed_rows", no_active_user)

    memory = await load_resumed_thread_memory_async(
        identity=IDENTITY, thread_id="thread"
    )

    # Not a memory outage: must not trigger the "history unavailable" notice.
    assert memory.outcome == "not_registered"
    assert memory.sqlstate == "P0002"
    assert memory.text == "" and memory.routing_text == ""


@async_test
async def test_resume_loader_malformed_rows_are_memory_error(monkeypatch) -> None:
    async def malformed(*_args, **_kwargs):
        return _history_rows(
            [
                {
                    "user_id": "user",
                    "lineage_id": "lineage",
                    "sequence": 0,
                    "kind": "user",
                    "payload": {"role": "user", "content": "old"},
                }
            ],
            "previous",
            2,  # non-contiguous rank
        )

    _forbid_prepare(monkeypatch)
    monkeypatch.setattr(thread_memory_module, "_resumed_rows", malformed)

    memory = await load_resumed_thread_memory_async(
        identity=IDENTITY, thread_id="thread"
    )

    assert memory.outcome == "memory_error"


@async_test
async def test_resume_loader_query_is_read_only_and_owner_scoped(monkeypatch) -> None:
    class Cursor:
        description = [
            SimpleNamespace(name=name)
            for name in (
                "user_id", "lineage_id", "source_thread_id", "history_rank",
                "sequence", "kind", "payload",
            )
        ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, query, params):
            self.query = query
            self.params = params

        async def fetchall(self):
            return []

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

    monkeypatch.setattr(thread_memory_module, "get_async_pool", lambda: Pool())

    memory = await load_resumed_thread_memory_async(
        identity=IDENTITY, thread_id="resumed-thread"
    )

    assert memory.outcome == "not_registered"
    assert "prepare_thread_memory" not in cursor.query
    assert "fetch_previous_thread_memory" in cursor.query
    assert "resolve_user_id" in cursor.query
    assert "conversation_key IS NOT NULL" in cursor.query
    assert cursor.params == (
        PREVIOUS_THREAD_COUNT, "resumed-thread", IDENTITY.telegram_id
    )


@async_test
async def test_resume_injected_history_is_not_persisted() -> None:
    """Stage 4.4: history injected on a resume turn never reaches returned
    messages, and so never reaches the checkpoint or the memory snapshot."""

    from agents.agent_api.app.graph.nodes.orchestrator import create_agent_node
    from agents.agent_api.app.graph.prompts.context import build_initial_messages
    from agents.agent_api.app.graph.run_deps import RunDeps
    from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec

    history = (
        _HISTORY_PREAMBLE
        + "\n\n[Previous thread 1 — most recent]\n"
        + '{"content":"RESUME_HISTORY_MARKER","role":"user"}'
    )

    class RecordingClient:
        seen: list = []

        def create_message(self, messages, tools, **_kwargs):
            RecordingClient.seen = list(messages)
            return {"role": "assistant", "content": "Moved to Friday."}

    client = RecordingClient()
    registry = ToolRegistry().register(
        [
            ToolSpec(
                name="ask_user",
                openai_schema={"type": "function", "function": {"name": "ask_user"}},
            )
        ]
    )
    deps = RunDeps(
        agent_client=client,
        registry=registry,
        previous_thread_context=history,
        previous_thread_routing_context=history,
        previous_thread_outcome="loaded",
    )
    node = create_agent_node(client, registry, max_agent_turns=30)
    state = {
        "messages": build_initial_messages("move my dentist appointment"),
        "user_prompt": "move my dentist appointment",
        "turn_count": 1,
        "clarification_history": [{"question": "Which day?", "reply": "friday"}],
        "active_domains": [],
    }

    result = await node(state, {"configurable": {"deps": deps}})

    assert any(m.get("content") == history for m in RecordingClient.seen)
    returned = result["messages"]
    assert not any("RESUME_HISTORY_MARKER" in str(m.get("content")) for m in returned)
    canonical = canonical_memory_messages(returned)
    assert not any("RESUME_HISTORY_MARKER" in json.dumps(m) for m in canonical)
    snapshot_rows, _images = build_memory_snapshot(
        returned, (), None, canonical_user_id="user", thread_id="resumed-thread"
    )
    assert not any("RESUME_HISTORY_MARKER" in json.dumps(p) for *_r, p in snapshot_rows)
    assert any(m.get("content") == "Moved to Friday." for m in returned)


@async_test
async def test_prepare_builds_references_without_storage_io(monkeypatch) -> None:
    async def rows_with_image(*_args, **_kwargs):
        return [
            {
                "user_id": "user",
                "lineage_id": "lineage",
                "source_thread_id": "previous",
                "history_rank": 1,
                "sequence": 0,
                "kind": "user",
                "payload": {"role": "user", "content": "look at this"},
            },
            {
                "user_id": "user",
                "lineage_id": "lineage",
                "source_thread_id": "previous",
                "history_rank": 1,
                "sequence": 1,
                "kind": "image",
                "payload": {
                    "uploaded": True,
                    "user_message_sequence": 0,
                    "object_path": "user/previous/abc.jpg",
                    "mime_type": "image/jpeg",
                    "bytes": 4,
                    "sha256": "abc",
                    "detail": "auto",
                },
            },
        ]

    def no_storage():
        raise AssertionError("prepare must not touch storage; references are metadata only")

    monkeypatch.setattr(
        "agents.agent_api.app.thread_memory._prepare_rows", rows_with_image
    )
    monkeypatch.setattr(
        "agents.agent_api.app.thread_memory.get_thread_memory_storage_client",
        no_storage,
    )

    memory = await prepare_previous_thread_memory_async(
        identity=IDENTITY,
        conversation_key=CONVERSATION_KEY,
        current_thread_id="thread",
        user_prompt="hello",
    )

    assert memory.outcome == "loaded"
    assert memory.images == ()
    assert len(memory.image_references) == 1
    assert memory.image_references[0]["sha256"] == "abc"


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
    assert not any("thread_memory_heads" in query for query, _ in statements)

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
    assert PREVIOUS_THREAD_COUNT == 2
    assert PREVIOUS_THREAD_TEXT_LIMIT == 40_000


@async_test
async def test_prepare_reports_redacted_database_error(monkeypatch) -> None:
    import psycopg

    async def broken_prepare(*_args, **_kwargs):
        raise psycopg.errors.CheckViolation("secret database detail must not escape")

    monkeypatch.setattr(thread_memory_module, "_prepare_rows", broken_prepare)
    memory = await prepare_previous_thread_memory_async(
        identity=IDENTITY, conversation_key=CONVERSATION_KEY,
        current_thread_id="thread", user_prompt="whens this trip?",
    )
    assert memory.outcome == "db_error"
    assert memory.error_type == "CheckViolation"
    assert memory.sqlstate == "23514"
    assert "secret database detail" not in repr(memory)
    with pytest.raises(psycopg.errors.CheckViolation):
        await prepare_previous_thread_memory_async(
            identity=IDENTITY, conversation_key=CONVERSATION_KEY,
            current_thread_id="thread", user_prompt="reset", reset_memory=True,
        )
