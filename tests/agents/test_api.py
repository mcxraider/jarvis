import unittest
import json
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from agents.api import app
from agents.agent_api.app.checkpointing.postgres import create_postgres_checkpointer
from agents.agent_api.app.config import load_settings
from agents.agent_api.app.service import InMemorySaver, create_default_checkpointer


class JarvisApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_health_detail_ok(self) -> None:
        with patch(
            "agents.agent_api.app.api.routes.health._check_deepseek",
            return_value={"ok": True, "detail": "reachable"},
        ), patch(
            "agents.agent_api.app.api.routes.health._check_todoist",
            return_value={"ok": True, "detail": "5 project(s)"},
        ) as todoist:
            response = self.client.get("/health/detail", params={"telegram_user_id": 123})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("model", body)
        self.assertEqual(body["checks"]["deepseek"], {"ok": True, "detail": "reachable"})
        self.assertEqual(body["checks"]["todoist"], {"ok": True, "detail": "5 project(s)"})
        # The requesting user's id is forwarded so the Todoist token can be resolved.
        todoist.assert_called_once_with(123)

    def test_health_detail_degraded_when_a_check_fails(self) -> None:
        with patch(
            "agents.agent_api.app.api.routes.health._check_deepseek",
            return_value={"ok": True, "detail": "reachable"},
        ), patch(
            "agents.agent_api.app.api.routes.health._check_todoist",
            return_value={"ok": False, "detail": "no token for user"},
        ):
            response = self.client.get("/health/detail")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "degraded")
        self.assertFalse(body["checks"]["todoist"]["ok"])

    def test_invoke_completed(self) -> None:
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value={
                "thread_id": "thread-1",
                "interrupted": False,
                "final_response": "Done.",
                "tool_results": [{"tool_name": "add_todoist_task"}],
                "error": "",
            },
        ) as run:
            response = self.client.post(
                "/invoke",
                json={
                    "message": "add milk",
                    "user_id": "jerry",
                    "telegram_user_id": 123,
                    "telegram_username": "tester",
                    "telegram_first_name": "Test",
                    "request_id": "tg_test",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "completed",
                "thread_id": "thread-1",
                "response": "Done.",
                "interrupt": None,
                "tool_results": [{"tool_name": "add_todoist_task"}],
                "error": None,
            },
        )
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["user_prompt"], "add milk")
        self.assertEqual(run.call_args.kwargs["user_id"], "jerry")
        self.assertEqual(run.call_args.kwargs["request_source"], "telegram")
        identity = run.call_args.kwargs["identity"]
        self.assertEqual(identity.telegram_id, 123)
        self.assertEqual(identity.username, "tester")
        self.assertEqual(run.call_args.kwargs["request_id"], "tg_test")

    def test_invoke_interrupted(self) -> None:
        interrupt = {
            "type": "clarification",
            "question": "Which task should I update?",
            "thread_id": "thread-hitl",
        }
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value={
                "thread_id": "thread-hitl",
                "interrupted": True,
                "interrupt_payload": interrupt,
                "tool_results": [],
                "error": "",
            },
        ):
            response = self.client.post(
                "/invoke",
                json={"message": "update my task", "user_id": "jerry"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "interrupted")
        self.assertEqual(response.json()["thread_id"], "thread-hitl")
        self.assertEqual(response.json()["response"], "Which task should I update?")
        self.assertEqual(response.json()["interrupt"], interrupt)

    def test_invoke_stream_emits_progress_and_final_response(self) -> None:
        def fake_run(**kwargs):
            tracer = kwargs["tracer"]
            tracer.event("runtime.start", "Starting graph invocation.", resuming=False)
            tracer.event("graph.agent", "Entering agent node.", turn=1, max_turns=20)
            tracer.event("agent.request", "Calling DeepSeek chat completions.")
            tracer.event("agent.response", "Received assistant message.", tool_calls=1)
            tracer.event("graph.route", "Agent node completed.", next="tools")
            tracer.event("graph.tools", "Entering tools node.", tool_calls=1)
            tracer.event("tools.batch", "Executing tool call batch.", count=1)
            tracer.event("tool.done", "Tool call completed.")
            tracer.event("runtime.done", "Graph invocation completed.", interrupted=False)
            return {
                "thread_id": "thread-1",
                "interrupted": False,
                "final_response": "Done.",
                "tool_results": [],
                "error": "",
            }

        with patch("agents.agent_api.app.api.routes.invoke.run_jarvis", side_effect=fake_run):
            response = self.client.post(
                "/invoke/stream",
                json={"message": "show today", "user_id": "jerry"},
            )

        self.assertEqual(response.status_code, 200)
        events = [json.loads(line) for line in response.text.strip().splitlines()]
        self.assertEqual(events[-1]["type"], "final")
        self.assertEqual(events[-1]["response"]["response"], "Done.")
        progress_messages = [event["message"] for event in events if event["type"] == "progress"]
        self.assertIn("Agent started and opened a Jarvis run", progress_messages)
        self.assertIn("Calling Todoist (1 request(s))", progress_messages)

    def test_resume_stream_emits_resume_progress(self) -> None:
        def fake_run(**kwargs):
            tracer = kwargs["tracer"]
            tracer.event("runtime.start", "Starting graph invocation.", resuming=True)
            tracer.event("runtime.done", "Graph invocation completed.", interrupted=False)
            return {
                "thread_id": "thread-hitl",
                "interrupted": False,
                "final_response": "Updated.",
                "tool_results": [],
                "error": "",
            }

        with patch("agents.agent_api.app.api.routes.resume.run_jarvis", side_effect=fake_run):
            response = self.client.post(
                "/resume/stream",
                json={
                    "thread_id": "thread-hitl",
                    "message": "the dentist task",
                    "user_id": "jerry",
                },
            )

        self.assertEqual(response.status_code, 200)
        events = [json.loads(line) for line in response.text.strip().splitlines()]
        self.assertEqual(events[0]["message"], "Resuming the previous Jarvis run")
        self.assertEqual(events[-1]["response"]["response"], "Updated.")

    def test_resume_uses_thread_id_and_reply(self) -> None:
        with patch(
            "agents.agent_api.app.api.routes.resume.run_jarvis",
            return_value={
                "thread_id": "thread-hitl",
                "interrupted": False,
                "final_response": "Updated.",
                "tool_results": [],
                "error": "",
            },
        ) as run:
            response = self.client.post(
                "/resume",
                json={
                    "thread_id": "thread-hitl",
                    "message": "the dentist task",
                    "user_id": "jerry",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual(response.json()["response"], "Updated.")
        self.assertEqual(run.call_args.kwargs["thread_id"], "thread-hitl")
        self.assertEqual(run.call_args.kwargs["clarification_reply"], "the dentist task")
        self.assertEqual(run.call_args.kwargs["request_source"], "api")

    def test_invoke_bulk_runs_each_non_empty_message(self) -> None:
        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            side_effect=[
                {
                    "thread_id": "thread-1",
                    "interrupted": False,
                    "final_response": "First done.",
                    "tool_results": [],
                    "error": "",
                },
                {
                    "thread_id": "thread-2",
                    "interrupted": False,
                    "final_response": "Second done.",
                    "tool_results": [],
                    "error": "",
                },
            ],
        ) as run:
            response = self.client.post(
                "/invoke-bulk",
                json={
                    "messages": ["first prompt", "", " second prompt "],
                    "user_id": "jerry",
                    "max_agent_turns": 3,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["response"] for item in response.json()["results"]],
            ["First done.", "Second done."],
        )
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].kwargs["user_prompt"], "first prompt")
        self.assertEqual(run.call_args_list[1].kwargs["user_prompt"], "second prompt")
        self.assertEqual(run.call_args_list[0].kwargs["max_agent_turns"], 3)
        self.assertTrue(run.call_args_list[0].kwargs["allow_mutations"])
        self.assertEqual(run.call_args_list[0].kwargs["request_source"], "api")

    def test_config_defaults_to_memory_checkpointing_without_postgres_dsn(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "JARVIS_CHECKPOINT_BACKEND": "",
                "JARVIS_POSTGRES_DSN": "",
                "DATABASE_URL": "",
            },
            clear=False,
        ):
            settings = load_settings()

        self.assertEqual(settings.checkpoint_backend, "memory")

    def test_config_selects_postgres_when_dsn_exists(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "JARVIS_POSTGRES_DSN": "postgresql://jarvis:test@localhost:5432/jarvis",
                "JARVIS_CHECKPOINT_BACKEND": "",
            },
            clear=False,
        ):
            settings = load_settings()

        self.assertEqual(settings.checkpoint_backend, "postgres")

    def test_checkpoint_setup_defaults_to_disabled(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("JARVIS_RUN_CHECKPOINT_SETUP", None)
            settings = load_settings()

        self.assertFalse(settings.run_checkpoint_setup)

    def test_checkpoint_setup_parses_boolean_environment_values(self) -> None:
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value), patch.dict(
                "os.environ",
                {"JARVIS_RUN_CHECKPOINT_SETUP": value},
                clear=False,
            ):
                self.assertTrue(load_settings().run_checkpoint_setup)

        for value in ("0", "false", "no", "off", ""):
            with self.subTest(value=value), patch.dict(
                "os.environ",
                {"JARVIS_RUN_CHECKPOINT_SETUP": value},
                clear=False,
            ):
                self.assertFalse(load_settings().run_checkpoint_setup)

    def test_postgres_checkpointer_skips_setup_by_default(self) -> None:
        checkpointer = self._create_mock_postgres_checkpointer()

        checkpointer.setup.assert_not_called()

    def test_postgres_checkpointer_runs_explicit_setup(self) -> None:
        checkpointer = self._create_mock_postgres_checkpointer(run_setup=True)

        checkpointer.setup.assert_called_once_with()

    @staticmethod
    def _create_mock_postgres_checkpointer(*, run_setup: bool = False) -> MagicMock:
        checkpointer = MagicMock()
        postgres_module = ModuleType("langgraph.checkpoint.postgres")
        postgres_module.PostgresSaver = MagicMock(return_value=checkpointer)
        pool_module = ModuleType("psycopg_pool")
        pool_module.ConnectionPool = MagicMock()

        with patch.dict(
            sys.modules,
            {
                "langgraph.checkpoint.postgres": postgres_module,
                "psycopg_pool": pool_module,
            },
        ):
            result = create_postgres_checkpointer(
                "postgresql://jarvis:test@localhost:5432/jarvis",
                run_setup=run_setup,
            )

        assert result is checkpointer
        pool_module.ConnectionPool.assert_called_once_with(
            conninfo="postgresql://jarvis:test@localhost:5432/jarvis",
            kwargs={"autocommit": True, "prepare_threshold": None},
        )
        return checkpointer

    def test_default_checkpointer_forwards_postgres_setup_setting(self) -> None:
        with patch(
            "agents.agent_api.app.checkpointing.settings",
            SimpleNamespace(
                checkpoint_backend="postgres",
                postgres_dsn="postgresql://jarvis:test@localhost:5432/jarvis",
                redis_url=None,
                run_checkpoint_setup=True,
            ),
        ), patch(
            "agents.agent_api.app.checkpointing.create_postgres_checkpointer",
            return_value=MagicMock(),
        ) as create:
            create_default_checkpointer()

        create.assert_called_once_with(
            "postgresql://jarvis:test@localhost:5432/jarvis",
            run_setup=True,
        )

    def test_default_checkpointer_can_use_in_memory_backend(self) -> None:
        with patch(
            "agents.agent_api.app.checkpointing.settings",
            SimpleNamespace(
                checkpoint_backend="memory",
                postgres_dsn=None,
                redis_url=None,
                run_checkpoint_setup=False,
            ),
        ):
            checkpointer = create_default_checkpointer()

        self.assertIsInstance(checkpointer, InMemorySaver)


