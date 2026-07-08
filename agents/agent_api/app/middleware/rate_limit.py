"""Per-user fresh-thread quota backed by Supabase."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from agents.agent_api.app.config import settings
from agents.agent_api.app.user_context.identity import TelegramIdentity

logger = logging.getLogger(__name__)
SINGAPORE_TZ = ZoneInfo("Asia/Singapore")


@dataclass(frozen=True)
class QuotaResult:
    allowed: bool
    threads_used: int
    thread_limit: int
    reset_at: datetime


def _as_quota_result(row: object) -> QuotaResult:
    if isinstance(row, dict):
        allowed = row["allowed"]
        threads_used = row["threads_used"]
        thread_limit = row["thread_limit"]
        reset_at = row["reset_at"]
    else:
        allowed, threads_used, thread_limit, reset_at = row  # type: ignore[misc]

    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    return QuotaResult(
        allowed=bool(allowed),
        threads_used=int(threads_used),
        thread_limit=int(thread_limit),
        reset_at=reset_at,
    )


def _retry_after_seconds(reset_at: datetime) -> int:
    now = datetime.now(timezone.utc)
    reset_utc = reset_at.astimezone(timezone.utc)
    return max(1, math.ceil((reset_utc - now).total_seconds()))


def _reset_detail(reset_at: datetime) -> str:
    reset_sgt = reset_at.astimezone(SINGAPORE_TZ)
    return reset_sgt.strftime("%Y-%m-%d %H:%M:%S %Z")


def _is_transient_db_error(exc: Exception) -> bool:
    try:
        from psycopg import OperationalError
        from psycopg_pool import PoolTimeout
    except Exception:
        return type(exc).__name__ in {"OperationalError", "PoolTimeout"}
    return isinstance(exc, (OperationalError, PoolTimeout))


def _is_known_quota_error(exc: Exception) -> bool:
    try:
        from psycopg import IntegrityError, errors
    except Exception:
        return type(exc).__name__ in {
            "UndefinedFunction",
            "RaiseException",
            "IntegrityError",
            "CheckViolation",
            "ForeignKeyViolation",
            "NotNullViolation",
            "UniqueViolation",
        }

    known_error_types = (
        errors.UndefinedFunction,
        errors.RaiseException,
        errors.CheckViolation,
        errors.ForeignKeyViolation,
        errors.NotNullViolation,
        errors.UniqueViolation,
        IntegrityError,
    )
    return isinstance(exc, known_error_types)


def consume_new_thread_quota(identity: Optional[TelegramIdentity]) -> Optional[QuotaResult]:
    """Consume one fresh-thread quota slot for the Telegram identity.

    No-ops when: postgres_dsn unset or identity is None. Transient database
    unavailability fails open; known quota schema/data errors fail closed.
    """
    if not settings.postgres_dsn or identity is None:
        return None

    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT allowed, threads_used, thread_limit, reset_at
                    FROM public.try_consume_thread_quota(%s)
                    """,
                    (identity.telegram_id,),
                )
                row = cur.fetchone()
        if row is None:
            raise RuntimeError("try_consume_thread_quota returned no row")

        result = _as_quota_result(row)
        if result.allowed:
            return result

        retry_after = _retry_after_seconds(result.reset_at)
        raise HTTPException(
            status_code=429,
            detail=(
                "Daily thread limit reached. Your limit resets at "
                f"{_reset_detail(result.reset_at)}."
            ),
            headers={"Retry-After": str(retry_after)},
        )
    except HTTPException:
        raise
    except Exception as exc:
        if _is_transient_db_error(exc):
            logger.warning(
                "Thread quota check failed open due to transient database error.",
                extra={"error": type(exc).__name__},
            )
            return None
        if _is_known_quota_error(exc):
            logger.error(
                "Thread quota check failed closed due to known quota database error.",
                extra={"error": type(exc).__name__},
            )
            raise HTTPException(
                status_code=503,
                detail="Thread quota service is temporarily unavailable.",
            ) from exc
        logger.warning(
            "Thread quota check failed open due to unexpected database error.",
            extra={"error": type(exc).__name__},
        )
        return None
