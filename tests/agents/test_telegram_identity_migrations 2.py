"""Contracts for the expand/contract Telegram identity migration pair."""

from pathlib import Path


MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
EXPAND = (
    MIGRATIONS / "20260706024802_expand_telegram_identities.sql"
).read_text().lower()
FINALIZE = (
    MIGRATIONS / "20260706024803_finalize_telegram_identities.sql"
).read_text().lower()


def test_expand_keeps_old_and_new_identity_shapes_in_sync():
    assert "add column telegram_id bigint" in EXPAND
    assert "create trigger user_identities_sync_telegram_columns" in EXPAND
    assert "create view public.telegram_identities" in EXPAND
    assert "security_invoker = true" in EXPAND
    assert "only telegram identities are supported" in EXPAND


def test_finalize_removes_generic_and_duplicate_profile_columns():
    for column in (
        "identity_provider",
        "external_subject",
        "display_name",
        "metadata",
        "is_primary",
        "id",
    ):
        assert f"drop column {column}" in FINALIZE
    assert "rename to telegram_identities" in FINALIZE
    assert "telegram_identities_pkey primary key (user_id)" in FINALIZE
    assert "drop function public.resolve_user_id(text, text)" in FINALIZE


def test_finalize_preserves_users_as_display_name_authority():
    assert "alter column display_name set not null" in FINALIZE
    assert "users_display_name_not_blank" in FINALIZE
    assert "identity.display_name" not in FINALIZE
