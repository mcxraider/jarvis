"""Resolve identity, preferences, connections, and thread snapshots once per run.

Orchestration only. The seven steps the master plan describes are delegated to
focused modules: ``identity`` (gate + profile/preferences), ``secrets`` (Vault),
``domains`` (pure availability classification), and ``domain_adapters`` (the
registered capabilities). A single database connection performs the reads so no
credential, preference, or user lookup happens independently later in the request.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from agents.agent_api.app.credentials import IntegrationCredential
from agents.agent_api.app.db import get_pool
from agents.agent_api.app.tools.domain_adapters import DOMAIN_ADAPTERS
from agents.agent_api.app.user_context.domains import ConnectionRow, classify_domains
from agents.agent_api.app.user_context.identity import (
    TelegramIdentity,
    refresh_identity_profile,
    resolve_active_identity,
)
from agents.agent_api.app.user_context.runtime import (
    ResolvedRuntimeContext,
    RuntimeContextError,
    RuntimeContextSnapshot,
)
from agents.agent_api.app.user_context.secrets import resolve_secret_value


def _load_connections(cursor: Any, user_id: str) -> Dict[str, ConnectionRow]:
    """Read this user's integration connections keyed by provider."""

    cursor.execute(
        """
        SELECT id, provider, status, is_enabled
        FROM public.integration_connections
        WHERE user_id = %s
        ORDER BY provider
        """,
        (user_id,),
    )
    return {
        row[1]: ConnectionRow(
            connection_id=str(row[0]),
            provider=row[1],
            status=row[2],
            is_enabled=row[3],
        )
        for row in cursor.fetchall()
    }


def _make_secret_resolver(cursor: Any, user_id: str):
    """A provider -> secret closure that fails soft to credential_unavailable."""

    def resolver(provider: str) -> Optional[str]:
        try:
            return resolve_secret_value(cursor, user_id, provider)
        except Exception:
            return None

    return resolver


def resolve_runtime_context(
    inbound_identity: TelegramIdentity,
) -> ResolvedRuntimeContext:
    """Build a fresh context from canonical user state in one DB connection."""

    pool = get_pool()
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            # Gate + refresh, then authoritative read — same connection.
            refresh_identity_profile(cursor, inbound_identity)
            identity = resolve_active_identity(cursor, inbound_identity)
            connections = _load_connections(cursor, identity.user_id)
            domains, credentials = classify_domains(
                DOMAIN_ADAPTERS,
                connections,
                _make_secret_resolver(cursor, identity.user_id),
            )

            snapshot = RuntimeContextSnapshot(
                user_id=identity.user_id,
                display_name=identity.display_name,
                timezone=identity.timezone,
                locale=identity.locale,
                preference_schema_version=identity.preferences.schema_version,
                preference_revision=identity.preferences.revision,
                preferences=identity.preferences.preferences,
                domains=domains,
                resolved_at=datetime.now(timezone.utc),
            )
            return ResolvedRuntimeContext(snapshot=snapshot, credentials=credentials)


def store_thread_context(
    thread_id: str,
    user_prompt: str,
    snapshot: RuntimeContextSnapshot,
) -> None:
    """Persist the secret-free snapshot before graph execution."""

    pool = get_pool()
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.threads (
                    thread_id,
                    user_id,
                    title,
                    status,
                    message_count,
                    context_snapshot,
                    context_schema_version,
                    preference_revision,
                    context_resolved_at
                )
                VALUES (
                    %s, %s, LEFT(%s, 100), 'active', 0, %s, %s, %s, %s
                )
                ON CONFLICT (thread_id) DO UPDATE
                SET context_snapshot = excluded.context_snapshot,
                    context_schema_version = excluded.context_schema_version,
                    preference_revision = excluded.preference_revision,
                    context_resolved_at = excluded.context_resolved_at,
                    updated_at = now()
                """,
                (
                    thread_id,
                    snapshot.user_id,
                    user_prompt,
                    snapshot.model_dump_json(),
                    snapshot.schema_version,
                    snapshot.preference_revision,
                    snapshot.resolved_at,
                ),
            )


def load_thread_runtime_context(
    thread_id: str,
    inbound_identity: TelegramIdentity,
) -> ResolvedRuntimeContext:
    """Reuse an interrupted thread snapshot and rehydrate only its secrets.

    Fail closed: a missing/incompatible snapshot, or a saved capability that is no
    longer connected/enabled, aborts the resume rather than silently degrading.
    """

    pool = get_pool()
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            # Same active-user gate as a fresh resolution.
            refresh_identity_profile(cursor, inbound_identity)
            cursor.execute(
                """
                SELECT thread.context_snapshot
                FROM public.threads thread
                WHERE thread.thread_id = %s
                  AND thread.user_id = public.resolve_user_id(%s)
                """,
                (thread_id, inbound_identity.telegram_id),
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                raise RuntimeContextError(
                    "The interrupted thread has no reusable runtime snapshot."
                )

            snapshot = RuntimeContextSnapshot.model_validate(row[0])
            credentials: Dict[str, IntegrationCredential] = {}
            for domain in snapshot.domains:
                if domain.status != "active":
                    continue
                if not domain.connection_id:
                    raise RuntimeContextError("Active snapshot domain has no connection id.")
                cursor.execute(
                    """
                    SELECT user_id, provider, status, is_enabled
                    FROM public.integration_connections
                    WHERE id = %s
                    """,
                    (domain.connection_id,),
                )
                connection_row = cursor.fetchone()
                if (
                    not connection_row
                    or str(connection_row[0]) != snapshot.user_id
                    or connection_row[1] != domain.provider
                    or connection_row[2] != "connected"
                    or not connection_row[3]
                ):
                    raise RuntimeContextError(
                        f"Saved {domain.provider} capability is no longer available."
                    )
                secret = resolve_secret_value(cursor, snapshot.user_id, domain.provider)
                if not secret:
                    raise RuntimeContextError(
                        f"Saved {domain.provider} credential cannot be resolved."
                    )
                credentials[domain.provider] = IntegrationCredential(
                    connection_id=domain.connection_id,
                    provider=domain.provider,
                    secret=secret,
                )

            return ResolvedRuntimeContext(snapshot=snapshot, credentials=credentials)


__all__ = [
    "load_thread_runtime_context",
    "resolve_runtime_context",
    "store_thread_context",
]
