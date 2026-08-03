"""Administrative onboarding CLI: parsing, validation, safety, and dispatch."""

import io
import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import manage_integrations as admin


VALID_PREFERENCES = {
    "communication": {"tone": "casual", "verbosity": "concise"},
    "routing": {
        "task_provider": "todoist",
        "event_provider": "google_calendar",
        "calendar_usage": "default",
    },
    "domains": {
        "todoist": {},
        "google_calendar": {"event_category_defaults": {"work": "Work"}},
    },
}


def test_parser_exposes_all_command_groups():
    cases = [
        ["user", "disable", "--telegram-user-id", "1"],
        [
            "identity",
            "attach-telegram",
            "--owner-telegram-user-id",
            "1",
            "--telegram-user-id",
            "2",
        ],
        ["preferences", "set", "--telegram-user-id", "1", "--file", "-"],
        ["credential", "validate", "--telegram-user-id", "1", "--provider", "todoist"],
        ["credential", "disable", "--telegram-user-id", "1", "--provider", "todoist"],
        ["credential", "revoke", "--telegram-user-id", "1", "--provider", "todoist"],
        ["capabilities", "show", "--telegram-user-id", "1"],
        ["audit", "check"],
    ]
    for argv in cases:
        assert admin._parse_args(argv).command


@pytest.mark.parametrize("command", ["import", "rotate", "reconnect"])
def test_credential_writes_require_file_or_stdin(command):
    args = admin._parse_args(
        [
            "credential",
            command,
            "--telegram-user-id",
            "1",
            "--provider",
            "todoist",
            "--secret-file",
            "-",
        ]
    )
    assert args.secret_file == Path("-")
    assert args.credential_action == command


def test_preferences_are_strictly_validated():
    # The routing before-validator materializes reminder/time_related/explicit
    # calendar providers from task/event_provider, so exclude_unset serialization
    # returns those derived keys too. That normalization is the correct stored form.
    expected = {
        **VALID_PREFERENCES,
        "routing": {
            **VALID_PREFERENCES["routing"],
            "reminder_provider": "todoist",
            "time_related_provider": "google_calendar",
            "explicit_calendar_provider": "google_calendar",
        },
    }
    assert admin._load_preferences(
        Path("-"), io.StringIO(json.dumps(VALID_PREFERENCES))
    ) == expected
    invalid = {**VALID_PREFERENCES, "unexpected": True}
    with pytest.raises(admin.IntegrationAdminError, match="supported schema"):
        admin._load_preferences(Path("-"), io.StringIO(json.dumps(invalid)))


def test_load_preferences_serializes_llm_and_execution_sections():
    document = {
        **VALID_PREFERENCES,
        "llm": {"model": "deepseek-v4-pro", "reasoning_effort": "max"},
        "execution": {"max_agent_turns": 20, "allow_mutations": False},
    }
    serialized = admin._load_preferences(Path("-"), io.StringIO(json.dumps(document)))
    assert serialized["llm"] == {"model": "deepseek-v4-pro", "reasoning_effort": "max"}
    assert serialized["execution"] == {"max_agent_turns": 20, "allow_mutations": False}


def test_load_preferences_clearing_section_omits_it():
    # There is no field-level clear: "clearing" means submitting a full document
    # without the section. Omitted sections are absent from the serialized payload,
    # and admin_set_preferences replaces the whole document (see set_preferences).
    serialized = admin._load_preferences(
        Path("-"), io.StringIO(json.dumps(VALID_PREFERENCES))
    )
    assert "llm" not in serialized
    assert "execution" not in serialized


def test_load_preferences_rejects_invalid_llm_section():
    for bad_llm in (
        {"reasoning_effort": "disabled"},
        {"model": "x" * 101},  # exceeds max_length
        {"unexpected": True},  # extra key
    ):
        document = {**VALID_PREFERENCES, "llm": bad_llm}
        with pytest.raises(admin.IntegrationAdminError, match="supported schema"):
            admin._load_preferences(Path("-"), io.StringIO(json.dumps(document)))


def test_user_creation_rejects_non_iana_timezone(monkeypatch):
    args = Namespace(
        telegram_user_id=1,
        username="tester",
        display_name="Tester",
        timezone="Singapore-ish",
        locale="en",
        actor="admin:test",
    )
    monkeypatch.setattr(
        admin,
        "_execute_one",
        lambda statement, params: pytest.fail("database should not be called"),
    )
    with pytest.raises(admin.IntegrationAdminError, match="IANA"):
        admin.create_user(args)


def test_empty_secret_is_rejected_without_echoing_input():
    with pytest.raises(admin.IntegrationAdminError) as exc:
        admin._read_input(Path("-"), io.StringIO("   "), description="Credential")
    assert "Credential" in str(exc.value)


def test_todoist_validation_performs_harmless_read(monkeypatch):
    response = MagicMock()
    response.__enter__.return_value = response
    monkeypatch.setattr(admin.urllib.request, "urlopen", lambda request, timeout: response)
    result = admin.validate_provider_credential("todoist", "sentinel-token")
    response.read.assert_called_once_with(1)
    assert result["secret"] == "sentinel-token"
    assert result["settings"] == {"credential_source": "supabase_vault"}


