"""Optional live database checks for the thread-memory contract."""

from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
import uuid

import pytest


TEST_DSN = os.getenv("JARVIS_ADMIN_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="JARVIS_ADMIN_TEST_POSTGRES_DSN is not configured",
)


def _identity(cursor, telegram_id: int, user_id: uuid.UUID) -> None:
    cursor.execute(
        "insert into public.users (id, display_name) values (%s, 'Thread memory test')",
        (user_id,),
    )
    cursor.execute(
        """
        insert into public.user_identities (
          user_id, identity_provider, external_subject, telegram_id, verified_at
        ) values (%s, 'telegram', %s, %s, now())
        """,
        (user_id, str(telegram_id), telegram_id),
    )


def _prepare(
    cursor,
    telegram_id: int,
    conversation_key: str,
    thread_id: str | None,
    title: str | None,
    *,
    reset: bool = False,
):
    cursor.execute("set local role jarvis_app")
    cursor.execute(
        """
        select *
        from public.prepare_thread_memory(%s, %s, %s, %s, %s)
        """,
        (telegram_id, conversation_key, thread_id, title, reset),
    )
    rows = cursor.fetchall()
    cursor.execute("reset role")
    return rows


def _new_scope(prefix: str) -> tuple[int, uuid.UUID, str]:
    token = uuid.uuid4()
    telegram_id = 8_000_000_000_000 + token.int % 1_000_000_000_000
    return telegram_id, token, f"internal:{prefix}_{token.hex}"


