"""Static safety contract for the staged legacy-preference retirement."""

from pathlib import Path


MIGRATIONS = Path(__file__).parents[2] / "supabase" / "migrations"


def _migration(suffix: str) -> str:
    return next(MIGRATIONS.glob(f"*_{suffix}.sql")).read_text(encoding="utf-8").lower()


def test_expand_migration_backfills_and_dual_writes_typed_policy():
    sql = _migration("expand_typed_user_policy_storage")

    assert "create table private.user_runtime_policies" in sql
    assert "create table private.user_resource_restrictions" in sql
    assert "create table private.user_onboarding_metadata" in sql
    assert "legacy preferences contain unknown keys" in sql
    assert "every active user must have reviewed custom instructions" in sql
    assert "compatibility-window dual write" in sql
    assert "create or replace function private.runtime_policy_shadow_status" in sql
    assert "create or replace function private.onboard_user" in sql
    assert "insert into private.user_runtime_policies(user_id, updated_by)" in sql
    assert "drop function private.admin_capability_summary" in sql
    assert "runtime_policy_shadow_mismatch" in sql
    assert "policy revision must advance by exactly one" in sql
    assert "update public.users app_user" in sql
    assert "preferences_updated_by = btrim(p_actor)" in sql
    assert "alter table public.users drop column preferences" not in sql


def test_contract_archives_rewrites_and_restrictively_drops_legacy_column():
    sql = _migration("retire_legacy_user_preferences")

    assert "create table private.users_preferences_archive" in sql
    assert "create table private.preference_retirement_manifest" in sql
    assert "deterministic_checksum" in sql
    assert "end compatibility-window dual writes" in sql
    assert "create or replace function private.admin_set_runtime_policy" in sql
    assert "select true, true" in sql
    assert "catalog dependency still references public.users.preferences" in sql
    assert "alter table public.users drop column preferences" in sql
    assert "drop column preferences cascade" not in sql
    assert "set local lock_timeout = '5s'" in sql


def test_live_matrix_script_uses_user_one_and_all_domain_shapes():
    script = (Path(__file__).parents[2] / "scripts" / "verify_preferences_retirement.sh").read_text(
        encoding="utf-8"
    )

    assert "--user-1" in script
    assert "--no-mutations" in script
    assert "Todoist" in script
    assert "Google Calendar" in script
    assert "Do not call any tools" in script
    assert "Compare my overdue Todoist tasks" in script
