"""Optional transactional checks against an explicitly configured admin test DB."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest


TEST_DSN = os.getenv("JARVIS_ADMIN_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="JARVIS_ADMIN_TEST_POSTGRES_DSN is not configured",
)


def _onboard(cursor, telegram_id: int, name: str):
    cursor.execute(
        """
        select user_id, created
        from private.onboard_user(
          %s, %s, 'todoist', 'todoist', %s,
          'Asia/Singapore', 'en', 'neutral', 'balanced',
          'explicit_only', 'admin:test'
        )
        """,
        (telegram_id, name, f"test_{telegram_id}"),
    )
    return cursor.fetchone()


def test_user_identity_policy_and_audits_are_atomic():
    import psycopg

    telegram_id = int(f"8{uuid.uuid4().int % 10**12:012d}")
    with psycopg.connect(TEST_DSN) as connection:
        with connection.cursor() as cursor:
            user_id, created = _onboard(cursor, telegram_id, "Admin Test")
            assert created is True
            cursor.execute(
                "select status, telegram_verified_at, custom_instructions from public.users where id = %s",
                (user_id,),
            )
            original_status, original_verified_at, original_instructions = cursor.fetchone()
            duplicate_user_id, duplicate_created = _onboard(
                cursor, telegram_id, "Ignored Duplicate Name"
            )
            assert (duplicate_user_id, duplicate_created) == (user_id, False)
            cursor.execute(
                "select status, telegram_verified_at, custom_instructions, display_name from public.users where id = %s",
                (user_id,),
            )
            assert cursor.fetchone() == (
                original_status,
                original_verified_at,
                original_instructions,
                "Admin Test",
            )
            cursor.execute(
                """
                select finding_type
                from private.admin_integrity_findings()
                where subject_id = %s
                """,
                (str(user_id),),
            )
            assert cursor.fetchall() == []
            cursor.execute(
                "select count(*) from public.integration_connections where user_id = %s",
                (user_id,),
            )
            assert cursor.fetchone() == (0,)

            cursor.execute(
                """
                select user_id, policy_revision
                from private.admin_set_runtime_policy(%s, %s, %s, %s, %s, %s)
                """,
                (
                    telegram_id,
                    "gpt-test",
                    "high",
                    9,
                    False,
                    "admin:test",
                ),
            )
            assert cursor.fetchone() == (user_id, 2)
            cursor.execute(
                """
                select user_id, updated, instruction_length
                from private.admin_set_custom_instructions(%s, %s, %s)
                """,
                (
                    telegram_id,
                    "Apply the task or event label according to item type.",
                    "admin:test",
                ),
            )
            assert cursor.fetchone() == (user_id, True, 53)
            cursor.execute(
                """
                select policy.forced_model, policy.forced_reasoning_effort,
                       policy.max_agent_turns, policy.allow_mutations,
                       app_user.custom_instructions
                from public.users app_user
                join private.user_runtime_policies policy on policy.user_id = app_user.id
                where app_user.id = %s
                """,
                (user_id,),
            )
            assert cursor.fetchone() == (
                "gpt-test",
                "high",
                9,
                False,
                "Apply the task or event label according to item type.",
            )
            cursor.execute(
                """
                select event_type, actor
                from public.integration_events
                where user_id = %s
                order by id
                """,
                (user_id,),
            )
            assert cursor.fetchall() == [
                ("user_created", "admin:test"),
                ("runtime_policy_updated", "admin:test"),
                ("custom_instructions_updated", "admin:test"),
            ]
        connection.rollback()


def test_credential_versions_revocation_and_reconnect_are_atomic():
    import psycopg

    telegram_id = int(f"8{uuid.uuid4().int % 10**12:012d}")
    with psycopg.connect(TEST_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select vault.create_secret(%s, %s, %s)",
                (
                    "orphan-test-secret",
                    f"jarvis:test:orphan:{uuid.uuid4()}",
                    "transactional admin test",
                ),
            )
            orphan_id = cursor.fetchone()[0]
            cursor.execute(
                """
                select finding_type
                from private.admin_integrity_findings()
                where subject_id = %s
                """,
                (str(orphan_id),),
            )
            assert cursor.fetchone() == ("orphaned_vault_secret",)

            user_id = _onboard(cursor, telegram_id, "Credential Test")[0]

            def store(secret, mode):
                cursor.execute(
                    """
                    select connection_id, credential_version
                    from private.admin_store_integration(
                      %s, 'todoist', %s, 'Test Todoist', null,
                      '{}'::text[], '{}'::jsonb, null, %s, 'admin:test'
                    )
                    """,
                    (telegram_id, secret, mode),
                )
                return cursor.fetchone()

            connection_id, version = store("test-secret-one", "import")
            assert version == 1
            cursor.execute(
                "select vault_secret_id from public.integration_connections where id = %s",
                (connection_id,),
            )
            original_secret_id = cursor.fetchone()[0]

            assert store("test-secret-two", "rotate") == (connection_id, 2)
            cursor.execute(
                "select private.admin_set_integration_state(%s, 'todoist', 'revoke', 'admin:test')",
                (telegram_id,),
            )
            assert cursor.fetchone()[0] == connection_id
            cursor.execute(
                """
                select status, is_enabled, vault_secret_id
                from public.integration_connections where id = %s
                """,
                (connection_id,),
            )
            assert cursor.fetchone() == ("revoked", False, original_secret_id)

            assert store("test-secret-three", "reconnect") == (connection_id, 3)
            cursor.execute(
                """
                select status, is_enabled, vault_secret_id
                from public.integration_connections where id = %s
                """,
                (connection_id,),
            )
            assert cursor.fetchone() == ("connected", True, original_secret_id)
            cursor.execute(
                """
                select count(*)
                from public.integration_events
                where user_id = %s and actor = 'admin:test'
                """,
                (user_id,),
            )
            assert cursor.fetchone()[0] >= 5
        connection.rollback()


def test_thread_quota_function_allows_100_denies_101st_and_resets():
    import psycopg

    telegram_id = int(f"8{uuid.uuid4().int % 10**12:012d}")
    with psycopg.connect(TEST_DSN) as connection:
        with connection.cursor() as cursor:
            user_id = _onboard(cursor, telegram_id, "Quota Test")[0]

            allowed_count = 0
            for _ in range(100):
                cursor.execute(
                    "select allowed, threads_used, thread_limit from public.try_consume_thread_quota(%s)",
                    (telegram_id,),
                )
                allowed, threads_used, thread_limit = cursor.fetchone()
                assert allowed is True
                assert 1 <= threads_used <= 100
                assert thread_limit == 100
                allowed_count += 1
            assert allowed_count == 100

            cursor.execute(
                """
                select allowed, threads_used, thread_limit
                from public.try_consume_thread_quota(%s)
                """,
                (telegram_id,),
            )
            assert cursor.fetchone() == (False, 100, 100)

            cursor.execute(
                """
                update public.rate_limits
                set daily_threads_used = 100,
                    reset_at = now() - interval '1 second'
                where user_id = %s
                """,
                (user_id,),
            )
            cursor.execute(
                """
                select allowed, threads_used, thread_limit
                from public.try_consume_thread_quota(%s)
                """,
                (telegram_id,),
            )
            assert cursor.fetchone() == (True, 1, 100)

            cursor.execute(
                """
                select has_function_privilege(
                  'jarvis_runtime',
                  'public.try_consume_thread_quota(bigint)',
                  'EXECUTE'
                )
                """
            )
            assert cursor.fetchone() == (True,)
        connection.rollback()


def test_concurrent_onboarding_creates_one_user_and_one_audit_event():
    import psycopg

    telegram_id = int(f"8{uuid.uuid4().int % 10**12:012d}")
    barrier = Barrier(2)

    def onboard_once(_index):
        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                barrier.wait(timeout=10)
                return _onboard(cursor, telegram_id, "Concurrent Test")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(onboard_once, range(2)))

        assert results[0][0] == results[1][0]
        assert sorted(result[1] for result in results) == [False, True]

        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select id from public.users where telegram_id = %s",
                    (telegram_id,),
                )
                assert cursor.fetchall() == [(results[0][0],)]
                cursor.execute(
                    """
                    select count(*)
                    from public.integration_events
                    where user_id = %s and event_type = 'user_created'
                    """,
                    (results[0][0],),
                )
                assert cursor.fetchone() == (1,)
    finally:
        with psycopg.connect(TEST_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from public.integration_events where user_id = (select id from public.users where telegram_id = %s)",
                    (telegram_id,),
                )
                cursor.execute(
                    "delete from public.users where telegram_id = %s",
                    (telegram_id,),
                )
