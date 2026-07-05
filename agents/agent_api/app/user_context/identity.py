"""Resolve and refresh the canonical user behind an inbound identity.

Identity logic lives here in one place: the security gate (only an active user with
a verified identity may run) and the authoritative read of the user's profile and
validated preferences. Both are cursor-based so the resolver performs them inside a
single connection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from agents.agent_api.app.user_context.preferences import ResolvedUserPreferences
from agents.agent_api.app.user_context.runtime import RuntimeContextError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedIdentity:
    """The canonical user plus the data the snapshot needs (no secrets)."""

    user_id: str
    display_name: str
    timezone: str
    locale: str
    preferences: ResolvedUserPreferences


def refresh_identity_profile(
    cursor: Any,
    telegram_user_id: int,
    telegram_username: Optional[str] = None,
    telegram_first_name: Optional[str] = None,
) -> str:
    """Gate the request and refresh non-authoritative Telegram profile data.

    Runtime requests never auto-provision users; onboarding is administrative. This
    both enforces the gate (raises ``PermissionError`` when no active user owns a
    verified Telegram identity) and bumps ``last_seen_at`` / profile fields. Returns
    the canonical ``user_id``.
    """

    cursor.execute(
        """
        UPDATE public.user_identities AS identity
        SET username = COALESCE(%s::text, identity.username),
            display_name = COALESCE(%s::text, identity.display_name),
            metadata = jsonb_strip_nulls(
                identity.metadata ||
                jsonb_build_object('telegram_first_name', %s::text)
            ),
            last_seen_at = NOW()
        FROM public.users AS app_user
        WHERE identity.user_id = app_user.id
          AND identity.identity_provider = 'telegram'
          AND identity.external_subject = %s::text
          AND identity.verified_at IS NOT NULL
          AND app_user.status = 'active'
        RETURNING app_user.id
        """,
        (
            telegram_username,
            telegram_first_name,
            telegram_first_name,
            str(telegram_user_id),
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise PermissionError(
            "No active Jarvis user is registered for this Telegram identity."
        )
    return str(row[0])


def resolve_active_identity(cursor: Any, telegram_user_id: int) -> ResolvedIdentity:
    """Read the active user's profile and validated preferences in one query.

    Raises ``RuntimeContextError`` if no active user with configured preferences is
    found, and ``PreferenceConfigurationError`` if the stored preferences fail
    versioned validation (fail closed).
    """

    cursor.execute(
        """
        SELECT app_user.id,
               COALESCE(
                   app_user.display_name,
                   identity.display_name,
                   identity.username,
                   'the user'
               ),
               app_user.timezone,
               app_user.locale,
               preferences.schema_version,
               preferences.revision,
               preferences.preferences
        FROM public.user_identities identity
        JOIN public.users app_user
          ON app_user.id = identity.user_id
        JOIN public.user_preferences preferences
          ON preferences.user_id = app_user.id
        WHERE identity.identity_provider = 'telegram'
          AND identity.external_subject = %s
          AND identity.verified_at IS NOT NULL
          AND app_user.status = 'active'
        """,
        (str(telegram_user_id),),
    )
    row = cursor.fetchone()
    if not row:
        raise RuntimeContextError(
            "No active user with configured preferences was found."
        )

    user_id, display_name, user_timezone, locale, schema_version, revision, raw_prefs = row
    resolved_preferences = ResolvedUserPreferences.from_database_row(
        (user_id, schema_version, revision, raw_prefs)
    )
    return ResolvedIdentity(
        user_id=str(user_id),
        display_name=display_name,
        timezone=user_timezone,
        locale=locale,
        preferences=resolved_preferences,
    )


__all__ = ["ResolvedIdentity", "refresh_identity_profile", "resolve_active_identity"]
