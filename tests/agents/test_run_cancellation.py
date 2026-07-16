"""Route-level cancellation, deadline, and stream ownership regressions."""

import asyncio
import importlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from agents.agent_api.app.api.active_runs import get_active_run_registry
from agents.agent_api.app.api.admission import RunAdmission
from agents.agent_api.app.api.routes.cancel import cancel_run
from agents.agent_api.app.api.schemas import (
    BulkInvokeRequest,
    CancelRequest,
    InvokeRequest,
    ResumeRequest,
)

invoke_routes = importlib.import_module("agents.agent_api.app.api.routes.invoke")
resume_routes = importlib.import_module("agents.agent_api.app.api.routes.resume")


def test_cancel_endpoint_requires_the_configured_api_key() -> None:
    from agents.agent_api.app import errors

    with patch.object(errors, "settings", SimpleNamespace(api_key="secret")):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(
                cancel_run(
                    CancelRequest(user_id="user", request_id="request"),
                    x_jarvis_agent_key=None,
                )
            )

    assert getattr(raised.value, "status_code", None) == 401


def setup_function() -> None:
    registry = get_active_run_registry()
    if registry.count == 0:
        registry.reset()


def teardown_function() -> None:
    registry = get_active_run_registry()
    if registry.count == 0:
        registry.reset()


def _http_request() -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(async_checkpointer=object()))
    )


def _gate_context(slot) -> SimpleNamespace:
    return SimpleNamespace(
        cached_response=None,
        claim=None,
        run_slot=slot,
        request_source="api",
        identity=None,
    )


def test_nonstream_cancel_targets_actual_producer_and_returns_terminal_response() -> None:
    async def scenario() -> None:
        gate = RunAdmission(1)
        slot = gate.try_acquire()
        started = asyncio.Event()

        async def run(**_kwargs):
            started.set()
            await asyncio.Event().wait()

        request = InvokeRequest(
            message="wait",
            user_id="user",
            request_id="request-nonstream",
        )
        with patch.object(
            invoke_routes,
            "apply_request_gate_async",
            new=AsyncMock(return_value=_gate_context(slot)),
        ), patch.object(invoke_routes, "run_jarvis", new=run):
            route_task = asyncio.create_task(
                invoke_routes.invoke(request, _http_request(), None)
            )
            await started.wait()
            active = get_active_run_registry().get("user", "request-nonstream")
            assert active is not None
            assert active.task is not route_task

            cancellation = await cancel_run(
                CancelRequest(user_id="user", request_id="request-nonstream")
            )
            assert cancellation.outcome == "cancelled"

            response = await route_task
            assert response.status == "failed"
            assert response.error_details == {"kind": "cancelled"}

        await asyncio.sleep(0)
        assert get_active_run_registry().count == 0
        returned = gate.try_acquire()
        assert returned is not None
        returned.release()

    asyncio.run(scenario())


def test_cancel_before_route_registration_prevents_runner_dispatch() -> None:
    async def scenario() -> None:
        gate = RunAdmission(1)
        slot = gate.try_acquire()
        gate_entered = asyncio.Event()
        allow_gate = asyncio.Event()

        async def delayed_gate(*_args, **_kwargs):
            gate_entered.set()
            await allow_gate.wait()
            return _gate_context(slot)

        run = AsyncMock()
        request = InvokeRequest(
            message="do not start",
            user_id="user",
            request_id="request-before-register",
        )
        with patch.object(
            invoke_routes,
            "apply_request_gate_async",
            new=delayed_gate,
        ), patch.object(invoke_routes, "run_jarvis", new=run):
            route_task = asyncio.create_task(
                invoke_routes.invoke(request, _http_request(), None)
            )
            await gate_entered.wait()

            cancellation = await cancel_run(
                CancelRequest(
                    user_id="user",
                    request_id="request-before-register",
                )
            )
            assert cancellation.outcome == "not_found"

            allow_gate.set()
            response = await route_task

        run.assert_not_awaited()
        assert response.status == "failed"
        assert response.error_details == {"kind": "cancelled"}
        await asyncio.sleep(0)
        assert get_active_run_registry().count == 0

    asyncio.run(scenario())


def test_cancel_wins_when_completion_is_already_queued() -> None:
    async def scenario() -> None:
        gate = RunAdmission(1)
        slot = gate.try_acquire()
        outcomes = []

        async def run(**_kwargs):
            # This queues task.cancel(), but returns without another suspension.
            # Settlement must observe the earlier cancellation linearization.
            outcomes.append(
                get_active_run_registry().cancel(
                    "user",
                    "request-finish-race",
                )
            )
            return {
                "thread_id": "thread",
                "final_response": "must not escape",
            }

        request = InvokeRequest(
            message="finish race",
            user_id="user",
            request_id="request-finish-race",
        )
        with patch.object(
            invoke_routes,
            "apply_request_gate_async",
            new=AsyncMock(return_value=_gate_context(slot)),
        ), patch.object(invoke_routes, "run_jarvis", new=run):
            response = await invoke_routes.invoke(request, _http_request(), None)

        assert [outcome.value for outcome in outcomes] == ["cancelled"]
        assert response.status == "failed"
        assert response.error_details == {"kind": "cancelled"}
        assert response.response != "must not escape"
        await asyncio.sleep(0)
        assert get_active_run_registry().count == 0

    asyncio.run(scenario())


