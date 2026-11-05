"""Regression checks for orphan heads, using an isolated migrated database."""

from pathlib import Path
import uuid

import pytest

from tests.agents.test_thread_memory_database_integration import (
    TEST_DSN, _identity, _new_scope, _prepare,
)

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="JARVIS_ADMIN_TEST_POSTGRES_DSN is not configured")
MIGRATION = Path(__file__).resolve().parents[2] / "supabase/migrations/20261104152216_guard_thread_memory_heads.sql"


def test_invalid_head_repair_preserves_snapshot_and_starts_new_lineage():
    import psycopg
    from psycopg.rows import dict_row

    telegram_id, user_id, key = _new_scope("orphan")
    orphan = str(uuid.uuid4())
    with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            _identity(cursor, telegram_id, user_id)
            old = _prepare(cursor, telegram_id, key, str(uuid.uuid4()), "registered")
            valid_key = f"internal:valid_{uuid.uuid4().hex}"
            valid_thread = str(uuid.uuid4())
            valid = _prepare(cursor, telegram_id, valid_key, valid_thread, "valid")
            cursor.execute(
                "insert into public.threads(thread_id,user_id,title,status,message_count,memory_status) "
                "values(%s,%s,'orphan','completed',1,'complete')", (orphan, user_id),
            )
            cursor.execute(
                "insert into public.thread_messages(thread_id,user_id,sequence,kind,payload) "
                "values(%s,%s,0,'assistant','{\"role\":\"assistant\",\"content\":\"preserved\"}')",
                (orphan, user_id),
            )
            # Recreate the pre-fix failure without weakening the deployed guard.
            cursor.execute("alter table public.thread_memory_heads disable trigger thread_memory_heads_validate")
            cursor.execute("update public.thread_memory_heads set latest_thread_id=%s where user_id=%s and conversation_key=%s", (orphan, user_id, key))
            cursor.execute("alter table public.thread_memory_heads enable trigger thread_memory_heads_validate")
            with pytest.raises(psycopg.errors.CheckViolation, match="head ownership or lineage mismatch"):
                with connection.transaction():
                    _prepare(cursor, telegram_id, key, str(uuid.uuid4()), "whens this trip?")
            # Apply the migration's actual data-repair statement, not a copy.
            cursor.execute(
                MIGRATION.read_text().split("begin;", 1)[1].split("create function")[0]
            )
            cursor.execute("select latest_thread_id,lineage_id from public.thread_memory_heads where user_id=%s and conversation_key=%s", (user_id, key))
            repaired = cursor.fetchone()
            assert repaired["latest_thread_id"] is None
            assert repaired["lineage_id"] != old[0]["lineage_id"]
            cursor.execute(
                "select latest_thread_id,lineage_id from public.thread_memory_heads "
                "where user_id=%s and conversation_key=%s", (user_id, valid_key),
            )
            assert cursor.fetchone() == {
                "latest_thread_id": valid_thread, "lineage_id": valid[0]["lineage_id"],
            }
            cursor.execute("select payload->>'content' as content from public.thread_messages where thread_id=%s", (orphan,))
            assert cursor.fetchone()["content"] == "preserved"
            fresh = _prepare(cursor, telegram_id, key, str(uuid.uuid4()), "fresh")
            assert fresh[0]["source_thread_id"] is None
            assert fresh[0]["lineage_id"] == repaired["lineage_id"]
        connection.rollback()


def test_head_guard_rejects_unregistered_wrong_owner_conversation_and_lineage():
    import psycopg
    from psycopg.rows import dict_row

    telegram_id, user_id, key = _new_scope("head_guard")
    other_telegram, other_user, other_key = _new_scope("other_head")
    with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            _identity(cursor, telegram_id, user_id)
            _identity(cursor, other_telegram, other_user)
            registered = str(uuid.uuid4())
            _prepare(cursor, telegram_id, key, registered, "first")
            orphan, other_conversation, other_owner = [str(uuid.uuid4()) for _ in range(3)]
            cursor.execute("insert into public.threads(thread_id,user_id,title,status,message_count) values(%s,%s,'orphan','completed',0)", (orphan, user_id))
            _prepare(cursor, telegram_id, other_key, other_conversation, "other conversation")
            _prepare(cursor, other_telegram, key, other_owner, "other owner")
            # Reset leaves the older registered thread in a different lineage.
            current = str(uuid.uuid4())
            _prepare(cursor, telegram_id, key, current, "reset", reset=True)
            for invalid in (orphan, other_conversation, other_owner, registered):
                with pytest.raises(psycopg.errors.CheckViolation, match="head must reference"):
                    with connection.transaction():
                        cursor.execute("set local role jarvis_app")
                        cursor.execute("update public.thread_memory_heads set latest_thread_id=%s where user_id=%s and conversation_key=%s", (invalid, user_id, key))
            cursor.execute("select latest_thread_id from public.thread_memory_heads where user_id=%s and conversation_key=%s", (user_id, key))
            assert cursor.fetchone()["latest_thread_id"] == current
        connection.rollback()


def test_snapshot_after_lookup_failure_or_out_of_order_completion_keeps_head(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    import psycopg
    from psycopg.rows import dict_row
    from agents.agent_api.app import thread_memory

    telegram_id, user_id, key = _new_scope("snapshot_head")
    older, newer, orphan = [str(uuid.uuid4()) for _ in range(3)]

    class Pool:
        @asynccontextmanager
        async def connection(self):
            async with await psycopg.AsyncConnection.connect(TEST_DSN) as connection:
                await connection.execute("set local role jarvis_app")
                yield connection

    monkeypatch.setattr(thread_memory, "get_async_pool", lambda: Pool())

    async def persist(thread):
        return await thread_memory.persist_thread_memory_async(
            thread_id=thread, canonical_user_id=str(user_id), conversation_key=key,
            terminal_status="completed",
            messages=[{"role": "assistant", "content": "snapshot preserved"}],
        )

    with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
        try:
            with connection.cursor() as cursor:
                _identity(cursor, telegram_id, user_id)
                _prepare(cursor, telegram_id, key, older, "older")
                _prepare(cursor, telegram_id, key, newer, "newer")
                cursor.execute("insert into public.threads(thread_id,user_id,title,status,message_count) values(%s,%s,'lookup failed','active',0)", (orphan, user_id))
            connection.commit()
            assert asyncio.run(persist(older)) is True
            assert asyncio.run(persist(orphan)) is True
            with connection.cursor() as cursor:
                cursor.execute("select latest_thread_id from public.thread_memory_heads where user_id=%s and conversation_key=%s", (user_id, key))
                assert cursor.fetchone()["latest_thread_id"] == newer
                cursor.execute("select memory_status,conversation_key from public.threads where thread_id=%s", (orphan,))
                assert cursor.fetchone() == {"memory_status": "complete", "conversation_key": None}
                rows = _prepare(cursor, telegram_id, key, str(uuid.uuid4()), "next")
                assert rows[0]["source_thread_id"] == newer
                assert "snapshot preserved" in str(rows)
        finally:
            connection.rollback()
            connection.execute("delete from public.users where id=%s", (user_id,))
            connection.commit()
