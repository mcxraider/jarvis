"""Checkpointing backend helpers.

Owns the checkpoint backend factory and the process-wide default checkpointer
used by the graph builder and runtime. ``DEFAULT_CHECKPOINTER`` is created once at
import time so a single instance is shared across the API and CLI entrypoints.
"""

import threading
from typing import Any, Optional

from langgraph.checkpoint.memory import InMemorySaver

from agents.agent_api.app.checkpointing.postgres import (
    create_async_postgres_checkpointer,
    create_postgres_checkpointer,
)
from agents.agent_api.app.checkpointing.redis import create_redis_checkpointer
from agents.agent_api.app.config import settings


def create_default_checkpointer(*, run_setup: Optional[bool] = None) -> Any:
    """Create the configured checkpoint backend for API/runtime defaults."""

    if settings.checkpoint_backend == "postgres":
        return create_postgres_checkpointer(
            settings.postgres_dsn,
            run_setup=(
                settings.run_checkpoint_setup if run_setup is None else run_setup
            ),
        )
    if settings.checkpoint_backend == "redis":
        return create_redis_checkpointer(settings.redis_url)
    if settings.checkpoint_backend == "memory":
        return InMemorySaver()
    raise RuntimeError(f"Unsupported JARVIS_CHECKPOINT_BACKEND: {settings.checkpoint_backend}")


# Schema setup has one owner. The process default is constructed without DDL;
# FastAPI awaits async setup during lifespan, while direct sync/CLI runs call the
# guarded sync setup helper below.
DEFAULT_CHECKPOINTER = create_default_checkpointer(run_setup=False)
_CHECKPOINT_SETUP_COMPLETE = not (
    settings.checkpoint_backend == "postgres" and settings.run_checkpoint_setup
)
_CHECKPOINT_SETUP_LOCK = threading.Lock()
_ASYNC_RUNTIME_CHECKPOINTER: Any = None
_ASYNC_RUNTIME_LOOP: Any = None
_ASYNC_RUNTIME_POOL: Any = None
_ASYNC_RUNTIME_INITIALIZING = False
_ASYNC_RUNTIME_STATE_LOCK = threading.Lock()


async def initialize_async_checkpointer(async_pool: Any = None) -> Any:
    """Initialize the lifespan-owned checkpointer used by native async runs.

    Memory mode deliberately reuses ``DEFAULT_CHECKPOINTER`` so synchronous
    compatibility calls and async API calls share interrupt/resume state.  A
    Postgres saver is constructed only while an event loop is running because
    the saver captures that loop.
    """

    global _ASYNC_RUNTIME_CHECKPOINTER, _ASYNC_RUNTIME_LOOP, _ASYNC_RUNTIME_POOL
    global _ASYNC_RUNTIME_INITIALIZING
    import asyncio

    loop = asyncio.get_running_loop()
    with _ASYNC_RUNTIME_STATE_LOCK:
        if _ASYNC_RUNTIME_CHECKPOINTER is not None:
            if _ASYNC_RUNTIME_LOOP is not None and _ASYNC_RUNTIME_LOOP is not loop:
                raise RuntimeError("Async checkpointer belongs to a different event loop.")
            if _ASYNC_RUNTIME_POOL is not None and _ASYNC_RUNTIME_POOL is not async_pool:
                raise RuntimeError(
                    "Async checkpointer is bound to a different Postgres pool."
                )
            return _ASYNC_RUNTIME_CHECKPOINTER
        if _ASYNC_RUNTIME_INITIALIZING:
            raise RuntimeError("Async checkpointer initialization is already in progress.")
        _ASYNC_RUNTIME_INITIALIZING = True

    try:
        if settings.checkpoint_backend == "memory":
            checkpointer = DEFAULT_CHECKPOINTER
        elif settings.checkpoint_backend == "postgres":
            checkpointer = create_async_postgres_checkpointer(async_pool)
            if settings.run_checkpoint_setup and not _CHECKPOINT_SETUP_COMPLETE:
                if not _CHECKPOINT_SETUP_LOCK.acquire(blocking=False):
                    raise RuntimeError("Checkpoint schema setup is already in progress.")
                try:
                    if not _CHECKPOINT_SETUP_COMPLETE:
                        await checkpointer.setup()
                        _mark_checkpoint_setup_complete()
                finally:
                    _CHECKPOINT_SETUP_LOCK.release()
        elif settings.checkpoint_backend == "redis":
            raise NotImplementedError(
                "Async Redis checkpointing is not implemented. Use Postgres or memory."
            )
        else:
            raise RuntimeError(
                f"Unsupported JARVIS_CHECKPOINT_BACKEND: {settings.checkpoint_backend}"
            )

        with _ASYNC_RUNTIME_STATE_LOCK:
            _ASYNC_RUNTIME_CHECKPOINTER = checkpointer
            if settings.checkpoint_backend == "postgres":
                _ASYNC_RUNTIME_LOOP = loop
                _ASYNC_RUNTIME_POOL = async_pool
        return checkpointer
    finally:
        with _ASYNC_RUNTIME_STATE_LOCK:
            _ASYNC_RUNTIME_INITIALIZING = False


def get_async_checkpointer() -> Any:
    """Return the lifespan-owned async checkpointer after startup."""

    if _ASYNC_RUNTIME_CHECKPOINTER is None:
        raise RuntimeError(
            "Async checkpointer is not initialized. Start the FastAPI lifespan first."
        )
    return _ASYNC_RUNTIME_CHECKPOINTER


def _mark_checkpoint_setup_complete() -> None:
    global _CHECKPOINT_SETUP_COMPLETE
    _CHECKPOINT_SETUP_COMPLETE = True


def ensure_default_checkpointer_setup() -> None:
    """Run configured Postgres setup once for direct synchronous entrypoints."""

    global _CHECKPOINT_SETUP_COMPLETE
    if (
        settings.checkpoint_backend != "postgres"
        or not settings.run_checkpoint_setup
        or _CHECKPOINT_SETUP_COMPLETE
    ):
        return
    with _CHECKPOINT_SETUP_LOCK:
        if not _CHECKPOINT_SETUP_COMPLETE:
            DEFAULT_CHECKPOINTER.setup()
            _CHECKPOINT_SETUP_COMPLETE = True


def reset_async_checkpointer(expected: Any = None) -> None:
    """Forget a saver after drain, optionally only when identity still matches."""

    global _ASYNC_RUNTIME_CHECKPOINTER, _ASYNC_RUNTIME_LOOP, _ASYNC_RUNTIME_POOL
    with _ASYNC_RUNTIME_STATE_LOCK:
        if expected is not None and _ASYNC_RUNTIME_CHECKPOINTER is not expected:
            return
        _ASYNC_RUNTIME_CHECKPOINTER = None
        _ASYNC_RUNTIME_LOOP = None
        _ASYNC_RUNTIME_POOL = None


__all__ = [
    "InMemorySaver",
    "create_async_postgres_checkpointer",
    "create_default_checkpointer",
    "create_postgres_checkpointer",
    "create_redis_checkpointer",
    "DEFAULT_CHECKPOINTER",
    "ensure_default_checkpointer_setup",
    "get_async_checkpointer",
    "initialize_async_checkpointer",
    "reset_async_checkpointer",
]
