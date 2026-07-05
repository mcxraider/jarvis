"""Thread ownership guard — blocks if thread belongs to another user."""

import logging
from typing import Optional

from fastapi import HTTPException

from agents.agent_api.app.config import settings

logger = logging.getLogger(__name__)


def validate_thread_ownership(
    thread_id: str,
    telegram_user_id: Optional[int],
) -> None:
    """Raise 403 if thread_id belongs to a different telegram user.

    No-ops when: postgres_dsn unset, telegram_user_id is None, or thread not
    found in registry (legacy threads predate registration).
    """
    if not settings.postgres_dsn or telegram_user_id is None:
        return

    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT t.user_id = public.resolve_user_id(%s)
                    FROM threads t
                    WHERE t.thread_id = %s
                    """,
                    (str(telegram_user_id), thread_id),
                )
                row = cur.fetchone()
                if row is None:
                    return
                if not row[0]:
                    raise HTTPException(
                        status_code=403,
                        detail="Thread belongs to a different user.",
                    )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Thread ownership check failed (allowing request).",
            extra={"thread_id": thread_id, "error": type(exc).__name__},
        )
