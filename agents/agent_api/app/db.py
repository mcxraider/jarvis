"""Lazy shared Postgres connection pool for Jarvis user-data queries."""

import logging
import threading
from typing import Any

from agents.agent_api.app.config import settings

logger = logging.getLogger(__name__)

_pool: Any = None
_pool_lock = threading.Lock()

_REQUIRED_RUNTIME_TABLES = (
    "users",
    "user_identities",
    "user_preferences",
    "telegram_pending_clarifications",
    "telegram_conversation_gates",
    "rate_limits",
)


def get_pool() -> Any:
    """Return the shared ConnectionPool, creating it lazily on first call.

    Raises RuntimeError if settings.postgres_dsn is None/empty.
    """
    global _pool
    if _pool is not None:
        return _pool

    with _pool_lock:
        if _pool is not None:
            return _pool

        dsn = settings.postgres_dsn
        if not dsn:
            raise RuntimeError(
                "Shared DB pool requires JARVIS_POSTGRES_DSN or DATABASE_URL."
            )

        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            conninfo=dsn,
            min_size=2,
            max_size=10,
            kwargs={"autocommit": True, "prepare_threshold": None},
            open=False,
        )
        try:
            pool.open()
            pool.wait(timeout=5.0)
        except Exception:
            pool.close()
            raise
        _pool = pool
        logger.info("Shared DB pool opened.")
        return _pool


def verify_database_runtime() -> None:
    """Fail fast when the configured DSN is not the least-privilege app runtime."""

    if not settings.postgres_dsn:
        return

    pool = get_pool()
    try:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT current_user,
                           pg_has_role(current_user, 'jarvis_runtime', 'MEMBER')
                    """
                )
                role, inherits_runtime = cursor.fetchone()
                if role != "jarvis_app":
                    raise RuntimeError(
                        f"Database runtime must connect as jarvis_app; connected as {role}"
                    )
                if not inherits_runtime:
                    raise RuntimeError(
                        "Database role jarvis_app must inherit jarvis_runtime"
                    )

                cursor.execute(
                    """
                    SELECT required.table_name
                    FROM unnest(%s::text[]) AS required(table_name)
                    WHERE to_regclass('public.' || required.table_name) IS NULL
                    """,
                    (list(_REQUIRED_RUNTIME_TABLES),),
                )
                missing_tables = [row[0] for row in cursor.fetchall()]
                if missing_tables:
                    raise RuntimeError(
                        "Database migrations are incomplete; missing: "
                        + ", ".join(f"public.{name}" for name in missing_tables)
                    )

                for table_name in _REQUIRED_RUNTIME_TABLES:
                    cursor.execute(f"SELECT 1 FROM public.{table_name} LIMIT 0")
    except Exception as error:
        raise RuntimeError(
            "Database runtime readiness failed. Apply Supabase migrations and "
            "verify JARVIS_POSTGRES_DSN uses jarvis_app."
        ) from error

    logger.info(
        "Database runtime ready.",
        extra={
            "database_role": "jarvis_app",
            "inherited_role": "jarvis_runtime",
            "required_table_count": len(_REQUIRED_RUNTIME_TABLES),
        },
    )


def close_pool() -> None:
    """Close the shared pool if it was opened. Safe to call multiple times."""
    global _pool
    if _pool is not None:
        try:
            _pool.close()
            logger.info("Shared DB pool closed.")
        except Exception as exc:
            logger.warning("Pool close error.", extra={"error": type(exc).__name__})
        finally:
            _pool = None
