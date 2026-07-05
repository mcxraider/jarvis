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
import json
from datetime import datetime
from typing import Callable, List, Optional

from agents.agent_api.app.tools.errors import ClassifiedApiError

logger = logging.getLogger(__name__)

CredentialPersistCallback = Callable[[str, Optional[datetime]], None]

DEFAULT_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


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


def _write_token(token_path: str, creds) -> None:
    """Persist the (possibly refreshed) credential back to disk as JSON."""

    with open(token_path, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())


def load_credentials(
    token_path: Optional[str] = None,
    *,
    credential_json: Optional[str] = None,
    persist_callback: Optional[CredentialPersistCallback] = None,
):
    """Load OAuth credentials from Vault JSON or a legacy local token file.

    Raises :class:`GoogleCalendarApiError` (kind="auth", reconnect=True) when the
    token is missing or cannot be refreshed, so the agent tells the user to
    reconnect rather than surfacing a raw crash.
    """

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    scopes = get_calendar_scopes()
    if credential_json is not None:
        try:
            credential_info = json.loads(credential_json)
            creds = Credentials.from_authorized_user_info(credential_info, scopes)
        except (ValueError, TypeError) as exc:
            raise GoogleCalendarApiError(
                kind="auth",
                message="Google Calendar credentials are invalid. Reconnect the account.",
                reconnect=True,
                operation="calendar.auth",
            ) from exc
    else:
        token_path = token_path or get_token_path()
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
        if credential_json is not None:
            if persist_callback is None:
                raise GoogleCalendarApiError(
                    kind="auth",
                    message="Google Calendar credential refresh could not be persisted.",
                    reconnect=True,
                    operation="calendar.auth",
                )
            persist_callback(creds.to_json(), creds.expiry)
        else:
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


def build_calendar_service(
    token_path: Optional[str] = None,
    *,
    credential_json: Optional[str] = None,
    persist_callback: Optional[CredentialPersistCallback] = None,
):
    """Build a Google Calendar v3 discovery service from local credentials."""

    from googleapiclient.discovery import build

    return build(
        "calendar",
        "v3",
        credentials=load_credentials(
            token_path,
            credential_json=credential_json,
            persist_callback=persist_callback,
        ),
        cache_discovery=False,
    )


__all__ = [
    "DEFAULT_CALENDAR_SCOPE",
    "GoogleCalendarApiError",
    "build_calendar_service",
    "get_calendar_scopes",
    "get_token_path",
    "load_credentials",
]
