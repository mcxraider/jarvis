"""Request-level idempotency coordination for API routes."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from agents.agent_api.app.config import settings
from agents.agent_api.app.graph.canonicalize import build_request_idempotency_key
from agents.agent_api.app.idempotency import (
    DEFAULT_IDEMPOTENCY_STORE,
    ClaimResult,
    ClaimState,
    IdempotencyStore,
)

logger = logging.getLogger(__name__)


@dataclass
class RequestClaim:
    state: ClaimState
    key: Optional[str] = None
    owner_token: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    _heartbeat_stop: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )
    _heartbeat_thread: Optional[threading.Thread] = field(default=None, repr=False)


class RequestIdempotencyCoordinator:
    """Own request claims while graph executions are in flight."""

    def __init__(
        self,
        store: IdempotencyStore,
        ttl_seconds: int = settings.idempotency_request_ttl_seconds,
        lease_seconds: int = settings.idempotency_lease_seconds,
        wait_seconds: float = settings.idempotency_wait_seconds,
        poll_interval_seconds: float = settings.idempotency_poll_interval_seconds,
        heartbeat_interval_seconds: Optional[float] = None,
    ):
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.lease_seconds = lease_seconds
        self.wait_seconds = wait_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds or max(
            0.1,
            lease_seconds / 3,
        )

    def begin(
        self,
        logical_route: str,
        source: str,
        user_id: str,
        request_id: Optional[str],
    ) -> RequestClaim:
        if not request_id:
            return RequestClaim(ClaimState.UNAVAILABLE)

        key = build_request_idempotency_key(
            logical_route,
            source,
            user_id,
            request_id,
        )
        claim = self._claim(key)
        if claim.state is ClaimState.IN_PROGRESS:
            claim = self._wait(key)

        request_claim = RequestClaim(
            state=claim.state,
            key=key,
            owner_token=claim.owner_token,
            result=claim.result,
        )
        if request_claim.state is ClaimState.ACQUIRED:
            self._start_heartbeat(request_claim)
        self._log(request_claim.state.value, request_claim)
        return request_claim

    def complete(self, claim: RequestClaim, result: Dict[str, Any]) -> bool:
        self._stop_claim_heartbeat(claim)
        if (
            claim.state is not ClaimState.ACQUIRED
            or not claim.key
            or not claim.owner_token
        ):
            return False
        try:
            completed = self.store.complete(
                claim.key,
                claim.owner_token,
                result,
                self.ttl_seconds,
            )
        except Exception:
            completed = False
        self._log("completed" if completed else "completion_failed", claim)
        if not completed:
            self._abandon_owned(claim)
        return completed

    def abandon(self, claim: RequestClaim) -> bool:
        self._stop_claim_heartbeat(claim)
        return self._abandon_owned(claim)

    def _claim(self, key: str) -> ClaimResult:
        try:
            return self.store.claim(
                key,
                "request",
                self.ttl_seconds,
                self.lease_seconds,
            )
        except Exception:
            return ClaimResult(ClaimState.UNAVAILABLE)

    def _wait(self, key: str) -> ClaimResult:
        deadline = time.monotonic() + self.wait_seconds
        while time.monotonic() < deadline:
            time.sleep(
                min(
                    self.poll_interval_seconds,
                    max(0.0, deadline - time.monotonic()),
                )
            )
            claim = self._claim(key)
            if claim.state is not ClaimState.IN_PROGRESS:
                return claim
        return ClaimResult(ClaimState.IN_PROGRESS)

    def _start_heartbeat(self, claim: RequestClaim) -> None:
        def heartbeat() -> None:
            while not claim._heartbeat_stop.wait(self.heartbeat_interval_seconds):
                if not claim.key or not claim.owner_token:
                    return
                try:
                    renewed = self.store.renew(
                        claim.key,
                        claim.owner_token,
                        self.lease_seconds,
                    )
                except Exception:
                    renewed = False
                self._log("lease_renewed" if renewed else "lease_renew_failed", claim)

        claim._heartbeat_thread = threading.Thread(
            target=heartbeat,
            name="jarvis-idempotency-heartbeat",
            daemon=True,
        )
        claim._heartbeat_thread.start()

    @staticmethod
    def _stop_claim_heartbeat(claim: RequestClaim) -> None:
        claim._heartbeat_stop.set()
        thread = claim._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _abandon_owned(self, claim: RequestClaim) -> bool:
        if (
            claim.state is not ClaimState.ACQUIRED
            or not claim.key
            or not claim.owner_token
        ):
            return False
        try:
            abandoned = self.store.abandon(claim.key, claim.owner_token)
        except Exception:
            abandoned = False
        self._log("abandoned" if abandoned else "abandon_failed", claim)
        return abandoned

    @staticmethod
    def _log(action: str, claim: RequestClaim) -> None:
        logger.info(
            "Request idempotency state changed.",
            extra={
                "idempotency_action": action,
                "idempotency_layer": "request",
                "key_fingerprint": claim.key[:12] if claim.key else None,
            },
        )


DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR = RequestIdempotencyCoordinator(
    DEFAULT_IDEMPOTENCY_STORE
)

__all__ = [
    "DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR",
    "RequestClaim",
    "RequestIdempotencyCoordinator",
]
