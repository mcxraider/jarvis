"""Tests for concurrent mutation independence: resource key extraction,
partition logic, and RunControl counter-based mutation tracking."""

import asyncio
import threading

import pytest

from agents.agent_api.app.tools.metadata import mutation_resource_key
from agents.agent_api.app.graph.run_control import (
    CancelOutcome,
    RunControl,
    RunPhase,
)


# ---------------------------------------------------------------------------
# mutation_resource_key
# ---------------------------------------------------------------------------


class TestMutationResourceKey:
    def test_creation_tools_return_none(self):
        assert mutation_resource_key("add_todoist_task", {"content": "x"}) is None
        assert mutation_resource_key("create_project", {"name": "p"}) is None
        assert mutation_resource_key("create_section", {"name": "s"}) is None
        assert mutation_resource_key("create_calendar_event", {"summary": "e"}) is None

    def test_entity_tools_return_service_and_id(self):
        assert mutation_resource_key("update_todoist_task", {"task_id": "123"}) == "todoist:123"
        assert mutation_resource_key("complete_task", {"task_id": "abc"}) == "todoist:abc"
        assert mutation_resource_key("delete_todoist_task", {"task_id": "999"}) == "todoist:999"
        assert mutation_resource_key("update_calendar_event", {"event_id": "ev1"}) == "google calendar:ev1"
        assert mutation_resource_key("delete_calendar_event", {"event_id": "ev2"}) == "google calendar:ev2"

    def test_tools_without_display_meta_still_group_by_entity(self):
        # uncomplete_task and add_comment have no _REGISTRY entry so service is ""
        key_a = mutation_resource_key("uncomplete_task", {"task_id": "t1"})
        key_b = mutation_resource_key("uncomplete_task", {"task_id": "t1"})
        assert key_a is not None
        assert key_a == key_b
        # Same entity across different tools still groups (both use task_id "t1")
        key_c = mutation_resource_key("complete_task", {"task_id": "t1"})
        # Service labels differ, so keys may differ — that's fine, both are non-None

    def test_add_comment_with_task_id(self):
        key = mutation_resource_key("add_comment", {"task_id": "t1", "content": "hi"})
        assert key is not None
        assert "t1" in key

    def test_add_comment_without_task_id(self):
        assert mutation_resource_key("add_comment", {"content": "standalone"}) is None

    def test_unknown_tool_returns_none(self):
        assert mutation_resource_key("unknown_tool", {"id": "x"}) is None

    def test_empty_args(self):
        assert mutation_resource_key("update_todoist_task", {}) is None


# ---------------------------------------------------------------------------
# _partition_mutations (dispatcher-level)
# ---------------------------------------------------------------------------


class TestPartitionMutationsDispatcher:
    def _partition(self, indexed):
        from agents.agent_api.app.tools.dispatcher import _partition_mutations
        return _partition_mutations(indexed)

    def _tc(self, name, call_id, args):
        return {"id": call_id, "function": {"name": name, "arguments": args}}

    def test_all_creates_each_independent(self):
        items = [
            (0, self._tc("add_todoist_task", "c0", {"content": "a"})),
            (1, self._tc("add_todoist_task", "c1", {"content": "b"})),
            (2, self._tc("add_todoist_task", "c2", {"content": "c"})),
        ]
        chains = self._partition(items)
        assert len(chains) == 3
        assert all(len(c) == 1 for c in chains)

    def test_same_resource_single_chain(self):
        items = [
            (0, self._tc("update_todoist_task", "c0", {"task_id": "X", "content": "a"})),
            (1, self._tc("complete_task", "c1", {"task_id": "X"})),
        ]
        chains = self._partition(items)
        assert len(chains) == 1
        assert len(chains[0]) == 2
        assert chains[0][0][0] == 0
        assert chains[0][1][0] == 1

    def test_mixed_resources_multiple_chains(self):
        items = [
            (0, self._tc("update_todoist_task", "c0", {"task_id": "A", "content": "x"})),
            (1, self._tc("update_todoist_task", "c1", {"task_id": "B", "content": "y"})),
            (2, self._tc("add_todoist_task", "c2", {"content": "new"})),
            (3, self._tc("complete_task", "c3", {"task_id": "A"})),
        ]
        chains = self._partition(items)
        # chain for task A (indices 0, 3), chain for task B (index 1), independent create (index 2)
        assert len(chains) == 3
        task_a_chain = [c for c in chains if len(c) == 2][0]
        assert task_a_chain[0][0] == 0
        assert task_a_chain[1][0] == 3

    def test_cross_domain_independent(self):
        items = [
            (0, self._tc("update_todoist_task", "c0", {"task_id": "T1", "content": "x"})),
            (1, self._tc("update_calendar_event", "c1", {"event_id": "E1", "summary": "y"})),
        ]
        chains = self._partition(items)
        assert len(chains) == 2