def test_connected_stream_receives_final_event_after_explicit_cancel() -> None:
    async def scenario() -> None:
        gate = RunAdmission(1)
        slot = gate.try_acquire()
        started = asyncio.Event()

        async def run(**_kwargs):
            started.set()
            await asyncio.Event().wait()

        request = InvokeRequest(
            message="stream",
            user_id="user",
            request_id="request-stream",
        )
        with patch.object(
            invoke_routes,
            "apply_request_gate_async",
            new=AsyncMock(return_value=_gate_context(slot)),
        ), patch.object(invoke_routes, "run_jarvis", new=run):
            response = await invoke_routes.invoke_stream(
                request,
                _http_request(),
                None,
            )
            await started.wait()
            cancellation = await cancel_run(
                CancelRequest(user_id="user", request_id="request-stream")
            )
            assert cancellation.outcome == "cancelled"

            events = [
                json.loads(chunk)
                async for chunk in response.body_iterator
            ]
            assert events[-1]["type"] == "final"
            assert events[-1]["response"]["error_details"] == {
                "kind": "cancelled"
            }

        assert await invoke_routes.drain_stream_workers(timeout=1.0) is True
        assert get_active_run_registry().count == 0
        returned = gate.try_acquire()
        assert returned is not None
        returned.release()

    asyncio.run(scenario())


def test_stream_disconnect_keeps_run_visible_for_explicit_cancel() -> None:
    async def scenario() -> None:
        gate = RunAdmission(1)
        slot = gate.try_acquire()
        started = asyncio.Event()

        async def run(**kwargs):
            kwargs["tracer"].progress(
                {"phase": "lookup", "action": "started", "intent": "read"}
            )
            started.set()
            await asyncio.Event().wait()

        request = InvokeRequest(
            message="disconnect",
            user_id="user",
            request_id="request-disconnect",
        )
        with patch.object(
            invoke_routes,
            "apply_request_gate_async",
            new=AsyncMock(return_value=_gate_context(slot)),
        ), patch.object(invoke_routes, "run_jarvis", new=run):
            response = await invoke_routes.invoke_stream(
                request,
                _http_request(),
                None,
            )
            await started.wait()
            iterator = response.body_iterator
            first = json.loads(await anext(iterator))
            assert first["type"] == "progress"
            await iterator.aclose()

            assert (
                get_active_run_registry().get("user", "request-disconnect")
                is not None
            )
            cancellation = await cancel_run(
                CancelRequest(user_id="user", request_id="request-disconnect")
            )
            assert cancellation.outcome == "cancelled"

        assert await invoke_routes.drain_stream_workers(timeout=1.0) is True
        assert get_active_run_registry().count == 0

    asyncio.run(scenario())


def test_deadline_is_enforced_and_returns_structured_terminal_response() -> None:
    async def scenario() -> None:
        gate = RunAdmission(1)
        slot = gate.try_acquire()
        started = asyncio.Event()

        async def run(**_kwargs):
            started.set()
            await asyncio.Event().wait()

        request = InvokeRequest(
            message="deadline",
            user_id="user",
            request_id="request-deadline",
        )
        configured = replace(invoke_routes.settings, run_deadline_seconds=0.01)
        with patch.object(
            invoke_routes,
            "apply_request_gate_async",
            new=AsyncMock(return_value=_gate_context(slot)),
        ), patch.object(invoke_routes, "run_jarvis", new=run), patch.object(
            invoke_routes,
            "settings",
            configured,
        ):
            response = await asyncio.wait_for(
                invoke_routes.invoke(request, _http_request(), None),
                timeout=1,
            )

        assert started.is_set()
        assert response.status == "failed"
        assert response.error_details == {"kind": "deadline"}
        assert get_active_run_registry().count == 0
        returned = gate.try_acquire()
        assert returned is not None
        returned.release()

    asyncio.run(scenario())


