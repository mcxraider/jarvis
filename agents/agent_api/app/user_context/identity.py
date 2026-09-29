"""Resolve and refresh the canonical user behind an inbound identity.

Identity logic lives here in one place: the security gate (only an active user with
a verified identity may run) and the authoritative read of the user's profile and
typed runtime policy. Both are cursor-based so the resolver performs them inside a
single connection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import ValidationError

from agents.agent_api.app.user_context.policy import (
    ResourceRestrictions,
    RuntimePolicy,
)
from agents.agent_api.app.user_context.runtime import (
    PolicyShadowMismatchError,
    RuntimeContextError,
)

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
    runtime_policy: RuntimePolicy
    resource_restrictions: ResourceRestrictions
    policy_revision: int
    custom_instructions: str


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
    """Read the active user's profile and typed policy in one query.

    Raises ``RuntimeContextError`` if the active user or required typed policy is
    missing, or if stored policy rows fail validation (fail closed).
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
               policy.policy_revision,
               policy.forced_model,
               policy.forced_reasoning_effort,
               policy.max_agent_turns,
               policy.allow_mutations,
               COALESCE(app_user.custom_instructions, ''),
               COALESCE((
                   SELECT jsonb_agg(
                       jsonb_build_object(
                           'id', restriction.resource_id,
                           'label', restriction.label,
                           'is_primary', restriction.is_primary
                       ) ORDER BY restriction.resource_id
                   )
                   FROM private.user_resource_restrictions restriction
                   WHERE restriction.user_id = app_user.id
                     AND restriction.provider = 'todoist'
               ), '[]'::jsonb),
               COALESCE((
                   SELECT jsonb_agg(
                       jsonb_build_object(
                           'id', restriction.resource_id,
                           'label', restriction.label,
                           'is_primary', restriction.is_primary
                       ) ORDER BY restriction.resource_id
                   )
                   FROM private.user_resource_restrictions restriction
                   WHERE restriction.user_id = app_user.id
                     AND restriction.provider = 'google_calendar'
               ), '[]'::jsonb),
               shadow.runtime_matches,
               shadow.access_matches
        FROM public.users app_user
        JOIN private.user_runtime_policies policy ON policy.user_id = app_user.id
        CROSS JOIN LATERAL private.runtime_policy_shadow_status(app_user.id) shadow
        WHERE app_user.telegram_id = %s
          AND app_user.telegram_verified_at IS NOT NULL
          AND app_user.status = 'active'
        """,
        (inbound_identity.telegram_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise RuntimeContextError(
            "No active user with a configured runtime policy was found."
        )

    (
        user_id,
        display_name,
        user_timezone,
        locale,
        policy_revision,
        forced_model,
        forced_reasoning_effort,
        max_agent_turns,
        allow_mutations,
        custom_instructions,
        todoist_restrictions,
        calendar_restrictions,
        runtime_shadow_matches,
        access_shadow_matches,
    ) = row
    if not runtime_shadow_matches or not access_shadow_matches:
        raise PolicyShadowMismatchError(
            runtime_matches=bool(runtime_shadow_matches),
            access_matches=bool(access_shadow_matches),
        )
    try:
        runtime_policy = RuntimePolicy.model_validate(
            {
                "forced_model": forced_model,
                "forced_reasoning_effort": forced_reasoning_effort,
                "max_agent_turns": max_agent_turns,
                "allow_mutations": allow_mutations,
            }
        )
        resource_restrictions = ResourceRestrictions.model_validate(
            {
                "restricted_todoist_projects": todoist_restrictions,
                "restricted_google_calendars": calendar_restrictions,
            }
        )
    except ValidationError as exc:
        raise RuntimeContextError("Stored user policy failed validation.") from exc
    return ResolvedIdentity(
        user_id=str(user_id),
        display_name=display_name,
        timezone=user_timezone,
        locale=locale,
        runtime_policy=runtime_policy,
        resource_restrictions=resource_restrictions,
        policy_revision=policy_revision,
        custom_instructions=custom_instructions,
    )


__all__ = [
    "ResolvedIdentity",
    "TelegramIdentity",
    "refresh_identity_profile",
    "resolve_active_identity",
    "telegram_identity",
]
