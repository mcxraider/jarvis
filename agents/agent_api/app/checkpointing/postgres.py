"""Postgres checkpointing helpers.

The optional LangGraph Postgres checkpoint package is imported lazily so local
tests can keep using in-memory checkpointing without a database dependency.
"""

from typing import Any, Optional


def create_postgres_checkpointer(dsn: Optional[str]) -> Any:
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
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    checkpointer = PostgresSaver(pool)
    if hasattr(checkpointer, "setup"):
        checkpointer.setup()
    return checkpointer