def test_thread_memory_database_contract() -> None:
    import psycopg
    from psycopg.rows import dict_row

    telegram_id, user_id, conversation_key = _new_scope("contract")
    other_telegram_id, other_user_id, _ = _new_scope("owner")

    with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            _identity(cursor, telegram_id, user_id)
            _identity(cursor, other_telegram_id, other_user_id)

            first = _prepare(
                cursor,
                telegram_id,
                conversation_key,
                "memory-contract-a",
                "first",
            )
            first_lineage = first[0]["lineage_id"]
            assert first[0]["previous_thread_id"] is None
            cursor.execute(
                """
                update public.threads
                   set status = 'completed', memory_status = 'complete'
                 where thread_id = 'memory-contract-a'
                """
            )
            cursor.execute(
                """
                insert into public.thread_messages (
                  thread_id, user_id, sequence, kind, payload
                ) values (
                  'memory-contract-a', %s, 1, 'assistant',
                  '{"role":"assistant","content":"remembered"}'::jsonb
                )
                """,
                (user_id,),
            )

            second = _prepare(
                cursor,
                telegram_id,
                conversation_key,
                "memory-contract-b",
                "second",
            )
            assert {row["previous_thread_id"] for row in second} == {
                "memory-contract-a"
            }
            assert [row["sequence"] for row in second] == [0, 1]
            assert second[0]["previous_status"] == "completed"

            repeated = _prepare(
                cursor,
                telegram_id,
                conversation_key,
                "memory-contract-b",
                "second",
            )
            assert repeated == second
            cursor.execute(
                """
                select count(*) as count
                from public.thread_messages
                where thread_id = 'memory-contract-b' and sequence = 0
                """
            )
            assert cursor.fetchone()["count"] == 1

            owner_isolated = _prepare(
                cursor,
                other_telegram_id,
                conversation_key,
                "memory-owner-isolated",
                "owner",
            )
            assert owner_isolated[0]["previous_thread_id"] is None
            conversation_isolated = _prepare(
                cursor,
                telegram_id,
                f"internal:other_{uuid.uuid4().hex}",
                "memory-conversation-isolated",
                "conversation",
            )
            assert conversation_isolated[0]["previous_thread_id"] is None

            reset_with_message = _prepare(
                cursor,
                telegram_id,
                conversation_key,
                "memory-reset-message",
                "new lineage",
                reset=True,
            )
            assert reset_with_message[0]["previous_thread_id"] is None
            assert reset_with_message[0]["lineage_id"] != first_lineage
            reset_only = _prepare(
                cursor,
                telegram_id,
                conversation_key,
                None,
                None,
                reset=True,
            )
            assert reset_only[0]["previous_thread_id"] is None
            assert reset_only[0]["lineage_id"] != reset_with_message[0]["lineage_id"]
            after_reset = _prepare(
                cursor,
                telegram_id,
                conversation_key,
                "memory-after-reset",
                "blank slate",
            )
            assert after_reset[0]["previous_thread_id"] is None
            assert after_reset[0]["lineage_id"] == reset_only[0]["lineage_id"]

            for status in ("completed", "failed", "cancelled", "interrupted"):
                status_key = f"internal:{status}_{uuid.uuid4().hex}"
                old_thread = f"memory-{status}-old"
                _prepare(cursor, telegram_id, status_key, old_thread, status)
                cursor.execute(
                    """
                    update public.threads
                       set status = %s,
                           memory_status = case
                             when %s = 'completed' then 'complete'
                             else 'incomplete'
                           end
                     where thread_id = %s
                    """,
                    (status, status, old_thread),
                )
                rows = _prepare(
                    cursor,
                    telegram_id,
                    status_key,
                    f"memory-{status}-new",
                    "next",
                )
                assert rows[0]["previous_status"] == status
                assert [row["sequence"] for row in rows] == [0]

            boundary_key = f"internal:boundary_{uuid.uuid4().hex}"
            _prepare(
                cursor,
                telegram_id,
                boundary_key,
                "memory-boundary-old",
                "boundary",
            )
            cursor.execute(
                "delete from public.thread_messages where thread_id = 'memory-boundary-old'"
            )
            cursor.execute(
                """
                insert into public.thread_messages (
                  thread_id, user_id, sequence, kind, payload, created_at
                ) values
                  (
                    'memory-boundary-old', %s, 0, 'user',
                    '{"role":"user","content":"expired"}'::jsonb,
                    now() - interval '48 hours 1 second'
                  ),
                  (
                    'memory-boundary-old', %s, 1, 'assistant',
                    '{"role":"assistant","content":"retained"}'::jsonb,
                    now() - interval '47 hours 59 minutes'
                  )
                """,
                (user_id, user_id),
            )
            boundary_rows = _prepare(
                cursor,
                telegram_id,
                boundary_key,
                "memory-boundary-new",
                "next",
            )
            assert [row["sequence"] for row in boundary_rows] == [1]

            bounded_key = f"internal:bounded_{uuid.uuid4().hex}"
            _prepare(
                cursor,
                telegram_id,
                bounded_key,
                "memory-bounded-old",
                "bounded",
            )
            cursor.execute(
                "delete from public.thread_messages where thread_id = 'memory-bounded-old'"
            )
            cursor.execute(
                """
                insert into public.thread_messages (
                  thread_id, user_id, sequence, kind, payload
                )
                select
                  'memory-bounded-old', %s, sequence, 'assistant',
                  jsonb_build_object(
                    'role', 'assistant',
                    'content', repeat('x', 400)
                  )
                from generate_series(0, 299) as sequence
                """,
                (user_id,),
            )
            bounded_rows = _prepare(
                cursor,
                telegram_id,
                bounded_key,
                "memory-bounded-new",
                "next",
            )
            sequences = [row["sequence"] for row in bounded_rows]
            assert sequences == sorted(sequences)
            assert len(sequences) <= 256
            assert min(sequences) >= 44
            assert max(sequences) == 299
            cursor.execute(
                """
                select sum(octet_length(payload::text)) as payload_bytes
                from public.thread_messages
                where thread_id = 'memory-bounded-old'
                  and sequence = any(%s)
                """,
                (sequences,),
            )
            assert cursor.fetchone()["payload_bytes"] <= 65_536

            cleanup_key = f"internal:cleanup_{uuid.uuid4().hex}"
            _prepare(
                cursor,
                telegram_id,
                cleanup_key,
                "memory-cleanup",
                "cleanup",
            )
            cursor.execute(
                """
                insert into public.thread_messages (
                  thread_id, user_id, sequence, kind, payload, created_at
                ) values
                  (
                    'memory-cleanup', %s, 1, 'image',
                    '{"bucket":"thread-images","uploaded":true,"object_path":"expired.jpg"}'::jsonb,
                    now() - interval '49 hours'
                  ),
                  (
                    'memory-cleanup', %s, 2, 'image',
                    '{"bucket":"thread-images","uploaded":true,"object_path":"retained.jpg"}'::jsonb,
                    now() - interval '47 hours'
                  ),
                  (
                    'memory-cleanup', %s, 3, 'image',
                    '{"bucket":"other","uploaded":true,"object_path":"wrong-bucket.jpg"}'::jsonb,
                    now() - interval '49 hours'
                  ),
                  (
                    'memory-cleanup', %s, 4, 'image',
                    '{"bucket":"thread-images","uploaded":false,"object_path":"not-uploaded.jpg"}'::jsonb,
                    now() - interval '49 hours'
                  )
                """,
                (user_id, user_id, user_id, user_id),
            )
            cursor.execute(
                """
                select decrypted_secret as secret
                from vault.decrypted_secrets
                where name = 'runtime_state_cleanup_cron_secret'
                """
            )
            cron_secret = cursor.fetchone()["secret"]
            cursor.execute("set local role service_role")
            cursor.execute(
                "select * from public.prepare_thread_memory_cleanup(%s)",
                (cron_secret,),
            )
            assert {row["object_path"] for row in cursor.fetchall()} == {
                "expired.jpg"
            }
            cursor.execute("reset role")
            cursor.execute(
                """
                select count(*) as count
                from public.thread_messages
                where created_at < now() - interval '48 hours'
                """
            )
            expired_count = cursor.fetchone()["count"]
            cursor.execute("set local role service_role")
            cursor.execute(
                "select * from public.cleanup_runtime_state_daily(%s)",
                (cron_secret,),
            )
            cleanup = cursor.fetchone()
            cursor.execute("reset role")
            assert cleanup["thread_messages_deleted"] == expired_count
            cursor.execute(
                """
                select sequence
                from public.thread_messages
                where thread_id = 'memory-cleanup'
                order by sequence
                """
            )
            assert [row["sequence"] for row in cursor.fetchall()] == [0, 2]

        connection.rollback()


