"""Declarative onboarding dossier validation, generation, and application."""

import json
import stat
from pathlib import Path

import pytest
import yaml

from agents.agent_api.app.user_context.preferences import AssistantPreferencesV1
from onboard import onboard


EXAMPLE = Path(__file__).parents[2] / "onboard" / "example-dossier.yml"


def _payload() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "dossier.yml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_example_validates_and_round_trips_runtime_preferences():
    dossier = onboard.load_dossier(EXAMPLE)
    assert dossier.profile.timezone == "Asia/Singapore"
    assert (
        AssistantPreferencesV1.model_validate(onboard.preference_payload(dossier))
        == dossier.preferences
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["profile"].update(timezone="Singapore-ish"), "IANA"),
        (lambda p: p["preferences"]["communication"].update(tone="charming"), "tone"),
        (
            lambda p: p["integrations"]["todoist"].update(requested=False),
            "routing providers must be requested",
        ),
        (lambda p: p.update(api_token="sentinel-secret"), "secret field"),
        (lambda p: p.update(future_integrations=["github", "github"]), "duplicates"),
    ],
)
def test_invalid_dossiers_fail_safely(tmp_path, mutate, message):
    payload = _payload()
    mutate(payload)
    with pytest.raises(onboard.OnboardingError, match=message):
        onboard.load_dossier(_write(tmp_path, payload))


def test_future_integrations_are_interest_only():
    dossier = onboard.load_dossier(EXAMPLE)
    preview = onboard.render_preview(dossier)
    assert "Future interests: github, notion, gmail, google_drive" in preview
    assert "Current integrations: todoist, google_calendar" in preview


def test_sql_is_escaped_secret_free_transactional_and_idempotent(tmp_path):
    payload = _payload()
    payload["profile"]["display_name"] = "Alex O'Brien"
    dossier = onboard.load_dossier(_write(tmp_path, payload))
    sql = onboard.render_sql(dossier)
    assert "Alex O''Brien" in sql
    assert sql.startswith("-- Generated")
    assert "\nbegin;\n" in sql
    assert sql.rstrip().endswith("commit;")
    assert "if not exists" in sql
    assert "admin_upsert_user" in sql
    assert "admin_set_preferences" in sql
    assert "credential import" not in sql
    assert onboard.render_sql(dossier) == sql


def test_generate_creates_deterministic_expected_artifacts(tmp_path):
    dossier = onboard.load_dossier(EXAMPLE)
    first = onboard.generate(dossier, tmp_path)
    contents = {path.name: path.read_text(encoding="utf-8") for path in first}
    second = onboard.generate(dossier, tmp_path)
    assert [path.name for path in first] == [
        "preferences.json",
        "supabase-onboarding.sql",
        "operator-checklist.md",
    ]
    assert contents == {
        path.name: path.read_text(encoding="utf-8") for path in second
    }
    assert json.loads(contents["preferences.json"]) == onboard.preference_payload(dossier)
    assert "Alex Tasks" in contents["operator-checklist.md"]
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in first)


def test_apply_uses_ordered_parameterized_admin_operations(monkeypatch):
    dossier = onboard.load_dossier(EXAMPLE)
    calls = []

    def create(args):
        calls.append(("user", args.telegram_user_id, args.actor))
        return {"user_id": "u1", "created": True}

    def preferences(args, stdin):
        calls.append(("preferences", json.loads(stdin.read()), args.actor))
        return {"user_id": "u1", "revision": 1}

    monkeypatch.setattr(onboard.admin, "create_user", create)
    monkeypatch.setattr(onboard.admin, "set_preferences", preferences)
    results = onboard.apply_dossier(dossier, "admin:test")
    assert [call[0] for call in calls] == ["user", "preferences"]
    assert calls[0] == ("user", 246801357, "admin:test")
    assert "communication" in calls[1][1]
    assert results[1]["revision"] == 1


def test_cli_error_never_echoes_secret_value(tmp_path, capsys):
    payload = _payload()
    payload["api_token"] = "sentinel-secret"
    code = onboard.main(["validate", str(_write(tmp_path, payload))])
    captured = capsys.readouterr()
    assert code == 1
    assert "sentinel-secret" not in captured.err
