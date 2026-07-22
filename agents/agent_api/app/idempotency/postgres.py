"""Lazy Postgres idempotency storage with migration-owned schema."""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import nullcontext
from typing import Any, Callable, Dict, Optional

from agents.agent_api.app.idempotency.store import ClaimResult, ClaimState

logger = logging.getLogger(__name__)

_CLEANUP_ADVISORY_LOCK_ID = 475_271_901


def _check_connection(conn: Any) -> None:
    conn.execute("SELECT 1")

class PostgresIdempotencyStore:
    """Postgres store that lazily connects without changing database schema."""

    def __init__(
        self,
        dsn: str,
        pool_factory: Optional[Callable[..., Any]] = None,
    ):
        if not dsn:
            raise ValueError("A Postgres DSN is required.")
        self._dsn = dsn
        self._pool_factory = pool_factory
        self._pool: Optional[Any] = None
        self._pool_lock = threading.Lock()

    def claim(
        self,
        key: str,
        layer: str,
        ttl_seconds: int,
        lease_seconds: int,
        tool_name: Optional[str] = None,
    ) -> ClaimResult:
        _validate_claim_durations(ttl_seconds, lease_seconds)
        owner_token = str(uuid.uuid4())
        try:
            pool = self._ensure_pool()
            with pool.connection() as connection:
                transaction = (
                    connection.transaction()
                    if hasattr(connection, "transaction")
                    else nullcontext()
                )
                with transaction:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO idempotency_results (
                                idempotency_key, layer, tool_name, status,
                                owner_token, lease_expires_at, expires_at
                            )
                            VALUES (
                                %s, %s, %s, 'in_progress', %s,
                                NOW() + (%s * INTERVAL '1 second'),
                                NOW() + (%s * INTERVAL '1 second')
                            )
                            ON CONFLICT DO NOTHING
                            RETURNING owner_token
                            """,
                            (
                                key,
                                layer,
                                tool_name,
                                owner_token,
                                lease_seconds,
                                ttl_seconds,
                            ),
                        )
                        if cursor.fetchone() is not None:
                            return ClaimResult(
                                ClaimState.ACQUIRED,
                                owner_token=owner_token,
                            )

                        cursor.execute(
                            """
                            SELECT status, result_json, lease_expires_at <= NOW(),
                                   expires_at <= NOW()
                            FROM idempotency_results
                            WHERE idempotency_key = %s
                            FOR UPDATE
                            """,
                            (key,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            return ClaimResult(ClaimState.UNAVAILABLE)

                        status, result, lease_expired, expired = row
                        if status == "completed" and not expired:
                            return ClaimResult(ClaimState.COMPLETED, result=result)
                        if not expired and not lease_expired:
                            return ClaimResult(ClaimState.IN_PROGRESS)

                        cursor.execute(
                            """
                            UPDATE idempotency_results
                            SET layer = %s,
                                tool_name = %s,
                                status = 'in_progress',
                                owner_token = %s,
                                result_json = NULL,
                                created_at = NOW(),
                                lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                                expires_at = NOW() + (%s * INTERVAL '1 second')
                            WHERE idempotency_key = %s
                            RETURNING owner_token
                            """,
                            (
                                layer,
                                tool_name,
                                owner_token,
                                lease_seconds,
                                ttl_seconds,
                                key,
                            ),
                        )
                        if cursor.fetchone() is None:
                            return ClaimResult(ClaimState.IN_PROGRESS)
                        return ClaimResult(
                            ClaimState.ACQUIRED,
                            owner_token=owner_token,
                        )
        except Exception as error:
            self._warn("claim", error)
            return ClaimResult(ClaimState.UNAVAILABLE)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            pool = self._ensure_pool()
            with pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT result_json
                        FROM idempotency_results
                        WHERE idempotency_key = %s
                          AND status = 'completed'
                          AND expires_at > NOW()
                        """,
                        (key,),
                    )
                    row = cursor.fetchone()
                    return row[0] if row is not None else None
        except Exception as error:
            self._warn("get", error)
            return None

    def complete(
        self,
        key: str,
        owner_token: str,
        result: Dict[str, Any],
        ttl_seconds: int,
    ) -> bool:
        _validate_duration("ttl_seconds", ttl_seconds)
        try:
            from psycopg.types.json import Jsonb

            pool = self._ensure_pool()
            with pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE idempotency_results
                        SET status = 'completed',
                            result_json = %s,
                            lease_expires_at = NULL,
                            expires_at = NOW() + (%s * INTERVAL '1 second')
                        WHERE idempotency_key = %s
                          AND status = 'in_progress'
                          AND owner_token = %s
                        """,
                        (Jsonb(result), ttl_seconds, key, owner_token),
                    )
                    return cursor.rowcount == 1
        except Exception as error:
            self._warn("complete", error)
            return False

    def abandon(self, key: str, owner_token: str) -> bool:
        try:
            pool = self._ensure_pool()
            with pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM idempotency_results
                        WHERE idempotency_key = %s
                          AND status = 'in_progress'
                          AND owner_token = %s
                        """,
                        (key, owner_token),
                    )
                    return cursor.rowcount == 1
        except Exception as error:
            self._warn("abandon", error)
            return False

    def renew(self, key: str, owner_token: str, lease_seconds: int) -> bool:
        _validate_duration("lease_seconds", lease_seconds)
        try:
            pool = self._ensure_pool()
            with pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE idempotency_results
                        SET lease_expires_at = NOW() + (%s * INTERVAL '1 second')
                        WHERE idempotency_key = %s
                          AND status = 'in_progress'
                          AND owner_token = %s
                          AND expires_at > NOW()
                        """,
                        (lease_seconds, key, owner_token),
                    )
                    return cursor.rowcount == 1
        except Exception as error:
            self._warn("renew", error)
            return False

    def cleanup_expired(self) -> int:
        try:
            pool = self._ensure_pool()
            with pool.connection() as connection:
                transaction = (
                    connection.transaction()
                    if hasattr(connection, "transaction")
                    else nullcontext()
                )
                with transaction:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_try_advisory_xact_lock(%s)",
                            (_CLEANUP_ADVISORY_LOCK_ID,),
                        )
                        row = cursor.fetchone()
                        if row is None or not row[0]:
                            return 0
                        cursor.execute(
                            "DELETE FROM idempotency_results WHERE expires_at <= NOW()"
                        )
                        return cursor.rowcount
        except Exception as error:
            self._warn("cleanup", error)
            return 0

    def close(self) -> None:
        with self._pool_lock:
            if self._pool is not None and hasattr(self._pool, "close"):
                self._pool.close()
            self._pool = None

    def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool

        with self._pool_lock:
            if self._pool is not None:
                return self._pool

            factory = self._pool_factory
            if factory is None:
                from psycopg_pool import ConnectionPool

                factory = ConnectionPool
                self._pool = factory(
                    conninfo=self._dsn,
                    kwargs={"autocommit": True, "prepare_threshold": None},
                    open=False,
                    min_size=2,
                    max_size=5,
                    check=ConnectionPool.check_connection,
                    max_idle=300,
                    max_lifetime=1800,
                )
            else:
                self._pool = factory(
                    conninfo=self._dsn,
                    kwargs={"autocommit": True, "prepare_threshold": None},
                    open=False,
                    min_size=2,
                    max_size=5,
                    check=_check_connection,
                    max_idle=300,
                    max_lifetime=1800,
                )
            if hasattr(self._pool, "open"):
                self._pool.open()
            if hasattr(self._pool, "wait"):
                self._pool.wait()
            logger.info(
                "Idempotency database pool ready.",
                extra={"idempotency_operation": "connect"},
            )
            return self._pool

    @staticmethod
    def _warn(operation: str, error: Exception) -> None:
        logger.warning(
            "Idempotency store operation failed open.",
            extra={
                "idempotency_operation": operation,
                "error_type": type(error).__name__,
            },
        )


def _validate_duration(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")


def _validate_claim_durations(ttl_seconds: int, lease_seconds: int) -> None:
    _validate_duration("ttl_seconds", ttl_seconds)
    _validate_duration("lease_seconds", lease_seconds)
    if lease_seconds > ttl_seconds:
        raise ValueError("lease_seconds must not exceed ttl_seconds.")


__all__ = ["PostgresIdempotencyStore"]
