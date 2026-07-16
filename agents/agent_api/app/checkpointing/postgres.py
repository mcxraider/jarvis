"""Postgres checkpointing helpers.

The optional LangGraph Postgres checkpoint package is imported lazily so local
tests can keep using in-memory checkpointing without a database dependency.
Both synchronous and asynchronous saver factories are exposed while backend
selection remains owned by ``checkpointing.__init__``.
"""

from typing import Any, Optional


def create_postgres_checkpointer(dsn: Optional[str], *, run_setup: bool = False) -> Any:
    if not dsn:
        raise RuntimeError("JARVIS_POSTGRES_DSN or DATABASE_URL is required for Postgres checkpointing.")
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool
    except ImportError as error:
        raise RuntimeError(
            "Postgres checkpointing requires langgraph-checkpoint-postgres and psycopg[pool]."
        ) from error

    pool = ConnectionPool(
        conninfo=dsn,
        kwargs={"autocommit": True, "prepare_threshold": None},
    )
    checkpointer = PostgresSaver(pool)
    if run_setup and hasattr(checkpointer, "setup"):
        checkpointer.setup()
    return checkpointer


def create_async_postgres_checkpointer(async_pool: Any) -> Any:
    """Build an ``AsyncPostgresSaver`` over an already-open shared pool."""

    if async_pool is None:
        raise RuntimeError("An open async Postgres pool is required for checkpointing.")
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as error:
        raise RuntimeError(
            "Async Postgres checkpointing requires langgraph-checkpoint-postgres."
        ) from error
    return AsyncPostgresSaver(async_pool)
