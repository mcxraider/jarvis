"""Per-user daily rate limiting backed by Supabase rate_limits table."""

import logging
from typing import Optional

from fastapi import HTTPException

from agents.agent_api.app.config import settings
from agents.agent_api.app.user_context.identity import TelegramIdentity

logger = logging.getLogger(__name__)


def check_rate_limit(identity: Optional[TelegramIdentity]) -> None:
    """Atomic check-and-increment against the rate_limits table.

    No-ops when: postgres_dsn unset, identity is None, or no
    rate_limits row exists for the user (unlimited by default).
    Raises HTTPException(429) when over limit.
    """
    if not settings.postgres_dsn or identity is None:
        return

    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.rate_limits rl
                    SET daily_requests_used = CASE
                            WHEN rl.reset_at <= NOW() THEN 1
                            ELSE LEAST(rl.daily_requests_used + 1,
                                       rl.daily_request_limit + 1)
                        END,
                        reset_at = CASE
                            WHEN rl.reset_at <= NOW()
                            THEN DATE_TRUNC('day', NOW()) + INTERVAL '1 day'
                            ELSE rl.reset_at
                        END,
                        updated_at = NOW()
                    FROM public.users u
                    WHERE u.id = rl.user_id
                      AND u.id = public.resolve_user_id(%s)
                    RETURNING rl.daily_requests_used, rl.daily_request_limit
                    """,
                    (identity.telegram_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return
                current_count, max_requests = row
                if current_count > max_requests:
                    raise HTTPException(
                        status_code=429,
                        detail="Daily request limit exceeded. Try again later.",
                        headers={"Retry-After": "3600"},
                    )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Rate limit check failed (allowing request).",
            extra={"error": type(exc).__name__},
        )
