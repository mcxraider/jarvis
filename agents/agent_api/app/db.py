"""Lazy shared Postgres connection pool for Jarvis user-data queries."""

import logging
import threading
from typing import Any

from agents.agent_api.app.config import settings

logger = logging.getLogger(__name__)

_pool: Any = None
_pool_lock = threading.Lock()


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
