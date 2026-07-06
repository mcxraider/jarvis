"""Checkpointing backend helpers.

Owns the checkpoint backend factory and the process-wide default checkpointer
used by the graph builder and runtime. ``DEFAULT_CHECKPOINTER`` is created once at
import time so a single instance is shared across the API and CLI entrypoints.
"""

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from agents.agent_api.app.checkpointing.postgres import create_postgres_checkpointer
from agents.agent_api.app.checkpointing.redis import create_redis_checkpointer
from agents.agent_api.app.config import settings


def create_default_checkpointer() -> Any:
    """Create the configured checkpoint backend for API/runtime defaults."""

    if settings.checkpoint_backend == "postgres":
        return create_postgres_checkpointer(
            settings.postgres_dsn,
            run_setup=settings.run_checkpoint_setup,
        )
    if settings.checkpoint_backend == "redis":
        return create_redis_checkpointer(settings.redis_url)
    if settings.checkpoint_backend == "memory":
        return InMemorySaver()
    raise RuntimeError(f"Unsupported JARVIS_CHECKPOINT_BACKEND: {settings.checkpoint_backend}")


DEFAULT_CHECKPOINTER = create_default_checkpointer()

__all__ = [
    "InMemorySaver",
    "create_default_checkpointer",
    "create_postgres_checkpointer",
    "create_redis_checkpointer",
    "DEFAULT_CHECKPOINTER",
]
