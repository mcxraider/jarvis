"""Ordered request gate for API routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from agents.agent_api.app.api.request_idempotency import RequestClaim
from agents.agent_api.app.api.schemas import AgentResponse
from agents.agent_api.app.errors import require_api_key
import agents.agent_api.app.middleware.idempotency as idempotency
import agents.agent_api.app.middleware.rate_limit as rate_limit
import agents.agent_api.app.middleware.thread_ownership as thread_ownership
from agents.agent_api.app.middleware.rate_limit import QuotaResult
from agents.agent_api.app.user_context.identity import TelegramIdentity


@dataclass(frozen=True)
class RequestContext:
    identity: Optional[TelegramIdentity]
    request_source: str
    claim: RequestClaim
    cached_response: Optional[AgentResponse]
    quota: Optional[QuotaResult]


def apply_request_gate(
    logical_route: str,
    request: Any,
    api_key: Optional[str],
    *,
    charges_new_thread_quota: bool = False,
    require_thread_ownership: bool = False,
) -> RequestContext:
    require_api_key(api_key)
    identity = request.resolved_telegram_identity()
    source = idempotency.request_source(request.source, identity)

    thread_id = getattr(request, "thread_id", None)
    if require_thread_ownership and thread_id:
        thread_ownership.validate_thread_ownership(thread_id, identity)

    claim, cached_response = idempotency.begin_idempotent_request(logical_route, request)
    if cached_response is not None:
        return RequestContext(
            identity=identity,
            request_source=source,
            claim=claim,
            cached_response=cached_response,
            quota=None,
        )

    quota = None
    if charges_new_thread_quota and thread_id is None:
        try:
            quota = rate_limit.consume_new_thread_quota(identity)
        except Exception:
            idempotency.abandon_idempotent_request(claim)
            raise

    return RequestContext(
        identity=identity,
        request_source=source,
        claim=claim,
        cached_response=None,
        quota=quota,
    )
