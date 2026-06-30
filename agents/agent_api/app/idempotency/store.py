"""Idempotency store contracts and the in-memory implementation."""

from __future__ import annotations

import copy
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional, Protocol


class ClaimState(str, Enum):
    ACQUIRED = "acquired"
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ClaimResult:
    state: ClaimState
    result: Optional[Dict[str, Any]] = None
    owner_token: Optional[str] = None


class IdempotencyStore(Protocol):
    def claim(
        self,
        key: str,
        layer: str,
        ttl_seconds: int,
        lease_seconds: int,
        tool_name: Optional[str] = None,
    ) -> ClaimResult: ...

    def get(self, key: str) -> Optional[Dict[str, Any]]: ...

    def complete(
        self,
        key: str,
        owner_token: str,
        result: Dict[str, Any],
        ttl_seconds: int,
    ) -> bool: ...

    def renew(self, key: str, owner_token: str, lease_seconds: int) -> bool: ...

    def abandon(self, key: str, owner_token: str) -> bool: ...

    def cleanup_expired(self) -> int: ...


@dataclass
class _MemoryRecord:
    layer: str
    tool_name: Optional[str]
    status: str
    owner_token: str
    result: Optional[Dict[str, Any]]
    lease_expires_at: float
    expires_at: float


class MemoryIdempotencyStore:
    """Thread-safe local store for tests and single-process development."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._records: Dict[str, _MemoryRecord] = {}
        self._lock = threading.RLock()

    def claim(
        self,
        key: str,
        layer: str,
        ttl_seconds: int,
        lease_seconds: int,
        tool_name: Optional[str] = None,
    ) -> ClaimResult:
        _validate_claim_durations(ttl_seconds, lease_seconds)
        now = self._clock()

        with self._lock:
            record = self._records.get(key)
            if record is not None and record.expires_at <= now:
                del self._records[key]
                record = None

            if record is not None and record.status == "completed":
                return ClaimResult(
                    ClaimState.COMPLETED,
                    result=copy.deepcopy(record.result),
                )

            if record is not None and record.lease_expires_at > now:
                return ClaimResult(ClaimState.IN_PROGRESS)

            owner_token = str(uuid.uuid4())
            self._records[key] = _MemoryRecord(
                layer=layer,
                tool_name=tool_name,
                status="in_progress",
                owner_token=owner_token,
                result=None,
                lease_expires_at=now + lease_seconds,
                expires_at=now + ttl_seconds,
            )
            return ClaimResult(ClaimState.ACQUIRED, owner_token=owner_token)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        now = self._clock()
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return None
            if record.expires_at <= now:
                del self._records[key]
                return None
            if record.status != "completed":
                return None
            return copy.deepcopy(record.result)

    def complete(
        self,
        key: str,
        owner_token: str,
        result: Dict[str, Any],
        ttl_seconds: int,
    ) -> bool:
        _validate_duration("ttl_seconds", ttl_seconds)
        now = self._clock()
        with self._lock:
            record = self._records.get(key)
            if (
                record is None
                or record.status != "in_progress"
                or record.owner_token != owner_token
            ):
                return False
            record.status = "completed"
            record.result = copy.deepcopy(result)
            record.lease_expires_at = 0.0
            record.expires_at = now + ttl_seconds
            return True

    def abandon(self, key: str, owner_token: str) -> bool:
        with self._lock:
            record = self._records.get(key)
            if (
                record is None
                or record.status != "in_progress"
                or record.owner_token != owner_token
            ):
                return False
            del self._records[key]
            return True

    def renew(self, key: str, owner_token: str, lease_seconds: int) -> bool:
        _validate_duration("lease_seconds", lease_seconds)
        now = self._clock()
        with self._lock:
            record = self._records.get(key)
            if (
                record is None
                or record.status != "in_progress"
                or record.owner_token != owner_token
                or record.expires_at <= now
            ):
                return False
            record.lease_expires_at = now + lease_seconds
            return True

    def cleanup_expired(self) -> int:
        now = self._clock()
        with self._lock:
            expired = [
                key for key, record in self._records.items() if record.expires_at <= now
            ]
            for key in expired:
                del self._records[key]
            return len(expired)


def _validate_duration(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")


def _validate_claim_durations(ttl_seconds: int, lease_seconds: int) -> None:
    _validate_duration("ttl_seconds", ttl_seconds)
    _validate_duration("lease_seconds", lease_seconds)
    if lease_seconds > ttl_seconds:
        raise ValueError("lease_seconds must not exceed ttl_seconds.")


__all__ = [
    "ClaimResult",
    "ClaimState",
    "IdempotencyStore",
    "MemoryIdempotencyStore",
]
