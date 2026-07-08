"""Optional transactional checks against an explicitly configured admin test DB."""

import os
import uuid

import pytest


TEST_DSN = os.getenv("JARVIS_ADMIN_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="JARVIS_ADMIN_TEST_POSTGRES_DSN is not configured",
)


def test_user_identity_preferences_and_audits_are_atomic():
    import psycopg

    telegram_id = int(f"8{uuid.uuid4().int % 10**12:012d}")
    with psycopg.connect(TEST_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select user_id, created
                from private.admin_upsert_user(%s, %s, %s, %s, %s, %s)
                """,
                (
                    telegram_id,
                    "admin_test",
                    "Admin Test",
                    "Asia/Singapore",
                    "en",
                    "admin:test",
                ),
            )
            user_id, created = cursor.fetchone()
            assert created is True
            cursor.execute(
                """
                select finding_type
                from private.admin_integrity_findings()
                where subject_id = %s
                """,
                (str(user_id),),
            )
            assert ("incomplete_profile",) in cursor.fetchall()

            cursor.execute(
                """
                select user_id, revision
                from private.admin_set_preferences(%s, 1, %s::jsonb, %s)
                """,
                (
                    telegram_id,
                    """{
                      "communication":{"tone":"neutral","verbosity":"balanced"},
                      "routing":{
                        "task_provider":"todoist",
                        "event_provider":"todoist",
                        "calendar_usage":"explicit_only"
                      },
                      "domains":{
                        "todoist":{},
                        "google_calendar":{"event_category_defaults":{}}
                      }
                    }""",
                    "admin:test",
                ),
            )
            assert cursor.fetchone() == (user_id, 1)
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
                ("preferences_updated", "admin:test"),
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

            cursor.execute(
                """
                select user_id
                from private.admin_upsert_user(%s, %s, %s, %s, %s, %s)
                """,
                (
                    telegram_id,
                    "credential_test",
                    "Credential Test",
                    "Asia/Singapore",
                    "en",
                    "admin:test",
                ),
            )
            user_id = cursor.fetchone()[0]

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
            cursor.execute(
                """
                select user_id
                from private.admin_upsert_user(%s, %s, %s, %s, %s, %s)
                """,
                (
                    telegram_id,
                    "quota_test",
                    "Quota Test",
                    "Asia/Singapore",
                    "en",
                    "admin:test",
                ),
            )
            user_id = cursor.fetchone()[0]

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