class RouteGuardTests(unittest.TestCase):
    """Tests that ownership/rate-limit guards propagate HTTP errors through routes."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_resume_returns_403_on_ownership_violation(self) -> None:
        from fastapi import HTTPException

        with patch(
            "agents.agent_api.app.api.thread_ownership.validate_thread_ownership",
            side_effect=HTTPException(status_code=403, detail="Thread belongs to a different user."),
        ):
            response = self.client.post(
                "/resume",
                json={
                    "message": "yes",
                    "user_id": "jerry",
                    "thread_id": "stolen-thread",
                    "telegram_user_id": 999,
                    "request_id": "req-1",
                },
            )
        self.assertEqual(response.status_code, 403)

    def test_resume_returns_429_on_rate_limit(self) -> None:
        from fastapi import HTTPException

        with patch(
            "agents.agent_api.app.api.thread_ownership.validate_thread_ownership",
        ), patch(
            "agents.agent_api.app.api.rate_limit.check_rate_limit",
            side_effect=HTTPException(
                status_code=429,
                detail="Daily request limit exceeded.",
                headers={"Retry-After": "3600"},
            ),
        ):
            response = self.client.post(
                "/resume",
                json={
                    "message": "yes",
                    "user_id": "jerry",
                    "thread_id": "t-1",
                    "telegram_user_id": 42,
                    "request_id": "req-2",
                },
            )
        self.assertEqual(response.status_code, 429)

    def test_invoke_returns_429_on_rate_limit(self) -> None:
        from fastapi import HTTPException

        with patch(
            "agents.agent_api.app.api.rate_limit.check_rate_limit",
            side_effect=HTTPException(
                status_code=429,
                detail="Daily request limit exceeded.",
                headers={"Retry-After": "3600"},
            ),
        ):
            response = self.client.post(
                "/invoke",
                json={
                    "message": "add milk",
                    "user_id": "jerry",
                    "telegram_user_id": 42,
                    "request_id": "req-3",
                },
            )
        self.assertEqual(response.status_code, 429)


    def test_invoke_returns_403_on_ownership_violation_with_thread_id(self) -> None:
        from fastapi import HTTPException

        with patch(
            "agents.agent_api.app.api.routes.invoke.validate_thread_ownership",
            side_effect=HTTPException(status_code=403, detail="Thread belongs to a different user."),
        ):
            response = self.client.post(
                "/invoke",
                json={
                    "message": "add milk",
                    "user_id": "jerry",
                    "thread_id": "stolen-thread",
                    "telegram_user_id": 999,
                    "request_id": "req-4",
                },
            )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
