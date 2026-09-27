"""Contracts for the executed two-table onboarding migration sequence."""

from pathlib import Path


MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
PREPARE = next(MIGRATIONS.glob("*_prepare_two_table_user_onboarding.sql")).read_text().lower()
CUTOVER = next(MIGRATIONS.glob("*_cutover_two_table_user_onboarding.sql")).read_text().lower()
CLEANUP = next(MIGRATIONS.glob("*_cleanup_legacy_user_profiles.sql")).read_text().lower()


def test_prepare_is_additive_and_withholds_onboarding_execution():
    assert "add column telegram_id bigint" in PREPARE
    assert "add column preferences jsonb" in PREPARE
    assert "create or replace function private.onboard_user" in PREPARE
    assert "from public.user_identities" in PREPARE
    assert "from public.user_preferences" in PREPARE
    assert "drop table public.user_identities" not in PREPARE
    assert "jarvis_admin_runtime;" in PREPARE


def test_cutover_recopies_and_switches_all_shared_functions():
    assert "final authoritative recopy" in CUTOVER
    assert "create or replace function public.resolve_user_id" in CUTOVER
    assert "create or replace function private.admin_user_id_for_telegram" in CUTOVER
    assert "create or replace function private.admin_set_preferences" in CUTOVER
    assert "create or replace function private.admin_capability_summary" in CUTOVER
    assert "drop function private.admin_attach_telegram_identity" in CUTOVER
    assert "grant execute on function private.onboard_user" in CUTOVER
    assert "configured_provider_unavailable" not in CUTOVER


def test_cleanup_is_restrictive_and_preserves_unmatched_first_seen_rows():
    assert "private.telegram_onboarding_seen_archive" in CLEANUP
    assert "drop view public.telegram_identities;" in CLEANUP
    assert "drop table public.user_preferences;" in CLEANUP
    assert "drop table public.user_identities;" in CLEANUP
    assert "drop table public.telegram_onboarding_seen;" in CLEANUP
    assert "cascade" not in CLEANUP
