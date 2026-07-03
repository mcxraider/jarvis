"""Single-user Google Calendar OAuth: load, refresh, and persist a local token.

Unlike Todoist's static personal-access token, Calendar uses OAuth 2.0. For the
single-user setup we store the authorized-user credential as a local
``token.json`` (gitignored). That file — written once by
``scripts/connect_google_calendar.py`` — embeds ``client_id``,
``client_secret``, and the long-lived ``refresh_token``, so at runtime we never
need the app client-secret env var: we load the file, refresh the short-lived
access token when it expires, and write the refreshed token back.

The app-level client secret (``GOOGLE_OAUTH_CLIENT_SECRETS_JSON``) is only used
during the one-time consent flow in the connect script, never here.

Never log token contents — only ``service`` and error ``kind`` (CLAUDE.md).
"""

import logging
import os
from typing import Dict, List, Optional

from agents.agent_api.app.tools.errors import ClassifiedApiError

logger = logging.getLogger(__name__)

DEFAULT_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
GOOGLE_TOKEN_MAP_ENV = "GOOGLE_TOKEN_PATHS_BY_TELEGRAM_USER_ID"


class GoogleCalendarApiError(ClassifiedApiError):
    """Structured Google Calendar failure safe to route through tool results.

    Subclasses the shared :class:`ClassifiedApiError` so the dispatcher catches
    it via the base. ``reconnect`` marks auth failures the user must fix by
    re-running the connect script; such errors must not be retried.
    """

    source = "google_calendar"

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        retryable: bool = False,
        status_code=None,
        attempts: int = 1,
        reconnect: bool = False,
        operation: str = "calendar.request",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.attempts = attempts
        self.reconnect = reconnect
        self.operation = operation

    def __str__(self) -> str:
        return self.message

    def to_classifier_payload(self):
        payload = super().to_classifier_payload()
        payload["operation"] = self.operation
        if self.reconnect:
            payload["reconnect"] = True
        return payload


def get_calendar_scopes() -> List[str]:
    """Space-separated OAuth scopes from env, defaulting to full calendar access."""

    raw = os.getenv("GOOGLE_CALENDAR_SCOPES", DEFAULT_CALENDAR_SCOPE)
    return raw.split()


def get_token_path() -> str:
    """Filesystem path to the local authorized-user token JSON."""

    return os.getenv("GOOGLE_TOKEN_PATH", "token.json")


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean env toggle. Unset/blank → default; only explicit off-values
    ("0", "false", "no", "off", case-insensitive) turn it off — anything else is on."""

    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def is_calendar_enabled() -> bool:
    """The explicit on/off config toggle for the Calendar domain (default on).

    Independent of whether a token exists — flip ``GOOGLE_CALENDAR_ENABLED=false``
    to disable calendar while keeping ``token.json`` in place.
    """

    return _env_flag("GOOGLE_CALENDAR_ENABLED", True)


def is_calendar_configured() -> bool:
    """Whether the Calendar domain should run this session.

    Two independent gates, both required:
    1. ``is_calendar_enabled()`` — the ``GOOGLE_CALENDAR_ENABLED`` config toggle.
    2. A local token file exists (it embeds client id/secret + refresh token, so
       it is necessary and sufficient *at runtime*).

    The builder calls this to decide whether to register calendar tools, so a run
    with the toggle off — or no token — never sees tools that would only fail.
    """

    return is_calendar_enabled() and os.path.exists(get_token_path())


def _parse_google_token_map(raw_value: Optional[str]) -> Dict[str, str]:
    """Parse the per-user token-path map from the environment variable.

    Format: ``telegram_user_id:/path/to/token.json,...``
    """

    if not raw_value:
        return {}
    mapping: Dict[str, str] = {}
    for entry in raw_value.split(","):
        cleaned = entry.strip()
        if not cleaned:
            continue
        telegram_user_id, separator, path = cleaned.partition(":")
        if not separator or not telegram_user_id.strip() or not path.strip():
            raise ValueError(
                f"{GOOGLE_TOKEN_MAP_ENV} entries must use telegram_user_id:token_path"
            )
        mapping[telegram_user_id.strip()] = path.strip()
    return mapping


def get_token_path_for_user(telegram_user_id: Optional[int] = None) -> str:
    """Resolve the token path for the given Telegram user.

    Checks the per-user map first (``GOOGLE_TOKEN_PATHS_BY_TELEGRAM_USER_ID``);
    falls back to the global ``get_token_path()``.
    """

    if telegram_user_id is not None:
        token_map = _parse_google_token_map(os.getenv(GOOGLE_TOKEN_MAP_ENV))
        path = token_map.get(str(telegram_user_id))
        if path:
            return path
    return get_token_path()


def is_calendar_configured_for_user(telegram_user_id: Optional[int] = None) -> bool:
    """Whether the Calendar domain should run for the given user.

    Same two-gate check as :func:`is_calendar_configured`, but resolves the token
    path via the per-user map when a ``telegram_user_id`` is provided.
    """

    return is_calendar_enabled() and os.path.exists(get_token_path_for_user(telegram_user_id))


def _write_token(token_path: str, creds) -> None:
    """Persist the (possibly refreshed) credential back to disk as JSON."""

    with open(token_path, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())


def load_credentials(token_path: Optional[str] = None):
    """Load local OAuth credentials, refreshing + persisting the token if expired.

    Raises :class:`GoogleCalendarApiError` (kind="auth", reconnect=True) when the
    token is missing or cannot be refreshed, so the agent tells the user to
    reconnect rather than surfacing a raw crash.
    """

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = token_path or get_token_path()
    scopes = get_calendar_scopes()

    if not os.path.exists(token_path):
        raise GoogleCalendarApiError(
            kind="auth",
            message=(
                "Google Calendar is not connected. Run "
                "scripts/connect_google_calendar.py to authorize."
            ),
            reconnect=True,
            operation="calendar.auth",
        )

    creds = Credentials.from_authorized_user_file(token_path, scopes)

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # google.auth.exceptions.RefreshError, etc.
            logger.warning(
                "calendar.auth.refresh_failed",
                extra={"service": "google_calendar", "error": type(exc).__name__},
            )
            raise GoogleCalendarApiError(
                kind="auth",
                message=(
                    "Google Calendar access expired and could not be refreshed. "
                    "Reconnect with scripts/connect_google_calendar.py."
                ),
                reconnect=True,
                operation="calendar.auth",
            ) from exc
        _write_token(token_path, creds)
        return creds

    raise GoogleCalendarApiError(
        kind="auth",
        message=(
            "Google Calendar credentials are invalid. Reconnect with "
            "scripts/connect_google_calendar.py."
        ),
        reconnect=True,
        operation="calendar.auth",
    )


def build_calendar_service(token_path: Optional[str] = None):
    """Build a Google Calendar v3 discovery service from local credentials."""

    from googleapiclient.discovery import build

    return build(
        "calendar",
        "v3",
        credentials=load_credentials(token_path),
        cache_discovery=False,
    )


__all__ = [
    "DEFAULT_CALENDAR_SCOPE",
    "GOOGLE_TOKEN_MAP_ENV",
    "GoogleCalendarApiError",
    "build_calendar_service",
    "get_calendar_scopes",
    "get_token_path",
    "get_token_path_for_user",
    "is_calendar_configured",
    "is_calendar_configured_for_user",
    "is_calendar_enabled",
    "load_credentials",
]
