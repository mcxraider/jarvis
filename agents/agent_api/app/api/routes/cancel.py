"""Authenticated endpoint for cancelling an accepted Jarvis run."""

from typing import Optional

from fastapi import APIRouter, Header

from agents.agent_api.app.api.active_runs import get_active_run_registry
from agents.agent_api.app.api.schemas import CancelRequest, CancelResponse
from agents.agent_api.app.errors import require_api_key

router = APIRouter()


@router.post("/runs/cancel", response_model=CancelResponse)
async def cancel_run(
    request: CancelRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> CancelResponse:
    require_api_key(x_jarvis_agent_key)
    outcome = get_active_run_registry().cancel(request.user_id, request.request_id)
    return CancelResponse(outcome=outcome.value, request_id=request.request_id)


__all__ = ["cancel_run", "router"]
