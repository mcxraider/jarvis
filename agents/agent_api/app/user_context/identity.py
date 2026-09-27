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
class TelegramIdentity:
    """Telegram account supplied by the inbound request surface."""

    telegram_id: int
    username: Optional[str] = None

    def __post_init__(self) -> None:
        if self.telegram_id <= 0:
            raise ValueError("telegram_id must be a positive integer")


def telegram_identity(
    telegram_user_id: int,
    telegram_username: Optional[str] = None,
    telegram_first_name: Optional[str] = None,
) -> TelegramIdentity:
    """Build the canonical identity used by legacy Telegram callers."""

    del telegram_first_name
    return TelegramIdentity(
        telegram_id=telegram_user_id,
        username=telegram_username,
    )


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
    inbound_identity: TelegramIdentity,
) -> str:
    """Gate the request and refresh non-authoritative Telegram profile data.

    Runtime requests never auto-provision users; onboarding is administrative. This
    both enforces the gate (raises ``PermissionError`` when no active user owns a
    verified Telegram identity) and bumps ``last_seen_at`` / profile fields. Returns
    the canonical ``user_id``.
    """

    cursor.execute(
        """
        UPDATE public.users AS app_user
        SET telegram_username = COALESCE(%s::text, app_user.telegram_username),
            telegram_last_seen_at = NOW()
        WHERE app_user.telegram_id = %s
          AND app_user.telegram_verified_at IS NOT NULL
          AND app_user.status = 'active'
        RETURNING app_user.id
        """,
        (
            inbound_identity.username,
            inbound_identity.telegram_id,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise PermissionError(
            "No active Jarvis user is registered for this Telegram identity."
        )
    return str(row[0])


def resolve_active_identity(
    cursor: Any, inbound_identity: TelegramIdentity
) -> ResolvedIdentity:
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
                   app_user.telegram_username,
                   'the user'
               ),
               app_user.timezone,
               app_user.locale,
               app_user.preference_schema_version,
               app_user.preference_revision,
               app_user.preferences
        FROM public.users app_user
        WHERE app_user.telegram_id = %s
          AND app_user.telegram_verified_at IS NOT NULL
          AND app_user.status = 'active'
        """,
        (inbound_identity.telegram_id,),
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


__all__ = [
    "ResolvedIdentity",
    "TelegramIdentity",
    "refresh_identity_profile",
    "resolve_active_identity",
    "telegram_identity",
]
