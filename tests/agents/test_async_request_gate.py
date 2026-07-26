"""Cancellation hardening for async request-claim acquisition and cleanup."""

import asyncio
import threading
from unittest.mock import patch

from agents.agent_api.app.api.request_idempotency import RequestClaim
from agents.agent_api.app.idempotency.store import ClaimState
from agents.agent_api.app.middleware import request_gate


def test_repeated_cancellation_waits_for_claim_then_abandons_it() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        claim = RequestClaim(ClaimState.ACQUIRED)

        def begin(_route, _request):
            started.set()
            assert release.wait(timeout=2)
            return claim, None

        with patch.object(
            request_gate.idempotency,
            "begin_idempotent_request",
            side_effect=begin,
        ), patch.object(
            request_gate.idempotency,
            "abandon_idempotent_request",
        ) as abandon:
            task = asyncio.create_task(
                request_gate._begin_claim_async("invoke", object())
            )
            while not started.is_set():
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("claim waiter cancellation should propagate")

        abandon.assert_called_once_with(claim)

    asyncio.run(scenario())


def test_repeated_cancellation_cannot_interrupt_claim_abandonment() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        claim = RequestClaim(ClaimState.ACQUIRED)

        def abandon(_claim):
            started.set()
            assert release.wait(timeout=2)

        with patch.object(
            request_gate.idempotency,
            "abandon_idempotent_request",
            side_effect=abandon,
        ) as cleanup:
            task = asyncio.create_task(request_gate.abandon_claim_async(claim))
            while not started.is_set():
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cleanup cancellation should propagate")

        cleanup.assert_called_once_with(claim)

    asyncio.run(scenario())
