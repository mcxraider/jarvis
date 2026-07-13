"""Contract checks for database-local quota and usage scheduling."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260711141100_remove_database_local_edge_cron_hops.sql"
).read_text(encoding="utf-8")
QUOTA_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260711134600_reset_rate_limits_daily_3am.sql"
).read_text(encoding="utf-8")
RESTORE_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260713025716_restore_rate_limit_reset_edge_schedule.sql"
).read_text(encoding="utf-8")


def normalized(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_reset_cron_and_secret_backed_function_are_removed():
    sql = normalized(MIGRATION)
    assert "cron.unschedule('reset-rate-limits-daily-singapore')" in sql
    assert "drop function if exists public.reset_rate_limits_daily(text)" in sql
    assert "'rate_limit_reset_cron_secret'" in sql


def test_snapshot_runs_directly_in_postgres_without_secret_or_http():
    sql = normalized(MIGRATION)
    assert "public.snapshot_usage_daily( p_day date )" in sql
    assert "security invoker" in sql
    assert "public.snapshot_usage_daily( (now() at time zone 'asia/singapore')::date - 1 )" in sql
    assert "'5 16 * * *'" in sql
    assert "net.http_post" not in sql
    assert "x-cron-secret" not in sql
    assert "p_cron_secret" not in sql


def test_snapshot_is_not_executable_by_application_roles():
    sql = normalized(MIGRATION)
    assert (
        "revoke all on function public.snapshot_usage_daily(date) "
        "from public, anon, authenticated, service_role, jarvis_runtime"
    ) in sql


def test_quota_consumption_rolls_expired_window_forward_atomically():
    sql = normalized(QUOTA_MIGRATION)
    assert "when quota.reset_at <= now() then 0" in sql
    assert "when quota.reset_at <= now() then 1" in sql
    assert "when quota.reset_at <= now() then sgt_next_reset" in sql
    assert "or quota.daily_threads_used < quota.daily_thread_limit" in sql


def test_reset_edge_function_schedule_is_restored_at_3am_singapore():
    sql = normalized(RESTORE_MIGRATION)
    assert "public.reset_rate_limits_daily( p_cron_secret text )" in sql
    assert "'reset-rate-limits-daily-singapore'" in sql
    assert "'0 19 * * *'" in sql
    assert "/functions/v1/reset-rate-limits-daily" in sql
    assert "'x-cron-secret'" in sql


def test_reset_function_remains_service_role_only():
    sql = normalized(RESTORE_MIGRATION)
    assert (
        "revoke all on function public.reset_rate_limits_daily(text) "
        "from public, anon, authenticated"
    ) in sql
    assert (
        "grant execute on function public.reset_rate_limits_daily(text) "
        "to service_role"
    ) in sql
