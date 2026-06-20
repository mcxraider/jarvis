"""Redis checkpointing placeholder.

Redis is intentionally not the production default in this v1 refactor.
"""

from typing import Any, Optional


def create_redis_checkpointer(redis_url: Optional[str]) -> Any:
    raise NotImplementedError(
        "Redis checkpointing is not implemented yet. Use Postgres or in-memory checkpointing."
    )