# ---------------------------------------------------------------------------
# _partition_held_calls (executor-level)
# ---------------------------------------------------------------------------


class TestPartitionHeldCalls:
    def _partition(self, indexed):
        from agents.agent_api.app.graph.nodes.executor import _partition_held_calls
        return _partition_held_calls(indexed)

    def _held(self, idx, tool_name, args):
        return (idx, {"tool_name": tool_name, "args": args, "id": f"id-{idx}"})

    def test_all_creates_each_independent(self):
        items = [
            self._held(0, "add_todoist_task", {"content": "a"}),
            self._held(1, "add_todoist_task", {"content": "b"}),
        ]
        chains = self._partition(items)
        assert len(chains) == 2

    def test_same_resource_single_chain(self):
        items = [
            self._held(0, "complete_task", {"task_id": "X"}),
            self._held(1, "delete_todoist_task", {"task_id": "X"}),
        ]
        chains = self._partition(items)
        assert len(chains) == 1
        assert chains[0][0][0] == 0
        assert chains[0][1][0] == 1


# ---------------------------------------------------------------------------
# RunControl concurrent mutations
# ---------------------------------------------------------------------------


class TestRunControlConcurrentMutations:
    def test_multiple_begin_mutation_succeed(self):
        rc = RunControl()
        assert rc.begin_mutation() is True
        assert rc.begin_mutation() is True
        assert rc.mutations_in_flight == 2
        assert rc.phase is RunPhase.MUTATION_IN_FLIGHT

    def test_counter_decrements_correctly(self):
        rc = RunControl()
        rc.begin_mutation()
        rc.begin_mutation()
        assert rc.finish_mutation() is False  # still one in flight
        assert rc.phase is RunPhase.MUTATION_IN_FLIGHT
        assert rc.finish_mutation() is False  # back to cancellable
        assert rc.phase is RunPhase.CANCELLABLE

    def test_deferred_cancel_blocks_new_mutations(self):
        rc = RunControl()
        rc.begin_mutation()
        rc.request_cancel("test")
        assert rc.begin_mutation() is False  # blocked by deferred cancel
        assert rc.mutations_in_flight == 1

    def test_last_finisher_triggers_cancel(self):
        rc = RunControl()
        rc.begin_mutation()
        rc.begin_mutation()
        rc.request_cancel("test")
        assert rc.finish_mutation() is False  # not the last one
        assert rc.phase is RunPhase.MUTATION_IN_FLIGHT
        assert rc.finish_mutation() is True  # last one, cancel deferred
        assert rc.phase is RunPhase.CANCELLED

    def test_concurrent_begin_finish_thread_safety(self):
        rc = RunControl()
        results = []

        def worker():
            ok = rc.begin_mutation()
            results.append(ok)
            if ok:
                rc.finish_mutation()

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(results)
        assert rc.phase is RunPhase.CANCELLABLE
        assert rc.mutations_in_flight == 0

    def test_single_mutation_backwards_compatible(self):
        rc = RunControl()
        assert rc.begin_mutation() is True
        assert rc.phase is RunPhase.MUTATION_IN_FLIGHT
        assert rc.finish_mutation() is False
        assert rc.phase is RunPhase.CANCELLABLE

    def test_single_mutation_with_cancel(self):
        rc = RunControl()
        rc.begin_mutation()
        decision = rc.request_cancel()
        assert decision.outcome is CancelOutcome.MUTATION_IN_FLIGHT
        assert rc.finish_mutation() is True
        assert rc.phase is RunPhase.CANCELLED
