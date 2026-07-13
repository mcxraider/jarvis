"""Contract checks for nightly checkpoint and idempotency cleanup."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260711135854_cleanup_runtime_state_daily.sql"
).read_text(encoding="utf-8")
EDGE_FUNCTION = (
    ROOT
    / "supabase"
    / "functions"
    / "cleanup-runtime-state-daily"
    / "index.ts"
).read_text(encoding="utf-8")


def normalized_sql() -> str:
    return " ".join(MIGRATION.lower().split())


def test_cleanup_uses_singapore_calendar_cutoff_and_expected_schedule():
    sql = normalized_sql()
    assert "now() at time zone 'asia/singapore'" in sql
    assert "::date - 1" in sql
    assert "'0 20 * * *'" in sql
    assert "cleanup-runtime-state-daily-singapore" in sql


def test_cleanup_deletes_checkpoint_dependents_in_safe_order():
    sql = normalized_sql()
    writes = sql.index("delete from public.checkpoint_writes")
    checkpoints = sql.index("delete from public.checkpoints")
    blobs = sql.index("delete from public.checkpoint_blobs")
    assert writes < checkpoints < blobs
    assert "checkpoint_row.checkpoint ->> 'ts'" in sql
    assert "where not exists" in sql[blobs:]
    assert "checkpoint_row.checkpoint -> 'channel_versions' ->> blob.channel = blob.version" in sql
    assert "delete from public.checkpoint_migrations" not in sql


def test_cleanup_rpc_is_secret_checked_and_service_role_only():
    sql = normalized_sql()
    assert "extensions.digest(p_cron_secret, 'sha256')" in sql
    assert "revoke all on function public.cleanup_runtime_state_daily(text) from public, anon, authenticated" in sql
    assert "grant execute on function public.cleanup_runtime_state_daily(text) to service_role" in sql
    assert "runtime_state_cleanup_cron_secret" in sql


def test_edge_function_rejects_invalid_request_shapes_and_sanitizes_failures():
    assert 'request.method !== "POST"' in EDGE_FUNCTION
    assert 'status: 405' in EDGE_FUNCTION
    assert 'request.headers.get("x-cron-secret")' in EDGE_FUNCTION
    assert 'status: 401' in EDGE_FUNCTION
    assert 'error.message' not in EDGE_FUNCTION
    assert 'console.' not in EDGE_FUNCTION
    assert 'cleanup_runtime_state_daily' in EDGE_FUNCTION


def test_edge_function_returns_all_cleanup_counts():
    for field in (
        "idempotency_results_deleted",
        "checkpoint_writes_deleted",
        "checkpoints_deleted",
        "checkpoint_blobs_deleted",
    ):
        assert field in EDGE_FUNCTION
