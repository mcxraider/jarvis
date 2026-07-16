"""Focused coverage for the process-wide run admission gate."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agents.api import app
from agents.agent_api.app.api import admission
from agents.agent_api.app.api.admission import RunAdmission
from agents.agent_api.app.api.request_idempotency import RequestClaim
from agents.agent_api.app.api.routes.invoke import (
    drain_stream_workers,
    stream_agent_run,
)
from agents.agent_api.app.api.schemas import AgentResponse
from agents.agent_api.app.config import load_settings
from agents.agent_api.app.idempotency import ClaimState


COMPLETED_RUN = {
    "thread_id": "thread-1",
    "interrupted": False,
    "final_response": "Done.",
    "tool_results": [],
    "error": "",
}


def _collect_stream(response) -> list[str]:
    async def collect() -> list[str]:
        return [chunk async for chunk in response.body_iterator]

    return asyncio.run(collect())


def test_admission_acquires_to_limit_and_release_is_idempotent() -> None:
    gate = RunAdmission(2)
    first = gate.try_acquire()
    second = gate.try_acquire()

    assert first is not None
    assert second is not None
    assert gate.try_acquire() is None

    first.release()
    first.release()
    replacement = gate.try_acquire()
    assert replacement is not None
    assert gate.try_acquire() is None

    second.release()
    replacement.release()


def test_sync_and_async_acquisitions_share_one_pool() -> None:
    gate = RunAdmission(1)
    sync_slot = gate.try_acquire()

    assert sync_slot is not None
    assert asyncio.run(gate.try_acquire_async()) is None

    sync_slot.release()
    async_slot = asyncio.run(gate.try_acquire_async())
    assert async_slot is not None
    assert gate.try_acquire() is None
    async_slot.release()


def test_config_defaults_max_concurrent_runs_to_eight(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_MAX_CONCURRENT_RUNS", raising=False)

    assert load_settings().max_concurrent_runs == 8


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/invoke", {"message": "hi", "user_id": "jerry"}),
        ("/invoke/stream", {"message": "hi", "user_id": "jerry"}),
        (
            "/resume",
            {"message": "yes", "user_id": "jerry", "thread_id": "thread-1"},
        ),
        (
            "/resume/stream",
            {"message": "yes", "user_id": "jerry", "thread_id": "thread-1"},
        ),
        ("/invoke-bulk", {"messages": ["hi"], "user_id": "jerry"}),
    ],
)
def test_run_routes_return_retryable_429_at_capacity(path, payload) -> None:
    gate = RunAdmission(1)
    held_slot = gate.try_acquire()

    with patch.object(admission, "_admission", gate), patch(
        "agents.agent_api.app.middleware.thread_ownership.validate_thread_ownership"
    ):
        response = TestClient(app).post(path, json=payload)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
    assert response.json()["detail"] == (
        "Jarvis is at capacity. Please retry in a few seconds."
    )
    held_slot.release()


def test_authentication_happens_before_capacity_is_exposed() -> None:
    gate = RunAdmission(1)
    held_slot = gate.try_acquire()

    with patch.object(admission, "_admission", gate), patch(
        "agents.agent_api.app.errors.settings",
        SimpleNamespace(api_key="secret"),
    ):
        response = TestClient(app).post(
            "/invoke",
            json={"message": "hi", "user_id": "jerry"},
        )

    assert response.status_code == 401
    held_slot.release()


def test_health_remains_available_at_run_capacity() -> None:
    gate = RunAdmission(1)
    held_slot = gate.try_acquire()

    with patch.object(admission, "_admission", gate):
        response = TestClient(app).get("/health")

    assert response.status_code == 200
    held_slot.release()


def test_invoke_releases_slot_after_success_and_failure() -> None:
    gate = RunAdmission(1)
    client = TestClient(app)

    with patch.object(admission, "_admission", gate), patch(
        "agents.agent_api.app.api.routes.invoke.run_jarvis",
        side_effect=[RuntimeError("boom"), COMPLETED_RUN],
    ):
        failed = client.post("/invoke", json={"message": "first", "user_id": "jerry"})
        succeeded = client.post(
            "/invoke",
            json={"message": "second", "user_id": "jerry"},
        )

    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert succeeded.status_code == 200
    assert succeeded.json()["status"] == "completed"


def test_gate_failure_releases_slot_before_run_starts() -> None:
    gate = RunAdmission(1)

    with patch.object(admission, "_admission", gate), patch(
        "agents.agent_api.app.middleware.rate_limit.consume_new_thread_quota",
        side_effect=HTTPException(status_code=429, detail="Daily quota reached."),
    ):
        response = TestClient(app).post(
            "/invoke",
            json={"message": "first", "user_id": "jerry"},
        )

    assert response.status_code == 429
    returned_slot = gate.try_acquire()
    assert returned_slot is not None
    returned_slot.release()


def test_gate_failure_releases_slot_when_claim_abandonment_fails() -> None:
    gate = RunAdmission(1)

    with patch.object(admission, "_admission", gate), patch(
        "agents.agent_api.app.middleware.rate_limit.consume_new_thread_quota",
        side_effect=HTTPException(status_code=429, detail="Daily quota reached."),
    ), patch(
        "agents.agent_api.app.middleware.request_gate.idempotency.abandon_idempotent_request",
        side_effect=RuntimeError("abandon failed"),
    ):
        response = TestClient(app).post(
            "/invoke",
            json={"message": "first", "user_id": "jerry"},
        )

    assert response.status_code == 429
    assert response.json()["detail"] == "Daily quota reached."
    returned_slot = gate.try_acquire()
    assert returned_slot is not None
    returned_slot.release()


@pytest.mark.parametrize("path", ["/invoke", "/invoke/stream"])
def test_cached_replay_bypasses_full_capacity_without_running_graph(path) -> None:
    gate = RunAdmission(1)
    held_slot = gate.try_acquire()
    cached = AgentResponse(
        status="completed",
        thread_id="cached-thread",
        response="Cached.",
    )

    with patch.object(admission, "_admission", gate), patch(
        "agents.agent_api.app.middleware.request_gate.idempotency.begin_idempotent_request",
        return_value=(RequestClaim(ClaimState.COMPLETED), cached),
    ), patch("agents.agent_api.app.api.routes.invoke.run_jarvis") as run:
        response = TestClient(app).post(
            path,
            json={"message": "repeat", "user_id": "jerry"},
        )

    assert response.status_code == 200
    run.assert_not_called()
    assert gate.try_acquire() is None
    held_slot.release()
    returned_slot = gate.try_acquire()
    assert returned_slot is not None
    returned_slot.release()


def test_capacity_rejection_abandons_new_claim() -> None:
    gate = RunAdmission(1)
    held_slot = gate.try_acquire()
    claim = RequestClaim(ClaimState.ACQUIRED)

    with patch.object(admission, "_admission", gate), patch(
        "agents.agent_api.app.middleware.request_gate.idempotency.begin_idempotent_request",
        return_value=(claim, None),
    ), patch(
        "agents.agent_api.app.middleware.request_gate.idempotency.abandon_idempotent_request"
    ) as abandon:
        response = TestClient(app).post(
            "/invoke",
            json={"message": "new", "user_id": "jerry"},
        )

    assert response.status_code == 429
    abandon.assert_called_once_with(claim)
    held_slot.release()


def test_capacity_rejection_survives_claim_abandonment_failure() -> None:
    gate = RunAdmission(1)
    held_slot = gate.try_acquire()
    claim = RequestClaim(ClaimState.ACQUIRED)

    with patch.object(admission, "_admission", gate), patch(
        "agents.agent_api.app.middleware.request_gate.idempotency.begin_idempotent_request",
        return_value=(claim, None),
    ), patch(
        "agents.agent_api.app.middleware.request_gate.idempotency.abandon_idempotent_request",
        side_effect=RuntimeError("abandon failed"),
    ):
        response = TestClient(app).post(
            "/invoke",
            json={"message": "new", "user_id": "jerry"},
        )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
    held_slot.release()


def test_duplicate_waiter_does_not_hold_run_capacity() -> None:
    gate = RunAdmission(1)
    waiting = threading.Event()
    resume_claim = threading.Event()

    def wait_for_claim(*_args, **_kwargs):
        waiting.set()
        assert resume_claim.wait(timeout=2)
        return RequestClaim(ClaimState.UNAVAILABLE), None

    with patch.object(admission, "_admission", gate), patch(
        "agents.agent_api.app.middleware.request_gate.idempotency.begin_idempotent_request",
        side_effect=wait_for_claim,
    ), patch(
        "agents.agent_api.app.api.routes.invoke.run_jarvis",
        return_value=COMPLETED_RUN,
    ):
        with ThreadPoolExecutor(max_workers=1) as executor:
            request = executor.submit(
                TestClient(app).post,
                "/invoke",
                json={"message": "duplicate", "user_id": "jerry"},
            )
            assert waiting.wait(timeout=1)
            probe_slot = gate.try_acquire()
            assert probe_slot is not None
            probe_slot.release()
            resume_claim.set()
            response = request.result(timeout=2)

    assert response.status_code == 200


def test_stream_worker_owns_slot_until_run_finishes() -> None:
    gate = RunAdmission(1)
    run_slot = gate.try_acquire()
    entered = threading.Event()
    finish = threading.Event()

    def run(_tracer):
        entered.set()
        assert finish.wait(timeout=2)
        return COMPLETED_RUN

    response = stream_agent_run(run, run_slot=run_slot)
    assert entered.wait(timeout=1)
    assert gate.try_acquire() is None

    finish.set()
    _collect_stream(response)

    returned_slot = gate.try_acquire()
    assert returned_slot is not None
    returned_slot.release()


def test_stream_worker_releases_slot_after_failure() -> None:
    gate = RunAdmission(1)
    run_slot = gate.try_acquire()

    def run(_tracer):
        raise RuntimeError("boom")

    response = stream_agent_run(run, run_slot=run_slot)
    _collect_stream(response)

    returned_slot = gate.try_acquire()
    assert returned_slot is not None
    returned_slot.release()


def test_disconnected_stream_worker_is_drained_before_shutdown() -> None:
    gate = RunAdmission(1)
    run_slot = gate.try_acquire()
    entered = threading.Event()
    finish = threading.Event()

    def run(_tracer):
        entered.set()
        assert finish.wait(timeout=2)
        return COMPLETED_RUN

    response = stream_agent_run(run, run_slot=run_slot)
    assert entered.wait(timeout=1)

    async def disconnect() -> None:
        await response.body_iterator.aclose()

    asyncio.run(disconnect())
    assert drain_stream_workers(timeout=0.01) is False
    assert gate.try_acquire() is None

    finish.set()
    assert drain_stream_workers(timeout=1.0) is True
    returned_slot = gate.try_acquire()
    assert returned_slot is not None
    returned_slot.release()


@pytest.mark.parametrize("failure_point", ["thread_constructor", "thread_start"])
def test_stream_start_failure_abandons_claim_and_releases_slot(failure_point) -> None:
    gate = RunAdmission(1)
    run_slot = gate.try_acquire()
    claim = RequestClaim(ClaimState.ACQUIRED)

    if failure_point == "thread_constructor":
        thread_patch = patch(
            "agents.agent_api.app.api.routes.invoke.threading.Thread",
            side_effect=RuntimeError("thread constructor failed"),
        )
    else:
        thread = MagicMock()
        thread.start.side_effect = RuntimeError("thread start failed")
        thread_patch = patch(
            "agents.agent_api.app.api.routes.invoke.threading.Thread",
            return_value=thread,
        )

    with thread_patch, patch(
        "agents.agent_api.app.api.routes.invoke.idempotency.abandon_idempotent_request"
    ) as abandon:
        with pytest.raises(RuntimeError, match="thread .* failed"):
            stream_agent_run(lambda _tracer: COMPLETED_RUN, claim, run_slot)

    abandon.assert_called_once_with(claim)
    assert drain_stream_workers(timeout=0.0) is True
    returned_slot = gate.try_acquire()
    assert returned_slot is not None
    returned_slot.release()


def test_stream_queue_failure_releases_slot_even_if_abandonment_fails() -> None:
    gate = RunAdmission(1)
    run_slot = gate.try_acquire()
    claim = RequestClaim(ClaimState.ACQUIRED)

    with patch(
        "agents.agent_api.app.api.routes.invoke.queue.Queue",
        side_effect=RuntimeError("queue construction failed"),
    ), patch(
        "agents.agent_api.app.api.routes.invoke.idempotency.abandon_idempotent_request",
        side_effect=RuntimeError("abandon failed"),
    ) as abandon:
        with pytest.raises(RuntimeError, match="queue construction failed"):
            stream_agent_run(lambda _tracer: COMPLETED_RUN, claim, run_slot)

    abandon.assert_called_once_with(claim)
    assert drain_stream_workers(timeout=0.0) is True
    returned_slot = gate.try_acquire()
    assert returned_slot is not None
    returned_slot.release()


def test_bulk_request_uses_one_slot_for_the_entire_batch() -> None:
    gate = RunAdmission(1)
    availability_during_calls = []

    def run(**_kwargs):
        availability_during_calls.append(gate.try_acquire())
        return COMPLETED_RUN

    with patch.object(admission, "_admission", gate), patch(
        "agents.agent_api.app.api.routes.invoke.run_jarvis",
        side_effect=run,
    ):
        response = TestClient(app).post(
            "/invoke-bulk",
            json={"messages": ["first", "second"], "user_id": "jerry"},
        )

    assert response.status_code == 200
    assert availability_during_calls == [None, None]
    returned_slot = gate.try_acquire()
    assert returned_slot is not None
    returned_slot.release()
