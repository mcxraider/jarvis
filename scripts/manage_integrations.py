"""Secret-safe administrative CLI for Jarvis onboarding and integrations."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agents.agent_api.app.tools.domain_adapters import DOMAIN_ADAPTERS  # noqa: E402
from agents.agent_api.app.tools.google_calendar.client import (  # noqa: E402
    GoogleCalendarClient,
)
from agents.agent_api.app.tools.todoist.client import TodoistApiClient  # noqa: E402
from agents.agent_api.app.user_context.preferences import (  # noqa: E402
    AssistantPreferencesV1,
)

SUPPORTED_PROVIDERS = tuple(DOMAIN_ADAPTERS)
DEFAULT_ACTOR = "admin:cli"


class IntegrationAdminError(RuntimeError):
    """A sanitized, user-actionable administration error."""


def _require_dsn() -> str:
    dsn = os.getenv("JARVIS_ADMIN_POSTGRES_DSN")
    if not dsn:
        raise IntegrationAdminError(
            "JARVIS_ADMIN_POSTGRES_DSN is required for administration."
        )
    return dsn


def _read_input(path: Path, stdin: TextIO, *, description: str) -> str:
    try:
        value = stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IntegrationAdminError(f"{description} file could not be read.") from exc
    if not value.strip():
        raise IntegrationAdminError(f"{description} input is empty.")
    return value


def _load_preferences(path: Path, stdin: TextIO) -> Dict[str, Any]:
    raw = _read_input(path, stdin, description="Preference")
    try:
        payload = json.loads(raw)
        validated = AssistantPreferencesV1.model_validate(payload)
    except (ValueError, TypeError) as exc:
        raise IntegrationAdminError(
            "Preferences do not match the supported schema."
        ) from exc
    return validated.model_dump(mode="json", exclude_unset=True)


def _validate_todoist(secret: str) -> Dict[str, Any]:
    token = secret.strip()
    if not token:
        raise IntegrationAdminError("Todoist credential is empty.")
    try:
        request = urllib.request.Request(
            "https://api.todoist.com/api/v1/tasks?limit=1",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read(1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise IntegrationAdminError(
            "Todoist rejected the credential; no changes were made."
        ) from exc
    return {
        "secret": token,
        "account_label": None,
        "external_account_id": None,
        "scopes": [],
        "settings": {"credential_source": "supabase_vault"},
        "token_expires_at": None,
    }


def _validate_google_calendar(secret: str) -> Dict[str, Any]:
    try:
        payload = json.loads(secret)
    except (TypeError, ValueError) as exc:
        raise IntegrationAdminError(
            "Google Calendar credential is not valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise IntegrationAdminError("Google Calendar credential must be a JSON object.")

    required = {"client_id", "client_secret", "refresh_token", "token_uri"}
    missing = sorted(required - payload.keys())
    if missing:
        raise IntegrationAdminError(
            f"Google Calendar credential is missing: {', '.join(missing)}."
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    try:
        credentials = Credentials.from_authorized_user_info(payload)
        if not credentials.valid:
            credentials.refresh(Request())
        service = build(
            "calendar", "v3", credentials=credentials, cache_discovery=False
        )
        response = service.calendarList().list(maxResults=100).execute()
    except Exception as exc:
        raise IntegrationAdminError(
            "Google Calendar rejected the credential; no changes were made."
        ) from exc

    calendars = response.get("items", []) if isinstance(response, dict) else []
    primary = next(
        (item for item in calendars if isinstance(item, dict) and item.get("primary")),
        None,
    )
    return {
        "secret": credentials.to_json(),
        "account_label": primary.get("summary") if primary else None,
        "external_account_id": primary.get("id") if primary else None,
        "scopes": list(credentials.scopes or []),
        "settings": {
            "calendar_count": len(calendars),
            "credential_source": "supabase_vault",
        },
        "token_expires_at": credentials.expiry,
    }


def validate_provider_credential(provider: str, secret: str) -> Dict[str, Any]:
    """Validate a credential live and return only storage metadata."""

    if provider == "todoist":
        return _validate_todoist(secret)
    if provider == "google_calendar":
        return _validate_google_calendar(secret)
    raise IntegrationAdminError("Unsupported provider.")


def _connect():
    try:
        import psycopg

        return psycopg.connect(_require_dsn())
    except IntegrationAdminError:
        raise
    except Exception as exc:
        raise IntegrationAdminError("Could not connect to the admin database.") from exc


def _execute_one(statement: str, params: Iterable[Any]) -> tuple:
    try:
        with _connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, tuple(params))
                row = cursor.fetchone()
                if row is None:
                    raise IntegrationAdminError("Administrative operation returned no result.")
                return row
    except IntegrationAdminError:
        raise
    except Exception as exc:
        raise IntegrationAdminError("Administrative database operation failed.") from exc


def _execute_all(statement: str, params: Iterable[Any] = ()) -> list[tuple]:
    try:
        with _connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement, tuple(params))
                return list(cursor.fetchall())
    except IntegrationAdminError:
        raise
    except Exception as exc:
        raise IntegrationAdminError("Administrative database operation failed.") from exc


def create_user(args: argparse.Namespace) -> Dict[str, Any]:
    try:
        ZoneInfo(args.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise IntegrationAdminError("Timezone must be a valid IANA name.") from exc
    user_id, created = _execute_one(
        """
        select user_id, created
        from private.admin_upsert_user(%s, %s, %s, %s, %s, %s)
        """,
        (
            args.telegram_user_id,
            args.username,
            args.display_name,
            args.timezone,
            args.locale,
            args.actor,
        ),
    )
    return {"user_id": str(user_id), "created": bool(created)}


def disable_user(args: argparse.Namespace) -> Dict[str, Any]:
    (user_id,) = _execute_one(
        "select private.admin_disable_user(%s, %s)",
        (args.telegram_user_id, args.actor),
    )
    return {"user_id": str(user_id), "status": "suspended"}


def attach_telegram_identity(args: argparse.Namespace) -> Dict[str, Any]:
    (user_id,) = _execute_one(
        "select private.admin_attach_telegram_identity(%s, %s, %s, %s, %s, %s)",
        (
            args.owner_telegram_user_id,
            args.telegram_user_id,
            args.username,
            args.display_name,
            args.primary,
            args.actor,
        ),
    )
    return {
        "user_id": str(user_id),
        "telegram_user_id": str(args.telegram_user_id),
        "primary": args.primary,
    }


def set_preferences(args: argparse.Namespace, stdin: TextIO) -> Dict[str, Any]:
    preferences = _load_preferences(args.file, stdin)
    _validate_restricted_resources(args.telegram_user_id, preferences)
    user_id, revision = _execute_one(
        "select user_id, revision from private.admin_set_preferences(%s, 1, %s::jsonb, %s)",
        (args.telegram_user_id, json.dumps(preferences), args.actor),
    )
    return {"user_id": str(user_id), "schema_version": 1, "revision": revision}


def _stored_credential(telegram_user_id: int, provider: str) -> str:
    (secret,) = _execute_one(
        "select private.admin_get_integration_secret(%s, %s)",
        (telegram_user_id, provider),
    )
    if not secret:
        raise IntegrationAdminError(
            f"{provider} must be connected before restricted resources are configured."
        )
    return secret


def _provider_resources(telegram_user_id: int, provider: str) -> list[Dict[str, Any]]:
    secret = _stored_credential(telegram_user_id, provider)
    try:
        if provider == "todoist":
            client = TodoistApiClient(api_key=secret)
            resources: list[Dict[str, Any]] = []
            cursor = None
            while True:
                payload = client.get_projects({"limit": 200, "cursor": cursor})
                if isinstance(payload, list):
                    rows, cursor = payload, None
                else:
                    rows = payload.get("results", payload.get("items", []))
                    cursor = payload.get("next_cursor")
                resources.extend(
                    {
                        "id": str(row["id"]),
                        "label": row.get("name") or str(row["id"]),
                        "is_primary": False,
                    }
                    for row in rows
                    if isinstance(row, dict) and row.get("id") is not None
                )
                if not cursor:
                    return resources

        if provider == "google_calendar":
            client = GoogleCalendarClient(credential_json=secret)
            resources = []
            page_token = None
            while True:
                payload = client.list_calendars(
                    {"max_results": 250, "page_token": page_token}
                )
                resources.extend(
                    {
                        "id": str(row["calendar_id"]),
                        "label": row.get("summary") or str(row["calendar_id"]),
                        "is_primary": bool(row.get("primary")),
                    }
                    for row in payload.get("calendars", [])
                    if isinstance(row, dict) and row.get("calendar_id") is not None
                )
                page_token = payload.get("next_page_token")
                if not page_token:
                    return resources
    except IntegrationAdminError:
        raise
    except Exception as exc:
        raise IntegrationAdminError(
            f"Could not discover {provider} resources."
        ) from exc
    raise IntegrationAdminError("Unsupported provider.")


def list_resources(args: argparse.Namespace) -> Dict[str, Any]:
    resources = _provider_resources(args.telegram_user_id, args.provider)
    return {"provider": args.provider, "resources": resources}


def _validate_restricted_resources(
    telegram_user_id: int,
    preferences: Dict[str, Any],
) -> None:
    access = preferences.get("access") or {}
    configured = {
        "todoist": access.get("restricted_todoist_projects") or [],
        "google_calendar": access.get("restricted_google_calendars") or [],
    }
    for provider, restrictions in configured.items():
        if not restrictions:
            continue
        discovered = {
            resource["id"]: resource
            for resource in _provider_resources(telegram_user_id, provider)
        }
        for restriction in restrictions:
            resource = discovered.get(str(restriction["id"]))
            if resource is None:
                raise IntegrationAdminError(
                    f"A restricted {provider} resource could not be resolved."
                )
            if bool(restriction.get("is_primary")) != bool(resource["is_primary"]):
                raise IntegrationAdminError(
                    f"A restricted {provider} resource has incorrect primary metadata."
                )


def store_credential(
    args: argparse.Namespace,
    stdin: TextIO,
) -> Dict[str, Any]:
    raw_secret = _read_input(args.secret_file, stdin, description="Credential")
    validated = validate_provider_credential(args.provider, raw_secret)
    account_label = args.account_label or validated["account_label"]
    connection_id, version = _execute_one(
        """
        select connection_id, credential_version
        from private.admin_store_integration(
          %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s
        )
        """,
        (
            args.telegram_user_id,
            args.provider,
            validated["secret"],
            account_label,
            validated["external_account_id"],
            validated["scopes"],
            json.dumps(validated["settings"]),
            validated["token_expires_at"],
            args.credential_action,
            args.actor,
        ),
    )
    return {
        "connection_id": str(connection_id),
        "provider": args.provider,
        "status": "connected",
        "enabled": True,
        "credential_version": version,
    }


def validate_stored_credential(args: argparse.Namespace) -> Dict[str, Any]:
    (secret,) = _execute_one(
        "select private.admin_get_integration_secret(%s, %s)",
        (args.telegram_user_id, args.provider),
    )
    if not secret:
        raise IntegrationAdminError("Stored credential is unavailable.")
    validate_provider_credential(args.provider, secret)
    (connection_id,) = _execute_one(
        "select private.admin_record_validation(%s, %s, %s)",
        (args.telegram_user_id, args.provider, args.actor),
    )
    return {
        "connection_id": str(connection_id),
        "provider": args.provider,
        "validated": True,
    }


def set_credential_state(args: argparse.Namespace) -> Dict[str, Any]:
    (connection_id,) = _execute_one(
        "select private.admin_set_integration_state(%s, %s, %s, %s)",
        (
            args.telegram_user_id,
            args.provider,
            args.credential_action,
            args.actor,
        ),
    )
    return {
        "connection_id": str(connection_id),
        "provider": args.provider,
        "status": "revoked" if args.credential_action == "revoke" else "disabled",
        "enabled": False,
        "vault_secret_retained": True,
    }


def capability_summary(args: argparse.Namespace) -> Dict[str, Any]:
    rows = _execute_all(
        "select * from private.admin_capability_summary(%s)",
        (args.telegram_user_id,),
    )
    if not rows:
        raise IntegrationAdminError("User was not found.")
    first = rows[0]
    user = {
        "user_id": str(first[0]),
        "display_name": first[1],
        "timezone": first[2],
        "locale": first[3],
        "status": first[4],
        "telegram_user_id": first[5],
        "preference_schema_version": first[6],
        "preference_revision": first[7],
    }
    by_provider = {row[9]: row for row in rows if row[9]}
    capabilities = []
    for provider, adapter in DOMAIN_ADAPTERS.items():
        row = by_provider.get(provider)
        active = bool(
            row
            and user["status"] == "active"
            and row[10] == "connected"
            and row[11]
        )
        capabilities.append(
            {
                "provider": provider,
                "display_name": adapter.display_name,
                "status": row[10] if row else "not_connected",
                "enabled": bool(row[11]) if row else False,
                "active": active,
                "account_label": row[12] if row else None,
                "last_validated_at": _json_value(row[13]) if row else None,
                "credential_version": row[14] if row else None,
                "capabilities": adapter.capabilities if active else [],
            }
        )
    return {"user": user, "providers": capabilities}


def audit_check(_args: argparse.Namespace) -> Dict[str, Any]:
    rows = _execute_all("select * from private.admin_integrity_findings()")
    findings = [
        {"type": row[0], "subject_id": row[1], "details": row[2]} for row in rows
    ]
    preference_rows = _execute_all("select * from private.admin_preference_profiles()")
    already_invalid = {
        finding["subject_id"]
        for finding in findings
        if finding["type"] == "invalid_preferences"
    }
    for user_id, schema_version, preferences in preference_rows:
        try:
            if schema_version != 1:
                raise ValueError("unsupported schema")
            AssistantPreferencesV1.model_validate(preferences)
        except (ValueError, TypeError):
            subject_id = str(user_id)
            if subject_id not in already_invalid:
                findings.append(
                    {
                        "type": "invalid_preferences",
                        "subject_id": subject_id,
                        "details": {"schema_version": schema_version},
                    }
                )
    return {"ok": not findings, "finding_count": len(findings), "findings": findings}


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _print_result(result: Dict[str, Any], as_json: bool, stdout: TextIO) -> None:
    if as_json:
        print(json.dumps(result, default=_json_value, sort_keys=True), file=stdout)
        return
    if "providers" in result:
        user = result["user"]
        print(
            f"{user['display_name']} ({user['telegram_user_id']}): {user['status']}",
            file=stdout,
        )
        for provider in result["providers"]:
            availability = "available" if provider["active"] else provider["status"]
            print(f"- {provider['display_name']}: {availability}", file=stdout)
        return
    if "findings" in result:
        print(
            "No integrity findings."
            if result["ok"]
            else f"Integrity findings: {result['finding_count']}",
            file=stdout,
        )
        for finding in result["findings"]:
            print(f"- {finding['type']}: {finding['subject_id']}", file=stdout)
        return
    if "resources" in result:
        print(f"{result['provider']} resources:", file=stdout)
        for resource in result["resources"]:
            suffix = " (primary)" if resource["is_primary"] else ""
            print(
                f"- {resource['label']}: {resource['id']}{suffix}",
                file=stdout,
            )
        return
    print(
        ", ".join(f"{key}={_json_value(value)}" for key, value in result.items()),
        file=stdout,
    )


def _add_actor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", default=DEFAULT_ACTOR)


def _add_user_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--telegram-user-id", type=int, required=True)


def _add_provider(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, required=True)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Administer Jarvis users and integrations.")
    parser.add_argument("--json", action="store_true", help="Emit sanitized JSON.")
    groups = parser.add_subparsers(dest="group", required=True)

    users = groups.add_parser("user").add_subparsers(dest="command", required=True)
    create = users.add_parser("create")
    _add_user_target(create)
    create.add_argument("--username", default="")
    create.add_argument("--display-name", required=True)
    create.add_argument("--timezone", required=True)
    create.add_argument("--locale", default="en")
    _add_actor(create)
    disable = users.add_parser("disable")
    _add_user_target(disable)
    _add_actor(disable)

    identities = groups.add_parser("identity").add_subparsers(
        dest="command", required=True
    )
    attach = identities.add_parser("attach-telegram")
    attach.add_argument("--owner-telegram-user-id", type=int, required=True)
    _add_user_target(attach)
    attach.add_argument("--username", default="")
    attach.add_argument("--display-name", default="")
    attach.add_argument("--primary", action="store_true")
    _add_actor(attach)

    preferences = groups.add_parser("preferences").add_subparsers(
        dest="command", required=True
    )
    set_parser = preferences.add_parser("set")
    _add_user_target(set_parser)
    set_parser.add_argument("--file", type=Path, required=True)
    _add_actor(set_parser)

    credentials = groups.add_parser("credential").add_subparsers(
        dest="command", required=True
    )
    for command in ("import", "rotate", "reconnect"):
        credential = credentials.add_parser(command)
        _add_user_target(credential)
        _add_provider(credential)
        credential.add_argument("--secret-file", type=Path, required=True)
        credential.add_argument("--account-label")
        credential.set_defaults(credential_action=command)
        _add_actor(credential)

    resources = groups.add_parser("resources").add_subparsers(
        dest="command", required=True
    )
    list_parser = resources.add_parser("list")
    _add_user_target(list_parser)
    _add_provider(list_parser)
    validate = credentials.add_parser("validate")
    _add_user_target(validate)
    _add_provider(validate)
    _add_actor(validate)
    for command in ("disable", "revoke"):
        credential = credentials.add_parser(command)
        _add_user_target(credential)
        _add_provider(credential)
        credential.set_defaults(credential_action=command)
        _add_actor(credential)

    capabilities = groups.add_parser("capabilities").add_subparsers(
        dest="command", required=True
    )
    show = capabilities.add_parser("show")
    _add_user_target(show)

    audit = groups.add_parser("audit").add_subparsers(dest="command", required=True)
    audit.add_parser("check")
    return parser.parse_args(argv)


def _dispatch(args: argparse.Namespace, stdin: TextIO) -> Dict[str, Any]:
    handler = (args.group, args.command)
    if handler == ("user", "create"):
        return create_user(args)
    if handler == ("user", "disable"):
        return disable_user(args)
    if handler == ("identity", "attach-telegram"):
        return attach_telegram_identity(args)
    if handler == ("preferences", "set"):
        return set_preferences(args, stdin)
    if args.group == "credential" and args.command in ("import", "rotate", "reconnect"):
        return store_credential(args, stdin)
    if handler == ("credential", "validate"):
        return validate_stored_credential(args)
    if args.group == "credential" and args.command in ("disable", "revoke"):
        return set_credential_state(args)
    if handler == ("resources", "list"):
        return list_resources(args)
    if handler == ("capabilities", "show"):
        return capability_summary(args)
    if handler == ("audit", "check"):
        return audit_check(args)
    raise IntegrationAdminError("Unsupported administrative command.")


def main(
    argv: Optional[list[str]] = None,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = _parse_args(argv)
    try:
        result = _dispatch(args, stdin)
    except IntegrationAdminError as exc:
        print(f"error: {exc}", file=stderr)
        return 1
    except Exception:
        # The unexpected exception may contain SQL parameters or provider
        # response bodies. Never interpolate it at the CLI boundary.
        print("error: administrative operation failed.", file=stderr)
        return 1
    _print_result(result, args.json, stdout)
    if args.group == "audit" and not result["ok"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
