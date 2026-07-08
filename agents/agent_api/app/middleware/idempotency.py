"""Route-facing request idempotency wrappers."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException

from agents.agent_api.app.api import request_idempotency
from agents.agent_api.app.api.request_idempotency import RequestClaim
from agents.agent_api.app.api.schemas import AgentResponse
from agents.agent_api.app.idempotency import ClaimState
from agents.agent_api.app.user_context.identity import TelegramIdentity


def request_source(source: Optional[str], identity: Optional[TelegramIdentity]) -> str:
    if source:
        return source
    return "telegram" if identity is not None else "api"


def _response_payload(response: AgentResponse) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        return response.model_dump(exclude_none=True)
    return response.dict(exclude_none=True)


def begin_idempotent_request(
    logical_route: str,
    request: Any,
) -> tuple[RequestClaim, Optional[AgentResponse]]:
    identity = request.resolved_telegram_identity()
    claim = request_idempotency.DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR.begin(
        logical_route,
        request_source(request.source, identity),
        request.user_id,
        request.request_id,
    )
    if claim.state is ClaimState.COMPLETED:
        return claim, AgentResponse(**(claim.result or {}))
    if claim.state is ClaimState.IN_PROGRESS:
        raise HTTPException(
            status_code=409,
            detail="An identical request is still in progress.",
            headers={"Retry-After": "1"},
        )
    return claim, None


def finish_idempotent_request(
    claim: RequestClaim,
    response: AgentResponse,
) -> None:
    coordinator = request_idempotency.DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR
    if response.status in {"completed", "interrupted"}:
        coordinator.complete(claim, _response_payload(response))
    else:
        coordinator.abandon(claim)


def abandon_idempotent_request(claim: RequestClaim) -> None:
    request_idempotency.DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR.abandon(claim)
