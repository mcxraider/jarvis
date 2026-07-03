"""One-time Google Calendar OAuth consent for the single-user setup.

Run this once to authorize Jarvis against your Google account. It opens a
browser for consent, then writes the authorized-user credential to the local
token file that ``tools/calendar/auth.py`` loads at runtime. After it succeeds,
``is_calendar_configured()`` returns ``True`` and the agent exposes calendar
tools.

    python -m scripts.connect_google_calendar          # from the repo root
    python scripts/connect_google_calendar.py          # equivalent

``GOOGLE_OAUTH_CLIENT_SECRETS_JSON`` must hold the OAuth *client* config from the
Google Cloud Console — either the raw JSON, or a path to the downloaded file.
That client secret is used ONLY here (to run the consent flow); at runtime the
written token embeds everything the agent needs, so the secret env is not read
again. Never commit the token or the client secret — ``.gitignore`` covers both.

The module is split into small, individually testable functions
(``load_client_config`` / ``save_credentials`` / ``connect``); only
``run_consent_flow`` touches the network + browser, and ``connect`` accepts an
injected runner so everything around it can be tested without either.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional

# Ensure the repo root is importable when the script is run directly
# (``python scripts/connect_google_calendar.py`` puts scripts/ on sys.path[0],
# not the repo root). Running via ``python -m scripts...`` doesn't need this,
# but supporting both keeps the documented usage honest.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agents.agent_api.app.tools.calendar.auth import (  # noqa: E402
    get_calendar_scopes,
    get_token_path,
)

CLIENT_SECRETS_ENV = "GOOGLE_OAUTH_CLIENT_SECRETS_JSON"


class ConnectError(RuntimeError):
    """A user-actionable failure in the connect flow (bad config, missing env)."""


def load_client_config(raw: Optional[str] = None) -> Dict[str, Any]:
    """Resolve the OAuth client config from a raw JSON string or a file path.

    ``raw`` defaults to ``$GOOGLE_OAUTH_CLIENT_SECRETS_JSON``. The value may be
    either the JSON text itself or a path to the JSON file downloaded from the
    Google Cloud Console — supporting both because the console hands you a file,
    but deployments often prefer inlining it into an env var.

    Raises :class:`ConnectError` with an actionable message when the value is
    absent, unreadable, not valid JSON, or not a recognizable client config.
    """

    if raw is None:
        raw = os.getenv(CLIENT_SECRETS_ENV)
    if not raw or not raw.strip():
        raise ConnectError(
            f"{CLIENT_SECRETS_ENV} is not set. Put the OAuth client JSON from the "
            "Google Cloud Console in that env var (raw JSON or a path to the file)."
        )

    raw = raw.strip()
    source = "env value"
    if os.path.exists(raw):
        source = raw
        try:
            with open(raw, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as exc:
            raise ConnectError(f"Could not read client secrets file {source}: {exc}") from exc

    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConnectError(
            f"Client secrets ({source}) is not valid JSON: {exc}."
        ) from exc

    if not isinstance(config, dict) or not ({"installed", "web"} & set(config)):
        raise ConnectError(
            "Client secrets JSON must be a Google OAuth client config with an "
            "'installed' or 'web' key. Download it from APIs & Services > "
            "Credentials in the Google Cloud Console."
        )
    return config


def run_consent_flow(client_config: Dict[str, Any], scopes: List[str]) -> Any:
    """Run the interactive installed-app consent flow, returning Credentials.

    This is the only function that opens a browser and talks to Google, so it is
    kept thin and is injected into :func:`connect` for tests. ``port=0`` lets the
    OS pick a free loopback port for the redirect.
    """

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(client_config, scopes)
    return flow.run_local_server(port=0)


def save_credentials(creds: Any, token_path: str) -> None:
    """Write the authorized-user credential to ``token_path`` as JSON."""

    with open(token_path, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())


def connect(
    *,
    client_secrets: Optional[str] = None,
    token_path: Optional[str] = None,
    scopes: Optional[List[str]] = None,
    flow_runner: Optional[Callable[[Dict[str, Any], List[str]], Any]] = None,
) -> str:
    """Run consent and persist the token. Returns the path written.

    Defaults for ``token_path`` and ``scopes`` come from ``calendar.auth`` so the
    connect flow and the runtime loader can never drift apart. ``flow_runner`` is
    injectable so the orchestration is testable without a browser; it defaults to
    :func:`run_consent_flow` resolved *at call time* (late binding), so patching
    the module attribute in tests takes effect.
    """

    config = load_client_config(client_secrets)
    resolved_scopes = scopes if scopes is not None else get_calendar_scopes()
    resolved_token_path = token_path or get_token_path()
    runner = flow_runner if flow_runner is not None else run_consent_flow

    creds = runner(config, resolved_scopes)
    save_credentials(creds, resolved_token_path)
    return resolved_token_path


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="connect_google_calendar",
        description="Authorize Jarvis against your Google Calendar (writes a local token).",
    )
    parser.add_argument(
        "--client-secrets",
        default=None,
        help=(
            "OAuth client JSON or path to it. Overrides "
            f"${CLIENT_SECRETS_ENV}."
        ),
    )
    parser.add_argument(
        "--token-path",
        default=None,
        help="Where to write the token JSON. Defaults to $GOOGLE_TOKEN_PATH or token.json.",
    )
    parser.add_argument(
        "--scopes",
        default=None,
        help="Space-separated OAuth scopes. Defaults to $GOOGLE_CALENDAR_SCOPES.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns a process exit code (0 = success, 1 = error)."""

    args = _parse_args(argv)
    scopes = args.scopes.split() if args.scopes else None
    try:
        written = connect(
            client_secrets=args.client_secrets,
            token_path=args.token_path,
            scopes=scopes,
        )
    except ConnectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Google Calendar connected. Token written to {written}.")
    print("Restart the agent; calendar tools are now available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
