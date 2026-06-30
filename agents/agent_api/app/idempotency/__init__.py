"""Idempotency store factory and process-wide lazy default."""

from typing import Optional

from agents.agent_api.app.config import settings
from agents.agent_api.app.idempotency.postgres import PostgresIdempotencyStore
from agents.agent_api.app.idempotency.store import (
    ClaimResult,
    ClaimState,
    IdempotencyStore,
    MemoryIdempotencyStore,
)


def create_default_idempotency_store(
    postgres_dsn: Optional[str] = None,
) -> IdempotencyStore:
    dsn = postgres_dsn if postgres_dsn is not None else settings.postgres_dsn
    if dsn:
        return PostgresIdempotencyStore(dsn)
    return MemoryIdempotencyStore()


DEFAULT_IDEMPOTENCY_STORE = create_default_idempotency_store()

__all__ = [
    "ClaimResult",
    "ClaimState",
    "DEFAULT_IDEMPOTENCY_STORE",
    "IdempotencyStore",
    "MemoryIdempotencyStore",
    "PostgresIdempotencyStore",
    "create_default_idempotency_store",
]