def test_provider_failure_is_sanitized(monkeypatch):
    monkeypatch.setattr(
        admin.urllib.request,
        "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(
            admin.urllib.error.URLError("sentinel-token")
        ),
    )
    with pytest.raises(admin.IntegrationAdminError) as exc:
        admin.validate_provider_credential("todoist", "sentinel-token")
    assert "sentinel-token" not in str(exc.value)


def test_store_credential_passes_secret_as_sql_parameter(monkeypatch):
    monkeypatch.setattr(
        admin,
        "validate_provider_credential",
        lambda provider, secret: {
            "secret": secret,
            "account_label": None,
            "external_account_id": None,
            "scopes": [],
            "settings": {"credential_source": "supabase_vault"},
            "token_expires_at": None,
        },
    )
    captured = {}

    def fake_execute(statement, params):
        captured["statement"] = statement
        captured["params"] = params
        return ("connection-id", 2)

    monkeypatch.setattr(admin, "_execute_one", fake_execute)
    args = Namespace(
        telegram_user_id=1,
        provider="todoist",
        secret_file=Path("-"),
        account_label="Tasks",
        credential_action="rotate",
        actor="admin:test",
    )
    result = admin.store_credential(args, io.StringIO("sentinel-token"))

    assert "sentinel-token" not in captured["statement"]
    assert "sentinel-token" in captured["params"]
    assert result["credential_version"] == 2
    assert "secret" not in result


def test_capability_summary_is_sanitized(monkeypatch):
    monkeypatch.setattr(
        admin,
        "_execute_all",
        lambda statement, params: [
            (
                "user-id",
                "Tester",
                "Asia/Singapore",
                "en",
                "active",
                "123",
                1,
                4,
                VALID_PREFERENCES,
                "todoist",
                "connected",
                True,
                "Tasks",
                None,
                2,
            )
        ],
    )
    result = admin.capability_summary(Namespace(telegram_user_id=123))
    assert result["providers"][0]["active"] is True
    assert "preferences" not in result["user"]
    assert "secret" not in json.dumps(result).lower()


def test_audit_findings_return_exit_two(monkeypatch):
    monkeypatch.setattr(
        admin,
        "_dispatch",
        lambda args, stdin: {
            "ok": False,
            "finding_count": 1,
            "findings": [
                {"type": "incomplete_profile", "subject_id": "u1", "details": {}}
            ],
        },
    )
    stdout = io.StringIO()
    assert admin.main(["audit", "check"], stdout=stdout) == 2
    assert "Integrity findings: 1" in stdout.getvalue()


def test_audit_check_detects_preferences_rejected_by_runtime_model(monkeypatch):
    invalid = {**VALID_PREFERENCES, "unexpected": True}
    calls = iter(
        [
            [],
            [("user-id", 1, invalid)],
        ]
    )
    monkeypatch.setattr(admin, "_execute_all", lambda statement, params=(): next(calls))
    result = admin.audit_check(Namespace())
    assert result["ok"] is False
    assert result["findings"][0]["type"] == "invalid_preferences"


def test_json_success_output_contains_no_secret(monkeypatch):
    monkeypatch.setattr(
        admin,
        "_dispatch",
        lambda args, stdin: {"connection_id": "c1", "validated": True},
    )
    stdout = io.StringIO()
    code = admin.main(
        ["--json", "credential", "validate", "--telegram-user-id", "1", "--provider", "todoist"],
        stdout=stdout,
    )
    assert code == 0
    assert json.loads(stdout.getvalue())["connection_id"] == "c1"


def test_unexpected_exception_never_reaches_stderr(monkeypatch):
    sentinel = "sentinel-secret"

    def explode(args, stdin):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(admin, "_dispatch", explode)
    stderr = io.StringIO()
    code = admin.main(["audit", "check"], stderr=stderr)
    assert code == 1
    assert sentinel not in stderr.getvalue()


def test_database_exception_chain_is_sanitized(monkeypatch):
    sentinel = "sentinel-secret"

    class BrokenConnection:
        def __enter__(self):
            raise RuntimeError(sentinel)

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(admin, "_connect", lambda: BrokenConnection())
    with pytest.raises(admin.IntegrationAdminError) as exc:
        admin._execute_one("select %s", (sentinel,))
    assert sentinel not in str(exc.value)


def test_migration_defines_restricted_atomic_entrypoints():
    migration = (
        Path(__file__).parents[2]
        / "supabase"
        / "migrations"
        / "20260705120000_add_admin_onboarding_cli_functions.sql"
    ).read_text(encoding="utf-8")
    for function in (
        "private.admin_upsert_user",
        "private.admin_set_preferences",
        "private.admin_store_integration",
        "private.admin_integrity_findings",
        "private.admin_preference_profiles",
    ):
        assert function in migration
    assert "grant execute" in migration
    assert "jarvis_admin_runtime" in migration
    assert "vault.decrypted_secrets" in migration
