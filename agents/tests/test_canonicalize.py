"""Tests for the canonicalization and hashing module."""

import json

import pytest

from agents.agent_api.app.graph.canonicalize import (
    build_held_call,
    canonicalize,
    verify_hash,
)


class TestCanonicalize:
    def test_sorted_keys(self):
        result = canonicalize({"z": 1, "a": 2, "m": 3})
        assert result == b'{"a":2,"m":3,"z":1}'

    def test_no_whitespace(self):
        result = canonicalize({"key": "value"})
        assert b" " not in result
        assert b"\n" not in result

    def test_deterministic(self):
        args = {"task_id": "123", "content": "hello world"}
        assert canonicalize(args) == canonicalize(args)

    def test_order_independent(self):
        a = canonicalize({"b": 2, "a": 1})
        b = canonicalize({"a": 1, "b": 2})
        assert a == b

    def test_nested_objects_sorted(self):
        result = canonicalize({"outer": {"z": 1, "a": 2}})
        parsed = json.loads(result)
        assert parsed == {"outer": {"z": 1, "a": 2}}

    def test_returns_bytes(self):
        assert isinstance(canonicalize({}), bytes)


class TestBuildHeldCall:
    def _make_tool_call(self, name="delete_todoist_task", args=None):
        return {
            "id": "call_abc123",
            "function": {
                "name": name,
                "arguments": json.dumps(args or {"task_id": "999"}),
            },
        }

    def test_structure(self):
        tc = self._make_tool_call()
        held = build_held_call(tc, "thread_1", 5)
        assert "id" in held
        assert held["tool_name"] == "delete_todoist_task"
        assert held["args"] == {"task_id": "999"}
        assert held["origin_tool_call_id"] == "call_abc123"
        assert "hash" in held
        assert "idempotency_key" in held

    def test_hash_is_sha256_hex(self):
        tc = self._make_tool_call()
        held = build_held_call(tc, "thread_1", 0)
        assert len(held["hash"]) == 64
        assert all(c in "0123456789abcdef" for c in held["hash"])

    def test_idempotency_key_varies_with_thread(self):
        tc = self._make_tool_call()
        h1 = build_held_call(tc, "thread_A", 1)
        h2 = build_held_call(tc, "thread_B", 1)
        assert h1["idempotency_key"] != h2["idempotency_key"]

    def test_idempotency_key_varies_with_turn(self):
        tc = self._make_tool_call()
        h1 = build_held_call(tc, "thread_1", 1)
        h2 = build_held_call(tc, "thread_1", 2)
        assert h1["idempotency_key"] != h2["idempotency_key"]

    def test_args_normalized(self):
        tc = self._make_tool_call(args={"z": 1, "a": 2})
        held = build_held_call(tc, "t", 0)
        assert held["args"] == {"a": 2, "z": 1}

    def test_unique_ids(self):
        tc = self._make_tool_call()
        h1 = build_held_call(tc, "t", 0)
        h2 = build_held_call(tc, "t", 0)
        assert h1["id"] != h2["id"]


class TestVerifyHash:
    def test_valid_hash(self):
        tc = {"id": "c1", "function": {"name": "delete_todoist_task", "arguments": '{"task_id":"1"}'}}
        held = build_held_call(tc, "t", 0)
        assert verify_hash(held) is True

    def test_tampered_args(self):
        tc = {"id": "c1", "function": {"name": "delete_todoist_task", "arguments": '{"task_id":"1"}'}}
        held = build_held_call(tc, "t", 0)
        held["args"]["task_id"] = "999"
        assert verify_hash(held) is False

    def test_tampered_with_extra_key(self):
        tc = {"id": "c1", "function": {"name": "delete_todoist_task", "arguments": '{"task_id":"1"}'}}
        held = build_held_call(tc, "t", 0)
        held["args"]["extra"] = "injected"
        assert verify_hash(held) is False
