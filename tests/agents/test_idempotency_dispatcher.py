"""Stage 2 tests for operation-level idempotency enforcement."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock

from agents.agent_api.app.graph.nodes.tools import create_tools_node
from agents.agent_api.app.idempotency.store import (
    ClaimResult,
    ClaimState,
    MemoryIdempotencyStore,
)
from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.dispatcher import (
    ToolDispatcher,
    tool_idempotency_context,
)
from agents.agent_api.app.tools.todoist.client import TodoistApiError
from agents.agent_api.app.tools.todoist.tools import TodoistToolDispatcher


def build_registry(mutating_handler=None, readonly_handler=None):
    registry = ToolRegistry()
    registry.register(
        [
            ToolSpec(
                name="mutate",
                openai_schema={"type": "function", "function": {"name": "mutate"}},
                handler=mutating_handler or (lambda arguments: {"args": arguments}),
                mutating=True,
            ),
            ToolSpec(
                name="read",
                openai_schema={"type": "function", "function": {"name": "read"}},
                handler=readonly_handler or (lambda arguments: {"args": arguments}),
                mutating=False,
            ),
        ]
    )
    return registry


def build_dispatcher(registry, store, **options):
    return ToolDispatcher(
        registry,
        allow_mutations=True,
        idempotency_store=store,
        idempotency_operation_ttl_seconds=60,
        idempotency_lease_seconds=5,
        idempotency_wait_seconds=options.get("wait", 0.25),
        idempotency_poll_interval_seconds=options.get("poll", 0.001),
    )


class ExplodingStore:
    def claim(self, *_args, **_kwargs):
        raise RuntimeError("database unavailable")

    def get(self, *_args, **_kwargs):
        raise RuntimeError("database unavailable")

    def complete(self, *_args, **_kwargs):
        raise RuntimeError("database unavailable")

    def renew(self, *_args, **_kwargs):
        raise RuntimeError("database unavailable")

    def abandon(self, *_args, **_kwargs):
        raise RuntimeError("database unavailable")

    def cleanup_expired(self):
        raise RuntimeError("database unavailable")


class SequenceStore:
    def __init__(self, claims):
        self.claims = iter(claims)
        self.completed = []

    def claim(self, *_args, **_kwargs):
        return next(self.claims)

    def complete(self, key, owner_token, result, ttl_seconds):
        self.completed.append((key, owner_token, result, ttl_seconds))
        return True

    def abandon(self, *_args, **_kwargs):
        return True

    def renew(self, *_args, **_kwargs):
        return True

    def get(self, *_args, **_kwargs):
        return None

    def cleanup_expired(self):
        return 0


class TestOperationLifecycle:
    def test_cache_miss_executes_and_hit_rebinds_tool_call_id(self):
        calls = []

        def mutate(arguments):
            calls.append(arguments)
            return {"id": "task-1"}

        dispatcher = build_dispatcher(
            build_registry(mutating_handler=mutate),
            MemoryIdempotencyStore(),
        )

        first = dispatcher.execute_tool("call-1", "mutate", {"content": "milk"}, "key")
        second = dispatcher.execute_tool("call-2", "mutate", {"content": "milk"}, "key")

        assert len(calls) == 1
        assert first["success"] is True
        assert second["content"] == {"id": "task-1"}
        assert second["tool_call_id"] == "call-2"
        assert second["tool_name"] == "mutate"

    def test_missing_key_preserves_existing_behavior(self):
        handler = MagicMock(return_value={"ok": True})
        store = MagicMock()
        dispatcher = build_dispatcher(
            build_registry(mutating_handler=handler),
            store,
        )

        dispatcher.execute_tool("call-1", "mutate", {})
        dispatcher.execute_tool("call-2", "mutate", {})

        assert handler.call_count == 2
        store.claim.assert_not_called()

    def test_readonly_tool_never_claims(self):
        store = MagicMock()
        dispatcher = build_dispatcher(build_registry(), store)

        result = dispatcher.execute_tool("call-1", "read", {}, "key")

        assert result["success"] is True
        store.claim.assert_not_called()

    def test_blocked_and_unsupported_tools_never_claim(self):
        store = MagicMock()
        dispatcher = ToolDispatcher(
            build_registry(),
            allow_mutations=False,
            idempotency_store=store,
        )

        blocked = dispatcher.execute_tool("call-1", "mutate", {}, "key")
        unsupported = dispatcher.execute_tool("call-2", "missing", {}, "key")

        assert blocked["mutation_blocked"] is True
        assert unsupported["success"] is False
        store.claim.assert_not_called()

    def test_handler_error_abandons_and_allows_retry(self):
        attempts = 0

        def mutate(_arguments):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("failed once")
            return {"ok": True}

        store = MemoryIdempotencyStore()
        dispatcher = build_dispatcher(build_registry(mutating_handler=mutate), store)

        first = dispatcher.execute_tool("call-1", "mutate", {}, "key")
        second = dispatcher.execute_tool("call-2", "mutate", {}, "key")

        assert first["success"] is False
        assert second["success"] is True
        assert attempts == 2

    def test_ambiguous_mutation_error_is_cached_without_reexecution(self):
        attempts = 0

        def mutate(_arguments):
            nonlocal attempts
            attempts += 1
            raise TodoistApiError(
                kind="transient",
                message="Check Todoist before trying again.",
                retryable=False,
                operation="todoist.request",
                method="POST",
                ambiguous_commit=True,
            )

        dispatcher = build_dispatcher(
            build_registry(mutating_handler=mutate),
            MemoryIdempotencyStore(),
        )

        first = dispatcher.execute_tool("call-1", "mutate", {}, "calendar-key")
        second = dispatcher.execute_tool("call-2", "mutate", {}, "calendar-key")

        assert attempts == 1
        assert first["success"] is second["success"] is False
        assert first["mutation_blocked"] is second["mutation_blocked"] is True
        assert first["classified_error"]["ambiguous_commit"] is True
        assert second["tool_call_id"] == "call-2"

    def test_store_exception_blocks_mutation(self):
        handler = MagicMock(return_value={"ok": True})
        dispatcher = build_dispatcher(
            build_registry(mutating_handler=handler),
            ExplodingStore(),
        )

        result = dispatcher.execute_tool("call-1", "mutate", {}, "key")

        assert result["success"] is False
        assert result["mutation_blocked"] is True
        assert "mutation safety" in result["error"]
        handler.assert_not_called()

    def test_store_exception_does_not_block_readonly_tool(self):
        handler = MagicMock(return_value={"ok": True})
        dispatcher = build_dispatcher(
            build_registry(readonly_handler=handler),
            ExplodingStore(),
        )

        result = dispatcher.execute_tool("call-1", "read", {}, "key")

        assert result["success"] is True
        handler.assert_called_once()

    def test_completion_failure_is_ambiguous_and_does_not_abandon_claim(self):
        store = MagicMock()
        store.claim.return_value = ClaimResult(
            ClaimState.ACQUIRED,
            owner_token="owner",
        )
        store.complete.side_effect = RuntimeError("write failed")
        store.abandon.side_effect = RuntimeError("write failed")
        dispatcher = build_dispatcher(build_registry(), store)

        result = dispatcher.execute_tool("call-1", "mutate", {}, "key")

        assert result["success"] is False
        assert result["content"] is None
        assert result["mutation_blocked"] is True
        assert "may have completed" in result["error"]
        assert "Do not retry automatically" in result["error"]
        assert store.complete.call_count == 2
        store.renew.assert_called_once_with("key", "owner", 60)
        store.abandon.assert_not_called()

    def test_partial_bulk_result_is_cached_as_success(self):
        handler = MagicMock(
            return_value={
                "created_count": 2,
                "requested_count": 3,
                "tasks": [{"id": "1"}, {"id": "2"}],
                "errors": [{"index": 2, "error": "rate limited"}],
            }
        )
        dispatcher = build_dispatcher(
            build_registry(mutating_handler=handler),
            MemoryIdempotencyStore(),
        )

        first = dispatcher.execute_tool("call-1", "mutate", {"count": 3}, "bulk-key")
        second = dispatcher.execute_tool("call-2", "mutate", {"count": 3}, "bulk-key")

        handler.assert_called_once()
        assert first["content"]["created_count"] == 2
        assert second["content"] == first["content"]


class TestOperationConcurrency:
    def test_concurrent_duplicate_executes_handler_once(self):
        calls = 0
        lock = threading.Lock()

        def mutate(_arguments):
            nonlocal calls
            with lock:
                calls += 1
            time.sleep(0.03)
            return {"id": "task-1"}

        dispatcher = build_dispatcher(
            build_registry(mutating_handler=mutate),
            MemoryIdempotencyStore(),
            wait=1.0,
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    dispatcher.execute_tool,
                    f"call-{index}",
                    "mutate",
                    {"content": "milk"},
                    "shared-key",
                )
                for index in range(2)
            ]
        results = [future.result() for future in futures]

        assert calls == 1
        assert all(result["success"] for result in results)
        assert {result["tool_call_id"] for result in results} == {"call-0", "call-1"}

    def test_active_claim_timeout_does_not_execute(self):
        store = MemoryIdempotencyStore()
        store.claim("key", "operation", 60, 5, tool_name="mutate")
        handler = MagicMock(return_value={"ok": True})
        dispatcher = build_dispatcher(
            build_registry(mutating_handler=handler),
            store,
            wait=0.01,
            poll=0.001,
        )

        result = dispatcher.execute_tool("call-1", "mutate", {}, "key")

        assert result["success"] is False
        assert "still in progress" in result["error"]
        handler.assert_not_called()

    def test_wait_can_take_over_expired_lease(self):
        store = SequenceStore(
            [
                ClaimResult(ClaimState.IN_PROGRESS),
                ClaimResult(ClaimState.ACQUIRED, owner_token="replacement"),
            ]
        )
        handler = MagicMock(return_value={"ok": True})
        dispatcher = build_dispatcher(
            build_registry(mutating_handler=handler),
            store,
        )

        result = dispatcher.execute_tool("call-1", "mutate", {}, "key")

        assert result["success"] is True
        handler.assert_called_once()
        assert store.completed[0][1] == "replacement"


class TestOperationContext:
    def test_context_derives_stable_key(self):
        handler = MagicMock(return_value={"ok": True})
        store = MemoryIdempotencyStore()
        dispatcher = build_dispatcher(
            build_registry(mutating_handler=handler),
            store,
        )

        with tool_idempotency_context("thread-1", 3):
            first = dispatcher.execute_tool("call-1", "mutate", {"content": "milk"})
        with tool_idempotency_context("thread-1", 3):
            second = dispatcher.execute_tool("call-2", "mutate", {"content": "milk"})

        assert first["success"] is True
        assert second["success"] is True
        assert second["tool_call_id"] == "call-2"
        handler.assert_called_once()

    def test_context_is_reset_after_exit(self):
        store = MagicMock()
        store.claim.return_value = ClaimResult(
            ClaimState.ACQUIRED,
            owner_token="owner",
        )
        store.complete.return_value = True
        dispatcher = build_dispatcher(build_registry(), store)

        with tool_idempotency_context("thread-1", 3):
            dispatcher.execute_tool("call-1", "mutate", {})
        dispatcher.execute_tool("call-2", "mutate", {})

        store.claim.assert_called_once()

    def test_explicit_key_takes_precedence_over_context(self):
        store = MagicMock()
        store.claim.return_value = ClaimResult(
            ClaimState.ACQUIRED,
            owner_token="owner",
        )
        store.complete.return_value = True
        dispatcher = build_dispatcher(build_registry(), store)

        with tool_idempotency_context("thread-1", 3):
            dispatcher.execute_tool(
                "call-1",
                "mutate",
                {"content": "milk"},
                idempotency_key="explicit",
            )

        assert store.claim.call_args.args[0] == "explicit"

    def test_toolnode_path_uses_thread_and_turn_context(self):
        todoist = MagicMock()
        todoist.add_todoist_task.return_value = {"id": "task-1"}
        todoist.async_add_todoist_task = AsyncMock(return_value={"id": "task-1"})
        dispatcher = TodoistToolDispatcher(
            todoist,
            allow_mutations=True,
            idempotency_store=MemoryIdempotencyStore(),
            idempotency_operation_ttl_seconds=60,
            idempotency_lease_seconds=5,
            idempotency_wait_seconds=0.25,
            idempotency_poll_interval_seconds=0.001,
        )
        node = create_tools_node(dispatcher)

        def state(call_id):
            return {
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "function": {
                                    "name": "add_todoist_task",
                                    "arguments": '{"content":"milk"}',
                                },
                            }
                        ],
                    }
                ],
                "thread_id": "thread-1",
                "turn_count": 3,
                "tool_results": [],
            }

        first = asyncio.run(node(state("call-1")))
        second = asyncio.run(node(state("call-2")))

        todoist.async_add_todoist_task.assert_awaited_once()
        todoist.add_todoist_task.assert_not_called()
        assert first["tool_results"][0]["success"] is True
        assert second["tool_results"][0]["tool_call_id"] == "call-2"
        # Verify a cached result exists in the store. Derive the key from
        # the actual tool signature rather than hardcoding all nullable fields,
        # so this test stays stable when parameters are added.
        store_records = dispatcher.idempotency_store._records
        assert len(store_records) == 1
        cached_key = next(iter(store_records))
        assert dispatcher.idempotency_store.get(cached_key) is not None

    def test_new_todoist_tools_follow_registry_mutation_metadata(self):
        todoist = MagicMock()
        todoist.uncomplete_task.return_value = {"success": True}
        todoist.add_comment.return_value = {"id": "comment-1"}
        todoist.get_comments.return_value = {"results": []}
        todoist.get_labels.return_value = {"results": []}
        store = MagicMock()
        store.claim.side_effect = [
            ClaimResult(ClaimState.ACQUIRED, owner_token="owner-1"),
            ClaimResult(ClaimState.ACQUIRED, owner_token="owner-2"),
        ]
        store.complete.return_value = True
        dispatcher = TodoistToolDispatcher(
            todoist,
            allow_mutations=True,
            idempotency_store=store,
        )

        dispatcher.execute_tool(
            "call-1",
            "uncomplete_task",
            {"task_id": "task-1"},
            "uncomplete-key",
        )
        dispatcher.execute_tool(
            "call-2",
            "add_comment",
            {"content": "hello", "task_id": "task-1"},
            "comment-key",
        )
        dispatcher.execute_tool("call-3", "get_comments", {"task_id": "task-1"})
        dispatcher.execute_tool("call-4", "get_labels", {})

        assert store.claim.call_count == 2
        assert store.complete.call_count == 2
        assert {call.kwargs["tool_name"] for call in store.claim.call_args_list} == {
            "uncomplete_task",
            "add_comment",
        }
