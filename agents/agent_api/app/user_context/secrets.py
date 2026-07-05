"""The single Vault secret building block.

Both the runtime resolver (which already holds an open cursor and the canonical
user id) and the health probe / admin tooling (which start from a Telegram id)
resolve provider secrets through this module — there is no other secret lookup in
the request path.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from agents.agent_api.app.config import settings
from agents.agent_api.app.credentials import IntegrationCredential

logger = logging.getLogger(__name__)


def resolve_secret_value(cursor: Any, user_id: str, provider: str) -> Optional[str]:
    """Decrypt an enabled connection's secret via the private Vault function.

    Cursor-based so the runtime resolver reuses its open connection. Returns None
    when no secret is available; raises only on an unexpected DB error, which the
    resolver's closure catches to classify the domain as credential_unavailable.
    """

    cursor.execute(
        "select private.resolve_integration_secret(%s, %s)",
        (user_id, provider),
    )
    row = cursor.fetchone()
    return row[0] if row and row[0] else None


def resolve_connection_secret(
    telegram_user_id: Optional[int],
    provider: str,
) -> Optional[IntegrationCredential]:
    """Resolve a full credential for a Telegram identity in a fresh connection.

    Used by out-of-request callers (health probe, admin tooling) that start from a
    Telegram id rather than a canonical user id. Returns None (never raises) on any
    failure so probes fail soft.
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
                    SELECT connection.id,
                           private.resolve_integration_secret(
                               connection.user_id,
                               connection.provider
                           )
                    FROM public.integration_connections connection
                    WHERE connection.user_id = public.resolve_user_id(%s)
                      AND connection.provider = %s
                      AND connection.status = 'connected'
                      AND connection.is_enabled
                    LIMIT 1
                    """,
                    (str(telegram_user_id), provider),
                )
                row = cur.fetchone()
                if not row or not row[1]:
                    return None
                return IntegrationCredential(
                    connection_id=str(row[0]),
                    provider=provider,
                    secret=row[1],
                )
    except Exception as exc:
        logger.warning(
            "Integration credential lookup failed.",
            extra={"provider": provider, "error": type(exc).__name__},
        )
        return None


__all__ = ["resolve_connection_secret", "resolve_secret_value"]
