"""Lazy shared Postgres connection pools for Jarvis database access.

The synchronous pool remains available for existing request preparation and
telemetry paths.  Native async graph/checkpoint paths use the process-wide
``AsyncConnectionPool`` opened by the FastAPI lifespan.
"""

import asyncio
import logging
import threading
from typing import Any, Optional

from agents.agent_api.app.config import settings

logger = logging.getLogger(__name__)

_pool: Any = None
_pool_lock = threading.Lock()
_async_pool: Optional[Any] = None
_async_pool_loop: Optional[asyncio.AbstractEventLoop] = None
_async_pool_open_task: Optional[asyncio.Task[Any]] = None
_async_pool_state_lock = threading.Lock()

_REQUIRED_RUNTIME_TABLES = (
    "users",
    "telegram_identities",
    "user_preferences",
    "telegram_pending_clarifications",
    "telegram_conversation_gates",
    "rate_limits",
    "idempotency_results",
    "threads",
    "thread_memory_heads",
    "thread_messages",
)

_REQUIRED_IDEMPOTENCY_COLUMNS = (
    "idempotency_key",
    "layer",
    "tool_name",
    "status",
    "owner_token",
    "result_json",
    "created_at",
    "lease_expires_at",
    "expires_at",
)

_REQUIRED_IDEMPOTENCY_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")


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
            max_size=16,
            kwargs={"autocommit": True, "prepare_threshold": None},
            open=False,
            check=ConnectionPool.check_connection,
            max_idle=300,
            max_lifetime=1800,
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

                cursor.execute(
                    """
                    SELECT required.column_name
                    FROM unnest(%s::text[]) AS required(column_name)
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM information_schema.columns actual
                        WHERE actual.table_schema = 'public'
                          AND actual.table_name = 'idempotency_results'
                          AND actual.column_name = required.column_name
                    )
                    """,
                    (list(_REQUIRED_IDEMPOTENCY_COLUMNS),),
                )
                missing_columns = [row[0] for row in cursor.fetchall()]
                if missing_columns:
                    raise RuntimeError("Idempotency table schema is incomplete")

                cursor.execute(
                    """
                    SELECT required.privilege
                    FROM unnest(%s::text[]) AS required(privilege)
                    WHERE NOT has_table_privilege(
                        current_user,
                        'public.idempotency_results',
                        required.privilege
                    )
                    """,
                    (list(_REQUIRED_IDEMPOTENCY_PRIVILEGES),),
                )
                missing_privileges = [row[0] for row in cursor.fetchall()]
                if missing_privileges:
                    raise RuntimeError("Idempotency table privileges are incomplete")

                cursor.execute(
                    """
                    WITH required_columns(table_name, column_name, data_type, nullable) AS (
                        VALUES
                            ('threads', 'conversation_key', 'text', 'YES'),
                            ('threads', 'lineage_id', 'uuid', 'YES'),
                            ('threads', 'previous_thread_id', 'text', 'YES'),
                            ('threads', 'memory_status', 'text', 'YES'),
                            ('thread_memory_heads', 'user_id', 'uuid', 'NO'),
                            ('thread_memory_heads', 'conversation_key', 'text', 'NO'),
                            ('thread_memory_heads', 'lineage_id', 'uuid', 'NO'),
                            ('thread_memory_heads', 'latest_thread_id', 'text', 'YES'),
                            ('thread_memory_heads', 'updated_at', 'timestamp with time zone', 'NO'),
                            ('thread_messages', 'id', 'bigint', 'NO'),
                            ('thread_messages', 'thread_id', 'text', 'NO'),
                            ('thread_messages', 'user_id', 'uuid', 'NO'),
                            ('thread_messages', 'sequence', 'integer', 'NO'),
                            ('thread_messages', 'kind', 'text', 'NO'),
                            ('thread_messages', 'payload', 'jsonb', 'NO'),
                            ('thread_messages', 'created_at', 'timestamp with time zone', 'NO')
                    ), required_constraints(name) AS (
                        VALUES
                            ('threads_conversation_key_check'),
                            ('threads_memory_status_check'),
                            ('threads_previous_thread_id_fkey'),
                            ('threads_status_check'),
                            ('thread_memory_heads_pkey'),
                            ('thread_memory_heads_conversation_key_check'),
                            ('thread_memory_heads_latest_thread_id_fkey'),
                            ('thread_messages_pkey'),
                            ('thread_messages_thread_id_fkey'),
                            ('thread_messages_user_id_fkey'),
                            ('thread_messages_sequence_check'),
                            ('thread_messages_kind_check'),
                            ('thread_messages_payload_check'),
                            ('thread_messages_thread_id_sequence_key')
                    ), required_privileges(table_name, privilege) AS (
                        SELECT table_name, privilege
                        FROM unnest(ARRAY[
                            'threads',
                            'thread_memory_heads',
                            'thread_messages'
                        ]) AS tables(table_name)
                        CROSS JOIN unnest(ARRAY[
                            'SELECT', 'INSERT', 'UPDATE', 'DELETE'
                        ]) AS privileges(privilege)
                    )
                    SELECT 'column:' || required.table_name || '.' || required.column_name
                    FROM required_columns required
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM information_schema.columns actual
                        WHERE actual.table_schema = 'public'
                          AND actual.table_name = required.table_name
                          AND actual.column_name = required.column_name
                          AND actual.data_type = required.data_type
                          AND actual.is_nullable = required.nullable
                    )
                    UNION ALL
                    SELECT 'constraint:' || required.name
                    FROM required_constraints required
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint actual
                        WHERE actual.conname = required.name
                          AND actual.connamespace = 'public'::regnamespace
                    )
                    UNION ALL
                    SELECT 'privilege:' || required.table_name || ':' || required.privilege
                    FROM required_privileges required
                    WHERE NOT has_table_privilege(
                        current_user,
                        'public.' || required.table_name,
                        required.privilege
                    )
                    UNION ALL
                    SELECT 'sequence:thread_messages_id_seq:USAGE'
                    WHERE NOT has_sequence_privilege(
                        current_user,
                        'public.thread_messages_id_seq',
                        'USAGE'
                    )
                    UNION ALL
                    SELECT 'function:prepare_thread_memory:EXECUTE'
                    WHERE NOT has_function_privilege(
                        current_user,
                        'public.prepare_thread_memory(bigint,text,text,text,boolean)',
                        'EXECUTE'
                    )
                    """
                )
                missing_memory_contract = [row[0] for row in cursor.fetchall()]
                if missing_memory_contract:
                    raise RuntimeError("Thread-memory database contract is incomplete")

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


