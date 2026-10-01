"""Authenticated control endpoints for accepted Jarvis runs."""

import asyncio
from typing import Optional

from fastapi import APIRouter, Header
from agents.agent_api.app.async_offload import bounded_to_thread
from agents.agent_api.app.api import request_idempotency
from agents.agent_api.app.api.active_runs import get_active_run_registry
from agents.agent_api.app.api.schemas import (
    AgentResponse,
    CancelRequest,
    CancelResponse,
    RunStatusRequest,
    RunStatusResponse,
)
from agents.agent_api.app.errors import require_api_key
from agents.agent_api.app.graph.canonicalize import build_request_idempotency_key

router = APIRouter()


@router.post("/runs/cancel", response_model=CancelResponse)
async def cancel_run(
    request: CancelRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> CancelResponse:
    require_api_key(x_jarvis_agent_key)
    outcome = get_active_run_registry().cancel(request.user_id, request.request_id)
    return CancelResponse(outcome=outcome.value, request_id=request.request_id)


async def _read_terminal(
    request: RunStatusRequest,
    logical_route: str,
) -> tuple[bool, Optional[AgentResponse]]:
    key = build_request_idempotency_key(
        logical_route,
        request.source,
        request.user_id,
        request.request_id,
    )
    try:
        payload = await bounded_to_thread(
            request_idempotency.DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR.store.get,
            key,
        )
        return True, AgentResponse(**payload) if payload is not None else None
    except Exception:
        return False, None


@router.post("/runs/status", response_model=RunStatusResponse)
async def run_status(
    request: RunStatusRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> RunStatusResponse:
    require_api_key(x_jarvis_agent_key)
    registry = get_active_run_registry()
    active = registry.get(request.user_id, request.request_id)
    if active is not None and not active.task.done():
        return RunStatusResponse(state="running", request_id=request.request_id)
    if active is not None:
        registry.finish(active)

    routes = (request.logical_route,) if request.logical_route else ("invoke", "resume")
    reads = await asyncio.gather(
        *(_read_terminal(request, route) for route in routes)
    )
    if not all(succeeded for succeeded, _response in reads):
        return RunStatusResponse(state="unknown", request_id=request.request_id)
    responses = [response for _succeeded, response in reads]
    matches = [
        (route, response)
        for route, response in zip(routes, responses)
        if response is not None
    ]
    if not matches:
        return RunStatusResponse(state="unknown", request_id=request.request_id)
    if len(matches) == 2 and matches[0][1] != matches[1][1]:
        return RunStatusResponse(state="unknown", request_id=request.request_id)

    logical_route, response = matches[0]
    return RunStatusResponse(
        state="completed",
        request_id=request.request_id,
        logical_route=logical_route,
        response=response,
    )


__all__ = ["cancel_run", "run_status", "router"]
