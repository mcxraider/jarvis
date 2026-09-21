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
MEMORY_MIGRATION = next(
    (ROOT / "supabase" / "migrations").glob("*_thread_memory.sql")
).read_text(encoding="utf-8")
CLEANUP_GUARD_MIGRATION = next(
    (ROOT / "supabase" / "migrations").glob(
        "*_guard_thread_memory_cleanup_storage_order.sql"
    )
).read_text(encoding="utf-8")


def normalized_sql() -> str:
    return " ".join(MIGRATION.lower().split())


def normalized_memory_sql() -> str:
    return " ".join(MEMORY_MIGRATION.lower().split())


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
    assert 'prepare_thread_memory_cleanup' in EDGE_FUNCTION
    assert 'cleanup_runtime_state_daily' in EDGE_FUNCTION


def test_edge_function_deletes_images_before_message_rows():
    image_list = EDGE_FUNCTION.index('prepare_thread_memory_cleanup')
    image_delete = EDGE_FUNCTION.index('storage/v1/object/${THREAD_IMAGE_BUCKET}')
    row_delete = EDGE_FUNCTION.index('cleanup_runtime_state_daily')
    assert image_list < image_delete < row_delete
    assert 'JSON.stringify({ prefixes: batch })' in EDGE_FUNCTION
    assert 'return Response.json({ error: "cleanup_failed" }, { status: 500 });' in EDGE_FUNCTION


def test_edge_function_returns_all_cleanup_counts():
    for field in (
        "idempotency_results_deleted",
        "checkpoint_writes_deleted",
        "checkpoints_deleted",
        "checkpoint_blobs_deleted",
        "thread_messages_deleted",
        "threadImages",
    ):
        assert field in EDGE_FUNCTION


def test_thread_memory_migration_provisions_schema_indexes_and_rls():
    sql = normalized_memory_sql()
    assert "create table public.thread_memory_heads" in sql
    assert "create table public.thread_messages" in sql
    assert "threads_conversation_key_check" in sql
    assert "threads_memory_status_check" in sql
    assert "threads_memory_lookup_idx" in sql
    assert "threads_previous_thread_id_idx" in sql
    assert "thread_memory_heads_latest_thread_id_idx" in sql
    assert "thread_messages_created_at_idx" in sql
    assert "thread_messages_user_created_at_idx" in sql
    assert "alter table public.thread_memory_heads enable row level security" in sql
    assert "alter table public.thread_messages enable row level security" in sql
    assert "to jarvis_runtime using (true) with check (true)" in sql
    assert "grant usage, select on sequence public.thread_messages_id_seq" in sql


def test_thread_memory_migration_locks_down_functions_and_storage():
    sql = normalized_memory_sql()
    assert "'thread-images', 'thread-images', false, 10485760" in sql
    assert "array['image/jpeg']::text[]" in sql
    assert "security invoker set search_path = ''" in sql
    assert "grant execute on function public.prepare_thread_memory(" in sql
    assert ") to jarvis_runtime" in sql
    assert "security definer set search_path = ''" in sql
    assert "grant execute on function public.prepare_thread_memory_cleanup(text) to service_role" in sql
    assert "from public, anon, authenticated, jarvis_runtime" in sql


def test_thread_memory_cleanup_keeps_six_fields_and_rolling_cutoff():
    sql = normalized_memory_sql()
    assert "thread_messages_deleted integer" in sql
    assert "delete from public.thread_messages" in sql
    assert "now() - interval '48 hours'" in sql
    assert "get diagnostics thread_messages_deleted = row_count" in sql


def test_thread_memory_cleanup_keeps_rows_until_storage_objects_are_gone():
    sql = " ".join(CLEANUP_GUARD_MIGRATION.lower().split())
    assert "delete from public.thread_messages" in sql
    assert "from storage.objects object" in sql
    assert "object.bucket_id = 'thread-images'" in sql
    assert "object.name = message.payload ->> 'object_path'" in sql