async def open_async_pool() -> Any:
    """Open and return the shared async pool, or ``None`` without a DSN.

    Construction is completed before publishing the singleton.  A failed open
    therefore cannot leave a partially initialized pool visible to later calls.
    FastAPI calls this once during lifespan startup before synchronous database
    readiness checks.
    """

    global _async_pool, _async_pool_loop, _async_pool_open_task
    loop = asyncio.get_running_loop()
    dsn = settings.postgres_dsn
    if not dsn:
        return None

    with _async_pool_state_lock:
        if _async_pool is not None:
            if _async_pool_loop is not loop:
                raise RuntimeError("Async DB pool belongs to a different event loop.")
            return _async_pool
        task = _async_pool_open_task
        if task is None:
            task = loop.create_task(_create_async_pool(dsn))
            _async_pool_open_task = task
        elif task.get_loop() is not loop:
            raise RuntimeError("Async DB pool startup is already owned by another loop.")

    try:
        pool = await asyncio.shield(task)
    except BaseException:
        if task.done():
            with _async_pool_state_lock:
                if _async_pool_open_task is task:
                    _async_pool_open_task = None
        raise
    with _async_pool_state_lock:
        if _async_pool_open_task is task:
            _async_pool = pool
            _async_pool_loop = loop
            _async_pool_open_task = None
            return pool
        if _async_pool is pool and _async_pool_loop is loop:
            return pool
    raise RuntimeError("Async DB pool startup was invalidated before publication.")


async def _create_async_pool(dsn: str) -> Any:
    """Construct a ready pool and clean up every partially-open failure."""

    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=2,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": None},
        open=False,
        check=AsyncConnectionPool.check_connection,
        max_idle=300,
        max_lifetime=1800,
    )
    try:
        await pool.open()
        await pool.wait(timeout=5.0)
    except BaseException as open_error:
        try:
            await pool.close()
        except BaseException as close_error:
            raise BaseExceptionGroup(
                "Async DB pool startup and rollback both failed.",
                [open_error, close_error],
            )
        raise
    return pool


def get_async_pool() -> Any:
    """Return the opened async pool or raise when startup has not opened it."""

    with _async_pool_state_lock:
        pool = _async_pool
        owner_loop = _async_pool_loop
    if pool is None:
        raise RuntimeError(
            "Async DB pool is not open. Call open_async_pool() during lifespan startup."
        )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and owner_loop is not loop:
        raise RuntimeError("Async DB pool belongs to a different event loop.")
    return pool


async def close_async_pool() -> None:
    """Clear and close the shared async pool; safe to call repeatedly."""

    global _async_pool, _async_pool_loop, _async_pool_open_task
    loop = asyncio.get_running_loop()
    with _async_pool_state_lock:
        if _async_pool is not None and _async_pool_loop is not loop:
            raise RuntimeError("Async DB pool must be closed by its owning event loop.")
        if (
            _async_pool_open_task is not None
            and _async_pool_open_task.get_loop() is not loop
        ):
            raise RuntimeError("Async DB pool startup belongs to a different event loop.")
        pool = _async_pool
        _async_pool = None
        _async_pool_loop = None
        open_task = _async_pool_open_task
        _async_pool_open_task = None
    if open_task is not None:
        if not open_task.done():
            open_task.cancel()
        try:
            pending_pool = await open_task
        except asyncio.CancelledError:
            pass
        except BaseException:
            # A normally awaited startup clears this task before propagating its
            # error. Reaching it here means shutdown owned the in-flight task, so
            # surface its rollback failure to the lifespan aggregator.
            raise
        else:
            if pool is None:
                pool = pending_pool
    if pool is not None:
        await pool.close()