def test_concurrent_head_claims_form_one_predecessor_chain() -> None:
    import psycopg
    from psycopg.rows import dict_row

    telegram_id, user_id, conversation_key = _new_scope("concurrent")
    thread_ids = (f"memory-claim-{uuid.uuid4().hex}", f"memory-claim-{uuid.uuid4().hex}")

    with psycopg.connect(TEST_DSN) as connection:
        with connection.cursor() as cursor:
            _identity(cursor, telegram_id, user_id)

    barrier = Barrier(2)

    def claim(thread_id: str):
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute("set role jarvis_app")
                barrier.wait(timeout=10)
                cursor.execute(
                    """
                    select *
                    from public.prepare_thread_memory(%s, %s, %s, %s, false)
                    """,
                    (telegram_id, conversation_key, thread_id, thread_id),
                )
                return cursor.fetchall()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, thread_ids))
        assert sum(rows[0]["previous_thread_id"] is None for rows in results) == 1

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select thread_id, previous_thread_id
                    from public.threads
                    where thread_id = any(%s)
                    order by thread_id
                    """,
                    (list(thread_ids),),
                )
                threads = cursor.fetchall()
                assert len(threads) == 2
                first = next(row for row in threads if row["previous_thread_id"] is None)
                second = next(row for row in threads if row["previous_thread_id"] is not None)
                assert second["previous_thread_id"] == first["thread_id"]
    finally:
        with psycopg.connect(TEST_DSN) as connection:
            connection.execute("delete from public.users where id = %s", (user_id,))


def test_cleanup_keeps_uploaded_image_rows_until_storage_object_is_gone() -> None:
    import psycopg
    from psycopg.rows import dict_row

    telegram_id, user_id, conversation_key = _new_scope("storage_guard")
    thread_id = f"memory-storage-guard-{uuid.uuid4().hex}"
    object_path = f"{user_id}/{thread_id}/{'a' * 64}.jpg"

    with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            _identity(cursor, telegram_id, user_id)
            _prepare(cursor, telegram_id, conversation_key, thread_id, "guard")
            cursor.execute(
                """
                insert into public.thread_messages (
                  thread_id, user_id, sequence, kind, payload, created_at
                ) values (
                  %s, %s, 1, 'image',
                  jsonb_build_object(
                    'bucket', 'thread-images',
                    'uploaded', true,
                    'object_path', %s
                  ),
                  now() - interval '49 hours'
                )
                """,
                (thread_id, user_id, object_path),
            )
            cursor.execute(
                "insert into storage.objects (bucket_id, name) values ('thread-images', %s)",
                (object_path,),
            )
            cursor.execute(
                """
                select decrypted_secret as secret
                from vault.decrypted_secrets
                where name = 'runtime_state_cleanup_cron_secret'
                """
            )
            cron_secret = cursor.fetchone()["secret"]

            cursor.execute("set local role service_role")
            cursor.execute(
                "select * from public.cleanup_runtime_state_daily(%s)",
                (cron_secret,),
            )
            cursor.fetchone()
            cursor.execute("reset role")
            cursor.execute(
                "select count(*) as count from public.thread_messages where thread_id = %s and sequence = 1",
                (thread_id,),
            )
            assert cursor.fetchone()["count"] == 1

            cursor.execute("set local storage.allow_delete_query = 'true'")
            cursor.execute(
                "delete from storage.objects where bucket_id = 'thread-images' and name = %s",
                (object_path,),
            )
            cursor.execute("set local role service_role")
            cursor.execute(
                "select * from public.cleanup_runtime_state_daily(%s)",
                (cron_secret,),
            )
            assert cursor.fetchone()["thread_messages_deleted"] == 1
            cursor.execute("reset role")

        connection.rollback()
