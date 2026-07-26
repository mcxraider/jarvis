"""Ordered request gate for API routes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from agents.agent_api.app.async_offload import bounded_to_thread
from agents.agent_api.app.api.admission import (
    RunSlot,
    capacity_exceeded,
    try_acquire_run_slot,
    try_acquire_run_slot_async,
)
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
    run_slot: Optional[RunSlot]


def _abandon_claim(claim: RequestClaim) -> None:
    """Best-effort claim cleanup that preserves the gate's original failure."""

    try:
        idempotency.abandon_idempotent_request(claim)
    except Exception:
        pass


async def abandon_claim_async(claim: RequestClaim) -> None:
    """Best-effort asynchronous claim cleanup for route failure paths."""

    task = asyncio.create_task(
        bounded_to_thread(idempotency.abandon_idempotent_request, claim)
    )
    cancellation_requested = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancellation_requested = True
        except BaseException:
            break
    if cancellation_requested:
        raise asyncio.CancelledError


async def _await_claim_task(
    task: asyncio.Task[tuple[RequestClaim, Optional[AgentResponse]]],
) -> tuple[tuple[RequestClaim, Optional[AgentResponse]], bool]:
    """Wait for claim acquisition through repeated caller cancellation."""

    cancellation_requested = False
    while True:
        try:
            return await asyncio.shield(task), cancellation_requested
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancellation_requested = True


async def _begin_claim_async(
    logical_route: str,
    request: Any,
) -> tuple[RequestClaim, Optional[AgentResponse]]:
    """Acquire a claim without leaking it if the caller is cancelled mid-I/O."""

    task = asyncio.create_task(
        bounded_to_thread(
            idempotency.begin_idempotent_request,
            logical_route,
            request,
        )
    )
    result, cancellation_requested = await _await_claim_task(task)
    if cancellation_requested:
        claim, _cached_response = result
        try:
            await abandon_claim_async(claim)
        except BaseException:
            pass
        raise asyncio.CancelledError
    return result


def apply_request_gate(
    logical_route: str,
    request: Any,
    api_key: Optional[str],
    *,
    charges_new_thread_quota: bool = False,
    require_thread_ownership: bool = False,
    admit_run: bool = False,
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
            run_slot=None,
        )

    try:
        run_slot = try_acquire_run_slot() if admit_run else None
    except BaseException:
        _abandon_claim(claim)
        raise
    if admit_run and run_slot is None:
        _abandon_claim(claim)
        raise capacity_exceeded()

    quota = None
    if charges_new_thread_quota and thread_id is None:
        try:
            quota = rate_limit.consume_new_thread_quota(identity)
        except BaseException:
            try:
                _abandon_claim(claim)
            finally:
                if run_slot is not None:
                    run_slot.release()
            raise

    return RequestContext(
        identity=identity,
        request_source=source,
        claim=claim,
        cached_response=None,
        quota=quota,
        run_slot=run_slot,
    )


async def apply_request_gate_async(
    logical_route: str,
    request: Any,
    api_key: Optional[str],
    *,
    charges_new_thread_quota: bool = False,
    require_thread_ownership: bool = False,
    admit_run: bool = False,
) -> RequestContext:
    """Async request gate preserving the synchronous gate's security ordering."""

    require_api_key(api_key)
    identity = request.resolved_telegram_identity()
    source = idempotency.request_source(request.source, identity)

    thread_id = getattr(request, "thread_id", None)
    if require_thread_ownership and thread_id:
        await bounded_to_thread(
            thread_ownership.validate_thread_ownership,
            thread_id,
            identity,
        )

    claim, cached_response = await _begin_claim_async(logical_route, request)
    if cached_response is not None:
        return RequestContext(
            identity=identity,
            request_source=source,
            claim=claim,
            cached_response=cached_response,
            quota=None,
            run_slot=None,
        )

    try:
        run_slot = await try_acquire_run_slot_async() if admit_run else None
    except BaseException:
        await abandon_claim_async(claim)
        raise
    if admit_run and run_slot is None:
        await abandon_claim_async(claim)
        raise capacity_exceeded()

    quota = None
    if charges_new_thread_quota and thread_id is None:
        try:
            quota = await bounded_to_thread(
                rate_limit.consume_new_thread_quota,
                identity,
            )
        except BaseException:
            try:
                await abandon_claim_async(claim)
            finally:
                if run_slot is not None:
                    run_slot.release()
            raise

    return RequestContext(
        identity=identity,
        request_source=source,
        claim=claim,
        cached_response=None,
        quota=quota,
        run_slot=run_slot,
    )


__all__ = [
    "RequestContext",
    "abandon_claim_async",
    "apply_request_gate",
    "apply_request_gate_async",
]
