"""Database-backed credential and preference resolution."""

import logging
from typing import Any, Dict, Optional

from agents.agent_api.app.config import settings

logger = logging.getLogger(__name__)


def get_credential(
    telegram_user_id: Optional[int],
    service: str = "todoist",
) -> Optional[str]:
    """Fetch an API key from user_credentials for the given telegram user.

    Returns None (not raises) on any failure — callers fall through to env var.
    """
    if telegram_user_id is None or not settings.postgres_dsn:
        return None
    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT uc.credential_data->>'api_key'
                    FROM user_credentials uc
                    JOIN users u ON u.id = uc.user_id
                    WHERE u.telegram_user_id = %s
                      AND uc.service = %s
                      AND uc.is_active = TRUE
                    LIMIT 1
                    """,
                    (str(telegram_user_id), service),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as exc:
        logger.warning(
            "Credential lookup failed, falling through to env var.",
            extra={"service": service, "error": type(exc).__name__},
        )
        return None


def get_user_preferences(telegram_user_id: Optional[int]) -> Dict[str, Any]:
    """Fetch user preferences from Supabase. Returns empty dict on failure."""
    if telegram_user_id is None or not settings.postgres_dsn:
        return {}
    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT up.preferences
                    FROM user_preferences up
                    JOIN users u ON u.id = up.user_id
                    WHERE u.telegram_user_id = %s
                    LIMIT 1
                    """,
                    (str(telegram_user_id),),
                )
                row = cur.fetchone()
                return row[0] if row and isinstance(row[0], dict) else {}
    except Exception as exc:
        logger.warning(
            "Preferences lookup failed.",
            extra={"error": type(exc).__name__},
        )
        return {}
