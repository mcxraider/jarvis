"""Contract checks for the destructive legacy-identity cleanup migration."""

from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "supabase"
    / "migrations"
    / "20260706022526_remove_legacy_user_identity_columns.sql"
).read_text()


def test_cleanup_refuses_an_incomplete_identity_backfill():
    normalized = " ".join(MIGRATION.lower().split())
    assert "not exists (" in normalized
    assert "identity.identity_provider = 'telegram'" in normalized
    assert "identity.external_subject = app_user.telegram_user_id::text" in normalized
    assert "raise exception" in normalized


def test_cleanup_drops_only_the_legacy_provider_columns():
    normalized = " ".join(MIGRATION.lower().split())
    assert "drop column telegram_user_id" in normalized
    assert "drop column telegram_username" in normalized
    assert "drop column telegram_first_name" in normalized
    assert "drop column display_name" not in normalized
