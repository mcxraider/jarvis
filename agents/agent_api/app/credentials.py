"""Integration credential value object and secret rotation.

Runtime secret resolution lives in :mod:`agents.agent_api.app.user_context.secrets`
and the unified resolver — there is no per-lookup credential/preference API here.
This module holds the shared :class:`IntegrationCredential` value object and the
one write path (rotation via the private Vault function).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class IntegrationCredential:
    connection_id: str
    provider: str
    secret: str


def update_integration_credential(
    connection_id: str,
    secret: str,
    token_expires_at: Optional[datetime] = None,
) -> None:
    """Persist a rotated/refreshed secret through the private Vault function."""

    from agents.agent_api.app.db import get_pool

    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT private.update_integration_secret(%s, %s, %s)",
                (connection_id, secret, token_expires_at),
            )


__all__ = ["IntegrationCredential", "update_integration_credential"]