def test_cancel_during_mutation_retains_registry_and_slot_until_settlement() -> None:
    async def scenario() -> None:
        gate = RunAdmission(1)
        slot = gate.try_acquire()
        started = asyncio.Event()
        release = asyncio.Event()

        async def run(**kwargs):
            control = kwargs["run_control"]
            assert control.begin_mutation() is True
            started.set()
            await release.wait()
            if control.finish_mutation():
                raise asyncio.CancelledError
            raise AssertionError("cancellation should have been deferred")

        request = InvokeRequest(
            message="mutate",
            user_id="user",
            request_id="request-mutation",
        )
        with patch.object(
            invoke_routes,
            "apply_request_gate_async",
            new=AsyncMock(return_value=_gate_context(slot)),
        ), patch.object(invoke_routes, "run_jarvis", new=run):
            route_task = asyncio.create_task(
                invoke_routes.invoke(request, _http_request(), None)
            )
            await started.wait()
            cancellation = await cancel_run(
                CancelRequest(user_id="user", request_id="request-mutation")
            )
            assert cancellation.outcome == "mutation_in_flight"
            assert gate.try_acquire() is None
            assert get_active_run_registry().count == 1
            assert not route_task.done()

            repeated = await cancel_run(
                CancelRequest(user_id="user", request_id="request-mutation")
            )
            assert repeated.outcome == "mutation_in_flight"
            release.set()
            response = await route_task

        assert response.status == "failed"
        assert response.error_details == {"kind": "cancelled"}
        await asyncio.sleep(0)
        assert get_active_run_registry().count == 0
        returned = gate.try_acquire()
        assert returned is not None
        returned.release()

    asyncio.run(scenario())


def test_resume_registers_new_request_against_existing_thread() -> None:
    async def scenario() -> None:
        gate = RunAdmission(1)
        slot = gate.try_acquire()
        started = asyncio.Event()

        async def run(**kwargs):
            assert kwargs["thread_id"] == "thread-existing"
            assert kwargs["clarification_reply"] == "yes"
            started.set()
            await asyncio.Event().wait()

        request = ResumeRequest(
            thread_id="thread-existing",
            message="yes",
            user_id="user",
            request_id="request-resume",
        )
        with patch.object(
            resume_routes,
            "apply_request_gate_async",
            new=AsyncMock(return_value=_gate_context(slot)),
        ), patch.object(resume_routes, "run_jarvis", new=run):
            route_task = asyncio.create_task(
                resume_routes.resume(request, _http_request(), None)
            )
            await started.wait()
            active = get_active_run_registry().get("user", "request-resume")
            assert active is not None
            assert active.thread_id == "thread-existing"

            cancellation = await cancel_run(
                CancelRequest(user_id="user", request_id="request-resume")
            )
            assert cancellation.outcome == "cancelled"
            response = await route_task

        assert response.error_details == {"kind": "cancelled"}
        await asyncio.sleep(0)
        assert get_active_run_registry().count == 0

    asyncio.run(scenario())


def test_bulk_cancel_stops_later_messages_and_returns_terminal_results() -> None:
    async def scenario() -> None:
        gate = RunAdmission(1)
        first_started = asyncio.Event()
        calls: list[str] = []

        async def run(**kwargs):
            calls.append(kwargs["user_prompt"])
            first_started.set()
            await asyncio.Event().wait()

        request = BulkInvokeRequest(
            messages=["first", "second"],
            user_id="user",
            request_id="request-bulk",
        )
        with patch.object(invoke_routes, "try_acquire_run_slot_async", AsyncMock(
            side_effect=gate.try_acquire_async
        )), patch.object(invoke_routes, "run_jarvis", new=run), patch.object(
            invoke_routes.rate_limit,
            "consume_new_thread_quota",
        ):
            route_task = asyncio.create_task(
                invoke_routes.invoke_bulk(request, _http_request(), None)
            )
            await first_started.wait()
            assert get_active_run_registry().get("user", "request-bulk") is not None
            cancellation = await cancel_run(
                CancelRequest(user_id="user", request_id="request-bulk")
            )
            assert cancellation.outcome == "cancelled"
            response = await route_task

        assert calls == ["first"]
        assert len(response.results) == 2
        assert all(
            result.error_details == {"kind": "cancelled"}
            for result in response.results
        )
        await asyncio.sleep(0)
        assert get_active_run_registry().count == 0
        returned = gate.try_acquire()
        assert returned is not None
        returned.release()

    asyncio.run(scenario())


def test_bulk_cancel_wins_over_queued_normal_completion() -> None:
    async def scenario() -> None:
        gate = RunAdmission(1)

        async def run(**_kwargs):
            assert (
                get_active_run_registry().cancel("user", "request-bulk-race").value
                == "cancelled"
            )
            return {
                "thread_id": "thread",
                "final_response": "must not escape",
            }

        request = BulkInvokeRequest(
            messages=["only"],
            user_id="user",
            request_id="request-bulk-race",
        )
        with patch.object(
            invoke_routes,
            "try_acquire_run_slot_async",
            AsyncMock(side_effect=gate.try_acquire_async),
        ), patch.object(invoke_routes, "run_jarvis", new=run), patch.object(
            invoke_routes.rate_limit,
            "consume_new_thread_quota",
        ):
            response = await invoke_routes.invoke_bulk(
                request,
                _http_request(),
                None,
            )

        assert response.results[0].error_details == {"kind": "cancelled"}
        assert response.results[0].response != "must not escape"
        await asyncio.sleep(0)
        assert get_active_run_registry().count == 0

    asyncio.run(scenario())
