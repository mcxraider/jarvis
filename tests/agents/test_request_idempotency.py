"""Stage 3 tests for request-level API idempotency."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agents.api import app
from agents.agent_api.app.api import request_idempotency
from agents.agent_api.app.api.request_idempotency import (
    RequestIdempotencyCoordinator,
)
from agents.agent_api.app.graph.canonicalize import build_request_idempotency_key
from agents.agent_api.app.idempotency.store import (
    ClaimResult,
    ClaimState,
    MemoryIdempotencyStore,
)


def completed_state(thread_id="thread-1", response="Done."):
    return {
        "thread_id": thread_id,
        "interrupted": False,
        "final_response": response,
        "tool_results": [{"tool_name": "add_todoist_task"}],
        "error": "",
    }


def interrupted_state(thread_id="thread-hitl"):
    return {
        "thread_id": thread_id,
        "interrupted": True,
        "interrupt_payload": {
            "type": "clarification",
            "question": "Which task?",
            "thread_id": thread_id,
        },
        "tool_results": [],
        "error": "",
    }


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


@pytest.fixture
def coordinator(monkeypatch):
    instance = RequestIdempotencyCoordinator(
        MemoryIdempotencyStore(),
        ttl_seconds=60,
        lease_seconds=5,
        wait_seconds=0.1,
        poll_interval_seconds=0.001,
        heartbeat_interval_seconds=0.01,
    )
    monkeypatch.setattr(
        request_idempotency,
        "DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR",
        instance,
    )
    return instance


@pytest.fixture
def client():
    return TestClient(app)


class TestRequestCache:
    def test_repeated_invoke_runs_graph_once(self, client, coordinator):
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value=completed_state(),
        ) as run:
            first = client.post(
                "/invoke",
                json={"message": "add milk", "user_id": "jerry", "request_id": "req-1"},
            )
            second = client.post(
                "/invoke",
                json={"message": "add milk", "user_id": "jerry", "request_id": "req-1"},
            )

        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        run.assert_called_once()

    def test_repeated_resume_runs_graph_once(self, client, coordinator):
        with patch(
            "agents.agent_api.app.api.routes.resume.run_jarvis",
            return_value=completed_state("thread-hitl", "Resumed."),
        ) as run:
            payload = {
                "thread_id": "thread-hitl",
                "message": "approve",
                "user_id": "jerry",
                "request_id": "resume-1",
            }
            first = client.post("/resume", json=payload)
            second = client.post("/resume", json=payload)

        assert first.json() == second.json()
        run.assert_called_once()

    def test_invoke_and_resume_namespaces_are_independent(self, client, coordinator):
        with (
            patch(
                "agents.agent_api.app.api.routes.invoke.run_jarvis",
                return_value=completed_state("thread-1", "Invoked."),
            ) as invoke_run,
            patch(
                "agents.agent_api.app.api.routes.resume.run_jarvis",
                return_value=completed_state("thread-1", "Resumed."),
            ) as resume_run,
        ):
            invoke_response = client.post(
                "/invoke",
                json={"message": "start", "user_id": "jerry", "request_id": "same"},
            )
            resume_response = client.post(
                "/resume",
                json={
                    "thread_id": "thread-1",
                    "message": "continue",
                    "user_id": "jerry",
                    "request_id": "same",
                },
            )

        assert invoke_response.json()["response"] == "Invoked."
        assert resume_response.json()["response"] == "Resumed."
        invoke_run.assert_called_once()
        resume_run.assert_called_once()

    def test_interrupted_response_is_cached(self, client, coordinator):
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value=interrupted_state(),
        ) as run:
            payload = {
                "message": "update a task",
                "user_id": "jerry",
                "request_id": "interrupt-1",
            }
            first = client.post("/invoke", json=payload)
            second = client.post("/invoke", json=payload)

        assert first.json()["status"] == "interrupted"
        assert second.json()["interrupt"] == first.json()["interrupt"]
        run.assert_called_once()

    def test_failed_response_is_not_cached(self, client, coordinator):
        failed = {
            **completed_state(),
            "final_response": "Could not finish.",
            "error": "upstream failed",
        }
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value=failed,
        ) as run:
            payload = {
                "message": "add milk",
                "user_id": "jerry",
                "request_id": "failed-1",
            }
            first = client.post("/invoke", json=payload)
            second = client.post("/invoke", json=payload)

        assert first.json()["status"] == second.json()["status"] == "failed"
        assert run.call_count == 2

    def test_exception_is_not_cached(self, client, coordinator):
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            side_effect=RuntimeError("boom"),
        ) as run:
            payload = {
                "message": "add milk",
                "user_id": "jerry",
                "request_id": "exception-1",
            }
            client.post("/invoke", json=payload)
            client.post("/invoke", json=payload)

        assert run.call_count == 2

    def test_missing_request_id_executes_every_time(self, client, coordinator):
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value=completed_state(),
        ) as run:
            payload = {"message": "add milk", "user_id": "jerry"}
            client.post("/invoke", json=payload)
            client.post("/invoke", json=payload)

        assert run.call_count == 2

    def test_user_and_source_scope_request_ids(self, client, coordinator):
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value=completed_state(),
        ) as run:
            client.post(
                "/invoke",
                json={"message": "one", "user_id": "jerry", "request_id": "shared"},
            )
            client.post(
                "/invoke",
                json={"message": "two", "user_id": "friend", "request_id": "shared"},
            )
            client.post(
                "/invoke",
                json={
                    "message": "three",
                    "user_id": "jerry",
                    "source": "telegram",
                    "request_id": "shared",
                },
            )

        assert run.call_count == 3

    def test_replayed_fresh_invoke_uses_cache_without_consuming_quota(
        self,
        client,
        coordinator,
    ):
        with patch(
            "agents.agent_api.app.middleware.rate_limit.consume_new_thread_quota",
        ) as quota, patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value=completed_state(),
        ) as run:
            payload = {
                "message": "add milk",
                "user_id": "jerry",
                "telegram_user_id": 42,
                "request_id": "quota-replay",
            }
            first = client.post("/invoke", json=payload)
            second = client.post("/invoke", json=payload)

        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        run.assert_called_once()
        quota.assert_called_once()

    def test_quota_is_consumed_after_idempotency_miss_before_graph_run(
        self,
        client,
        coordinator,
    ):
        events = []

        def consume(_identity):
            events.append("quota")

        def run(**_kwargs):
            events.append("run")
            return completed_state()

        with patch(
            "agents.agent_api.app.middleware.rate_limit.consume_new_thread_quota",
            side_effect=consume,
        ), patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            side_effect=run,
        ):
            response = client.post(
                "/invoke",
                json={
                    "message": "add milk",
                    "user_id": "jerry",
                    "telegram_user_id": 42,
                    "request_id": "quota-order",
                },
            )

        assert response.status_code == 200
        assert events == ["quota", "run"]

    def test_resume_never_consumes_new_thread_quota(self, client, coordinator):
        with patch(
            "agents.agent_api.app.middleware.rate_limit.consume_new_thread_quota",
            side_effect=AssertionError("resume must not consume quota"),
        ), patch(
            "agents.agent_api.app.api.routes.resume.run_jarvis",
            return_value=completed_state("thread-hitl", "Resumed."),
        ):
            response = client.post(
                "/resume",
                json={
                    "thread_id": "thread-hitl",
                    "message": "approve",
                    "user_id": "jerry",
                    "telegram_user_id": 42,
                    "request_id": "resume-no-quota",
                },
            )

        assert response.status_code == 200
        assert response.json()["response"] == "Resumed."


class TestStreamingInteroperability:
    def test_stream_completion_is_reused_by_nonstream(self, client, coordinator):
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value=completed_state(),
        ) as run:
            payload = {
                "message": "add milk",
                "user_id": "jerry",
                "request_id": "stream-first",
            }
            streamed = client.post("/invoke/stream", json=payload)
            normal = client.post("/invoke", json=payload)

        events = [json.loads(line) for line in streamed.text.strip().splitlines()]
        assert events[-1]["type"] == "final"
        assert normal.json()["response"] == events[-1]["response"]["response"]
        run.assert_called_once()

    def test_nonstream_completion_is_returned_as_single_final_event(
        self,
        client,
        coordinator,
    ):
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value=completed_state(),
        ) as run:
            payload = {
                "message": "add milk",
                "user_id": "jerry",
                "request_id": "normal-first",
            }
            client.post("/invoke", json=payload)
            streamed = client.post("/invoke/stream", json=payload)

        events = [json.loads(line) for line in streamed.text.strip().splitlines()]
        assert len(events) == 1
        assert events[0]["type"] == "final"
        assert events[0]["response"]["status"] == "completed"
        run.assert_called_once()

    def test_stream_failure_is_abandoned(self, client, coordinator):
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            side_effect=[RuntimeError("stream failed"), completed_state()],
        ) as run:
            payload = {
                "message": "add milk",
                "user_id": "jerry",
                "request_id": "stream-failure",
            }
            streamed = client.post("/invoke/stream", json=payload)
            normal = client.post("/invoke", json=payload)

        final = json.loads(streamed.text.strip().splitlines()[-1])
        assert final["response"]["status"] == "failed"
        assert normal.json()["status"] == "completed"
        assert run.call_count == 2

    def test_stream_completes_cache_before_emitting_final(self, client, coordinator):
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value=completed_state(),
        ):
            payload = {
                "message": "add milk",
                "user_id": "jerry",
                "request_id": "ordering",
            }
            streamed = client.post("/invoke/stream", json=payload)

        assert json.loads(streamed.text.strip())["type"] == "final"
        key = build_request_idempotency_key("invoke", "api", "jerry", "ordering")
        assert coordinator.store.get(key)["status"] == "completed"

    def test_resume_stream_completion_is_reused_by_nonstream(
        self,
        client,
        coordinator,
    ):
        with patch(
            "agents.agent_api.app.api.routes.resume.run_jarvis",
            return_value=completed_state("thread-hitl", "Approved."),
        ) as run:
            payload = {
                "thread_id": "thread-hitl",
                "message": "approve",
                "user_id": "jerry",
                "request_id": "resume-stream",
            }
            streamed = client.post("/resume/stream", json=payload)
            normal = client.post("/resume", json=payload)

        events = [json.loads(line) for line in streamed.text.strip().splitlines()]
        assert events[-1]["response"]["response"] == "Approved."
        assert normal.json()["response"] == "Approved."
        run.assert_called_once()


class TestConcurrencyAndFailureModes:
    def test_concurrent_duplicate_waits_for_completed_result(self, client, coordinator):
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        lock = threading.Lock()

        def run(**_kwargs):
            nonlocal calls
            with lock:
                calls += 1
            entered.set()
            release.wait(timeout=2)
            return completed_state()

        payload = {
            "message": "add milk",
            "user_id": "jerry",
            "request_id": "concurrent",
        }
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            side_effect=run,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(client.post, "/invoke", json=payload)
                assert entered.wait(timeout=1)
                second = pool.submit(client.post, "/invoke", json=payload)
                time.sleep(0.02)
                release.set()
                responses = [first.result(), second.result()]

        assert calls == 1
        assert all(response.status_code == 200 for response in responses)
        assert responses[0].json() == responses[1].json()

    def test_active_duplicate_times_out_with_409(self, client, coordinator):
        coordinator.wait_seconds = 0.01
        claim = coordinator.begin("invoke", "api", "jerry", "busy")
        try:
            with patch(
                "agents.agent_api.app.api.routes.invoke.run_jarvis",
                return_value=completed_state(),
            ) as run:
                response = client.post(
                    "/invoke",
                    json={
                        "message": "add milk",
                        "user_id": "jerry",
                        "request_id": "busy",
                    },
                )
        finally:
            coordinator.abandon(claim)

        assert response.status_code == 409
        assert response.headers["retry-after"] == "1"
        run.assert_not_called()

    def test_store_outage_fails_open(self, client, monkeypatch):
        coordinator = RequestIdempotencyCoordinator(
            ExplodingStore(),
            ttl_seconds=60,
            lease_seconds=5,
            wait_seconds=0.01,
            poll_interval_seconds=0.001,
        )
        monkeypatch.setattr(
            request_idempotency,
            "DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR",
            coordinator,
        )

        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value=completed_state(),
        ) as run:
            response = client.post(
                "/invoke",
                json={
                    "message": "add milk",
                    "user_id": "jerry",
                    "request_id": "outage",
                },
            )

        assert response.status_code == 200
        run.assert_called_once()

    def test_authentication_happens_before_claim(self, client, coordinator):
        with (
            patch(
                "agents.agent_api.app.middleware.request_gate.require_api_key",
                side_effect=HTTPException(status_code=401, detail="unauthorized"),
            ),
            patch.object(coordinator, "begin") as begin,
        ):
            response = client.post(
                "/invoke",
                json={
                    "message": "add milk",
                    "user_id": "jerry",
                    "request_id": "unauthorized",
                },
            )

        assert response.status_code == 401
        begin.assert_not_called()

    def test_invoke_bulk_does_not_claim(self, client, coordinator):
        with (
            patch(
                "agents.agent_api.app.api.routes.invoke.run_jarvis",
                return_value=completed_state(),
            ),
            patch.object(coordinator, "begin") as begin,
        ):
            response = client.post(
                "/invoke-bulk",
                json={"messages": ["one", "two"], "user_id": "jerry"},
            )

        assert response.status_code == 200
        begin.assert_not_called()


class TestHeartbeat:
    def test_acquired_request_renews_until_completion(self):
        store = MagicMock()
        store.claim.return_value = ClaimResult(
            ClaimState.ACQUIRED,
            owner_token="owner",
        )
        store.renew.return_value = True
        store.complete.return_value = True
        coordinator = RequestIdempotencyCoordinator(
            store,
            ttl_seconds=60,
            lease_seconds=5,
            wait_seconds=0.1,
            poll_interval_seconds=0.001,
            heartbeat_interval_seconds=0.005,
        )

        claim = coordinator.begin("invoke", "api", "jerry", "heartbeat")
        time.sleep(0.02)
        assert coordinator.complete(claim, {"status": "completed"})

        assert store.renew.call_count >= 1
        store.complete.assert_called_once()

    def test_expired_lease_can_be_taken_over(self):
        class Clock:
            value = 1000.0

            def __call__(self):
                return self.value

        clock = Clock()
        store = MemoryIdempotencyStore(clock)
        coordinator = RequestIdempotencyCoordinator(
            store,
            ttl_seconds=60,
            lease_seconds=5,
            wait_seconds=0.01,
            poll_interval_seconds=0.001,
            heartbeat_interval_seconds=100,
        )
        first = coordinator.begin("invoke", "api", "jerry", "takeover")
        clock.value += 6
        second = coordinator.begin("invoke", "api", "jerry", "takeover")
        try:
            assert first.owner_token != second.owner_token
            assert second.state is ClaimState.ACQUIRED
        finally:
            coordinator.abandon(first)
            coordinator.abandon(second)
