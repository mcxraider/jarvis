"""Durable status lookup for delivery-ambiguous agent runs."""

import asyncio
import importlib
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from agents.agent_api.app.api import request_idempotency
from agents.agent_api.app.api.active_runs import get_active_run_registry
from agents.agent_api.app.api.admission import RunAdmission
from agents.agent_api.app.api.request_idempotency import RequestIdempotencyCoordinator
from agents.agent_api.app.api.routes.cancel import run_status
from agents.agent_api.app.api.schemas import AgentResponse, InvokeRequest, RunStatusRequest
from agents.agent_api.app.graph.run_control import RunControl
from agents.agent_api.app.idempotency.store import MemoryIdempotencyStore

invoke_routes = importlib.import_module("agents.agent_api.app.api.routes.invoke")


def setup_function() -> None:
    registry = get_active_run_registry()
    if registry.count == 0:
        registry.reset()


def teardown_function() -> None:
    registry = get_active_run_registry()
    if registry.count == 0:
        registry.reset()


def _request(logical_route=None) -> RunStatusRequest:
    return RunStatusRequest(
        user_id="user",
        request_id="request",
        source="telegram",
        logical_route=logical_route,
    )


def _coordinator(store=None, ttl_seconds=60) -> RequestIdempotencyCoordinator:
    return RequestIdempotencyCoordinator(
        store or MemoryIdempotencyStore(),
        ttl_seconds=ttl_seconds,
        lease_seconds=1,
        wait_seconds=0.01,
        poll_interval_seconds=0.001,
    )


def _persist(
    coordinator: RequestIdempotencyCoordinator,
    route: str,
    response: AgentResponse,
) -> None:
    claim = coordinator.begin(route, "telegram", "user", "request")
    assert coordinator.complete(claim, response.model_dump(exclude_none=True))


def test_status_requires_the_configured_api_key() -> None:
    from agents.agent_api.app import errors

    with patch.object(errors, "settings", SimpleNamespace(api_key="secret")):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(run_status(_request(), x_jarvis_agent_key=None))

    assert raised.value.status_code == 401


def test_active_request_reports_running() -> None:
    async def scenario() -> None:
        task = asyncio.create_task(asyncio.Event().wait())
        run = get_active_run_registry().register(
            user_id="user",
            request_id="request",
            thread_id=None,
            deadline=time.monotonic() + 60,
            task=task,
            control=RunControl(),
        )
        try:
            assert (await run_status(_request())).state == "running"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            get_active_run_registry().finish(run)

    asyncio.run(scenario())


@pytest.mark.parametrize("route", ["invoke", "resume"])
def test_persisted_terminal_result_reports_completed(route: str) -> None:
    coordinator = _coordinator()
    response = AgentResponse(
        status="failed",
        thread_id="thread",
        response="This Jarvis run was cancelled.",
        error="Run cancelled.",
        error_details={"kind": "cancelled"},
    )
    _persist(coordinator, route, response)

    with patch.object(
        request_idempotency,
        "DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR",
        coordinator,
    ):
        status = asyncio.run(run_status(_request(route)))

    assert status.state == "completed"
    assert status.logical_route == route
    assert status.response == response


def test_missing_expired_and_storage_failed_records_report_unknown() -> None:
    with patch.object(
        request_idempotency,
        "DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR",
        _coordinator(),
    ):
        assert asyncio.run(run_status(_request("invoke"))).state == "unknown"

    now = [0.0]
    store = MemoryIdempotencyStore(clock=lambda: now[0])
    coordinator = _coordinator(store, ttl_seconds=1)
    _persist(
        coordinator,
        "invoke",
        AgentResponse(status="completed", thread_id="thread", response="Done."),
    )
    now[0] = 2.0

    with patch.object(
        request_idempotency,
        "DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR",
        coordinator,
    ):
        assert asyncio.run(run_status(_request("invoke"))).state == "unknown"

    coordinator.store = SimpleNamespace(get=lambda _key: (_ for _ in ()).throw(RuntimeError()))
    with patch.object(
        request_idempotency,
        "DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR",
        coordinator,
    ):
        assert asyncio.run(run_status(_request("invoke"))).state == "unknown"


def test_different_dual_route_results_fail_closed() -> None:
    coordinator = _coordinator()
    _persist(
        coordinator,
        "invoke",
        AgentResponse(status="completed", thread_id="thread", response="Invoked."),
    )
    _persist(
        coordinator,
        "resume",
        AgentResponse(status="completed", thread_id="thread", response="Resumed."),
    )

    with patch.object(
        request_idempotency,
        "DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR",
        coordinator,
    ):
        assert asyncio.run(run_status(_request())).state == "unknown"


def test_disconnected_stream_can_finish_and_be_reconciled() -> None:
    async def scenario() -> None:
        coordinator = _coordinator()
        claim = coordinator.begin("invoke", "telegram", "user", "request")
        slot = RunAdmission(1).try_acquire()
        assert slot is not None
        finish = asyncio.Event()

        async def run(**kwargs):
            kwargs["tracer"].progress(
                {"phase": "lookup", "action": "started", "intent": "read"}
            )
            await finish.wait()
            return {"thread_id": "thread", "final_response": "Done."}

        gate = SimpleNamespace(
            cached_response=None,
            claim=claim,
            run_slot=slot,
            request_source="telegram",
            identity=None,
        )
        request = InvokeRequest(
            message="disconnect",
            user_id="user",
            source="telegram",
            request_id="request",
        )
        with patch.object(
            request_idempotency,
            "DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR",
            coordinator,
        ), patch.object(
            invoke_routes,
            "apply_request_gate_async",
            return_value=gate,
        ), patch.object(invoke_routes, "run_jarvis", new=run):
            response = await invoke_routes.invoke_stream(
                request,
                SimpleNamespace(
                    app=SimpleNamespace(state=SimpleNamespace(async_checkpointer=object()))
                ),
                None,
            )
            iterator = response.body_iterator
            assert json.loads(await anext(iterator))["type"] == "progress"
            await iterator.aclose()
            finish.set()
            assert await invoke_routes.drain_stream_workers(timeout=1.0)

            status = await run_status(_request("invoke"))
            assert status.state == "completed"
            assert status.response is not None
            assert status.response.response == "Done."

    asyncio.run(scenario())
