import unittest
import json
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from agents.api import app, create_app
from agents.agent_api.app import run_logging
from agents.agent_api.app.checkpointing.postgres import create_postgres_checkpointer
from agents.agent_api.app.config import load_settings
from agents.agent_api.app.service import InMemorySaver, create_default_checkpointer


class JarvisApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        # Lifespan tests intentionally exercise the process-final logger shutdown.
        # Restore its worker so later tests do not inherit a closed executor.
        run_logging.reset_log_writer()

    def test_health(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_fastapi_lifespan_drains_run_logs_on_shutdown(self) -> None:
        with patch("agents.agent_api.app.db.verify_database_runtime"), \
            patch("agents.agent_api.app.db.close_pool"), \
            patch(
                "agents.agent_api.app.api.routes.invoke.drain_stream_workers",
                new_callable=AsyncMock,
            ) as drain_workers, \
            patch("agents.agent_api.app.run_logging.shutdown_run_logs") as shutdown, \
            patch(
                "agents.agent_api.app.tools.todoist.client.close_todoist_http_client"
            ) as close_todoist, \
            patch(
                "agents.agent_api.app.tools.todoist.client.close_todoist_async_http_client",
                new_callable=AsyncMock,
            ) as close_todoist_async:
            with TestClient(create_app()) as client:
                response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        close_todoist.assert_called_once_with()
        close_todoist_async.assert_awaited_once_with()
        drain_workers.assert_awaited_once_with(timeout=5.0)
        shutdown.assert_called_once_with(timeout=5.0)

    def test_fastapi_lifespan_owns_async_checkpointer_and_pool(self) -> None:
        lifecycle_order = []
        pool = object()
        saver = object()

        with patch(
            "agents.agent_api.app.db.open_async_pool",
            new_callable=AsyncMock,
            side_effect=lambda: lifecycle_order.append("async_pool_open") or pool,
        ) as open_pool, patch(
            "agents.agent_api.app.checkpointing.initialize_async_checkpointer",
            new_callable=AsyncMock,
            side_effect=lambda received: lifecycle_order.append("checkpointer") or saver,
        ) as initialize, patch(
            "agents.agent_api.app.graph.builder.get_or_compile_graph",
            side_effect=lambda received: lifecycle_order.append("graph") or MagicMock(),
        ) as compile_graph, patch(
            "agents.agent_api.app.db.verify_database_runtime",
            side_effect=lambda: lifecycle_order.append("sync_readiness"),
        ), patch(
            "agents.agent_api.app.api.routes.invoke.drain_stream_workers",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "agents.agent_api.app.db.close_pool",
            side_effect=lambda: lifecycle_order.append("sync_pool_close"),
        ), patch(
            "agents.agent_api.app.db.close_async_pool",
            new_callable=AsyncMock,
            side_effect=lambda: lifecycle_order.append("async_pool_close"),
        ) as close_pool:
            created_app = create_app()
            with TestClient(created_app) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertIs(created_app.state.async_checkpointer, saver)

        self.assertEqual(
            lifecycle_order[:4],
            ["async_pool_open", "sync_readiness", "checkpointer", "graph"],
        )
        self.assertEqual(lifecycle_order[-2:], ["sync_pool_close", "async_pool_close"])
        self.assertIsNone(created_app.state.async_checkpointer)
        open_pool.assert_awaited_once_with()
        initialize.assert_awaited_once_with(pool)
        compile_graph.assert_called_once_with(saver)
        close_pool.assert_awaited_once_with()

    def test_fastapi_lifespan_rolls_back_async_resources_on_startup_failure(
        self,
    ) -> None:
        saver = object()
        with patch(
            "agents.agent_api.app.db.open_async_pool",
            new_callable=AsyncMock,
            return_value=object(),
        ), patch(
            "agents.agent_api.app.checkpointing.initialize_async_checkpointer",
            new_callable=AsyncMock,
            return_value=saver,
        ), patch(
            "agents.agent_api.app.graph.builder.get_or_compile_graph"
        ), patch(
            "agents.agent_api.app.db.verify_database_runtime",
            side_effect=RuntimeError("readiness failed"),
        ), patch(
            "agents.agent_api.app.api.routes.invoke.drain_stream_workers",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "agents.agent_api.app.db.close_pool"
        ), patch(
            "agents.agent_api.app.db.close_async_pool",
            new_callable=AsyncMock,
        ) as close_pool:
            created_app = create_app()
            with self.assertRaisesRegex(RuntimeError, "readiness failed"):
                with TestClient(created_app):
                    pass

        close_pool.assert_awaited_once_with()
        self.assertIsNone(getattr(created_app.state, "async_checkpointer", None))

    def test_fastapi_lifespan_attempts_all_cleanup_after_close_failure(self) -> None:
        with patch("agents.agent_api.app.db.verify_database_runtime"), \
            patch("agents.agent_api.app.db.close_pool") as close_pool, \
            patch("agents.agent_api.app.run_logging.shutdown_run_logs") as shutdown, \
            patch(
                "agents.agent_api.app.tools.todoist.client.close_todoist_http_client",
                side_effect=RuntimeError("sync close failed"),
            ) as close_todoist, \
            patch(
                "agents.agent_api.app.tools.todoist.client.close_todoist_async_http_client",
                new_callable=AsyncMock,
            ) as close_todoist_async:
            with self.assertRaisesRegex(RuntimeError, "sync close failed"):
                with TestClient(create_app()) as client:
                    self.assertEqual(client.get("/health").status_code, 200)

        shutdown.assert_called_once_with(timeout=5.0)
        close_todoist.assert_called_once_with()
        close_todoist_async.assert_awaited_once_with()
        close_pool.assert_called_once_with()

    def test_fastapi_lifespan_reports_undrained_blocking_offloads(self) -> None:
        with patch("agents.agent_api.app.db.verify_database_runtime"), patch(
            "agents.agent_api.app.api.routes.invoke.drain_stream_workers",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "agents.agent_api.app.async_offload.drain_offloads",
            new_callable=AsyncMock,
            return_value=False,
        ) as drain_offloads, patch(
            "agents.agent_api.app.db.close_pool"
        ) as close_pool, patch(
            "agents.agent_api.app.db.close_async_pool",
            new_callable=AsyncMock,
        ) as close_async_pool:
            with self.assertRaisesRegex(
                TimeoutError,
                "Blocking async compatibility work did not drain",
            ):
                with TestClient(create_app()) as client:
                    self.assertEqual(client.get("/health").status_code, 200)

        drain_offloads.assert_awaited_once_with(5.0)
        close_pool.assert_not_called()
        close_async_pool.assert_not_awaited()

    def test_fastapi_lifespan_leaves_resources_open_for_undrained_producers(
        self,
    ) -> None:
        with patch("agents.agent_api.app.db.verify_database_runtime"), patch(
            "agents.agent_api.app.api.routes.invoke.drain_stream_workers",
            new_callable=AsyncMock,
            return_value=False,
        ) as drain_workers, patch(
            "agents.agent_api.app.async_offload.drain_offloads",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "agents.agent_api.app.graph.nodes.orchestrator.close_shared_agent_client"
        ) as close_agent, patch(
            "agents.agent_api.app.router.client.close_shared_router_client"
        ) as close_router, patch(
            "agents.agent_api.app.graph.nodes.summarize.close_shared_summarizer_client"
        ) as close_summarizer, patch(
            "agents.agent_api.app.graph.nodes.orchestrator.close_shared_async_agent_client",
            new_callable=AsyncMock,
        ) as close_agent_async, patch(
            "agents.agent_api.app.router.client.close_shared_async_router_openai_client",
            new_callable=AsyncMock,
        ) as close_router_async, patch(
            "agents.agent_api.app.graph.nodes.summarize.close_shared_async_summarizer_client",
            new_callable=AsyncMock,
        ) as close_summarizer_async, patch(
            "agents.agent_api.app.graph.builder.reset_compiled_graphs"
        ) as reset_graphs, patch(
            "agents.agent_api.app.checkpointing.reset_async_checkpointer"
        ) as reset_checkpointer, patch(
            "agents.agent_api.app.async_offload.reset_offload_limiters"
        ) as reset_offloads, patch(
            "agents.agent_api.app.api.active_runs.reset_active_run_registry"
        ) as reset_active_runs, patch(
            "agents.agent_api.app.run_logging.shutdown_run_logs"
        ) as shutdown_logs, patch(
            "agents.agent_api.app.tools.todoist.client.close_todoist_http_client"
        ) as close_todoist, patch(
            "agents.agent_api.app.tools.todoist.client.close_todoist_async_http_client",
            new_callable=AsyncMock,
        ) as close_todoist_async, patch(
            "agents.agent_api.app.db.close_pool"
        ) as close_pool, patch(
            "agents.agent_api.app.db.close_async_pool",
            new_callable=AsyncMock,
        ) as close_async_pool:
            with self.assertRaisesRegex(
                TimeoutError,
                "Active streaming workers did not drain",
            ):
                with TestClient(create_app()) as client:
                    self.assertEqual(client.get("/health").status_code, 200)

        drain_workers.assert_awaited_once_with(timeout=5.0)
        close_agent.assert_not_called()
        close_router.assert_not_called()
        close_summarizer.assert_not_called()
        close_agent_async.assert_not_awaited()
        close_router_async.assert_not_awaited()
        close_summarizer_async.assert_not_awaited()
        reset_graphs.assert_not_called()
        reset_checkpointer.assert_not_called()
        reset_offloads.assert_not_called()
        reset_active_runs.assert_not_called()
        shutdown_logs.assert_not_called()
        close_todoist.assert_not_called()
        close_todoist_async.assert_not_awaited()
        close_pool.assert_not_called()
        close_async_pool.assert_not_awaited()

    def test_fastapi_lifespan_cleanup_order_drains_workers_first(self) -> None:
        cleanup_order = []

        with patch("agents.agent_api.app.db.verify_database_runtime"), patch(
            "agents.agent_api.app.api.routes.invoke.drain_stream_workers",
            new_callable=AsyncMock,
            side_effect=lambda **_kwargs: cleanup_order.append("workers") or True,
        ), patch(
            "agents.agent_api.app.run_logging.shutdown_run_logs",
            side_effect=lambda **_kwargs: cleanup_order.append("logs"),
        ), patch(
            "agents.agent_api.app.tools.todoist.client.close_todoist_http_client",
            side_effect=lambda: cleanup_order.append("todoist_sync"),
        ), patch(
            "agents.agent_api.app.tools.todoist.client.close_todoist_async_http_client",
            new_callable=AsyncMock,
            side_effect=lambda: cleanup_order.append("todoist_async"),
        ), patch(
            "agents.agent_api.app.db.close_pool",
            side_effect=lambda: cleanup_order.append("database"),
        ):
            with TestClient(create_app()) as client:
                self.assertEqual(client.get("/health").status_code, 200)

        self.assertEqual(
            cleanup_order,
            ["workers", "logs", "todoist_sync", "todoist_async", "database"],
        )

    def test_fastapi_lifespan_closes_all_llm_resources_before_other_resources(
        self,
    ) -> None:
        cleanup_order = []

        with patch("agents.agent_api.app.db.verify_database_runtime"), patch(
            "agents.agent_api.app.api.routes.invoke.drain_stream_workers",
            new_callable=AsyncMock,
            side_effect=lambda **_kwargs: cleanup_order.append("workers") or True,
        ), patch(
            "agents.agent_api.app.graph.nodes.orchestrator.close_shared_agent_client",
            side_effect=lambda: cleanup_order.append("agent_sync"),
        ) as close_agent, patch(
            "agents.agent_api.app.router.client.close_shared_router_client",
            side_effect=lambda: cleanup_order.append("router_sync"),
        ) as close_router, patch(
            "agents.agent_api.app.graph.nodes.summarize.close_shared_summarizer_client",
            side_effect=lambda: cleanup_order.append("summarizer_sync"),
        ) as close_summarizer, patch(
            "agents.agent_api.app.graph.nodes.orchestrator.close_shared_async_agent_client",
            new_callable=AsyncMock,
            side_effect=lambda: cleanup_order.append("agent_async"),
        ) as close_agent_async, patch(
            "agents.agent_api.app.router.client.close_shared_async_router_openai_client",
            new_callable=AsyncMock,
            side_effect=lambda: cleanup_order.append("router_async"),
        ) as close_router_async, patch(
            "agents.agent_api.app.graph.nodes.summarize.close_shared_async_summarizer_client",
            new_callable=AsyncMock,
            side_effect=lambda: cleanup_order.append("summarizer_async"),
        ) as close_summarizer_async, patch(
            "agents.agent_api.app.run_logging.shutdown_run_logs",
            side_effect=lambda **_kwargs: cleanup_order.append("logs"),
        ), patch(
            "agents.agent_api.app.tools.todoist.client.close_todoist_http_client",
            side_effect=lambda: cleanup_order.append("todoist_sync"),
        ), patch(
            "agents.agent_api.app.tools.todoist.client.close_todoist_async_http_client",
            new_callable=AsyncMock,
            side_effect=lambda: cleanup_order.append("todoist_async"),
        ), patch(
            "agents.agent_api.app.db.close_pool",
            side_effect=lambda: cleanup_order.append("database"),
        ):
            with TestClient(create_app()) as client:
                self.assertEqual(client.get("/health").status_code, 200)

        self.assertEqual(
            cleanup_order,
            [
                "workers",
                "agent_sync",
                "router_sync",
                "summarizer_sync",
                "agent_async",
                "router_async",
                "summarizer_async",
                "logs",
                "todoist_sync",
                "todoist_async",
                "database",
            ],
        )
        close_agent.assert_called_once_with()
        close_router.assert_called_once_with()
        close_summarizer.assert_called_once_with()
        close_agent_async.assert_awaited_once_with()
        close_router_async.assert_awaited_once_with()
        close_summarizer_async.assert_awaited_once_with()

    def test_fastapi_lifespan_preserves_multiple_cleanup_failures(self) -> None:
        with patch("agents.agent_api.app.db.verify_database_runtime"), patch(
            "agents.agent_api.app.api.routes.invoke.drain_stream_workers",
            new_callable=AsyncMock,
        ) as drain_workers, patch(
            "agents.agent_api.app.run_logging.shutdown_run_logs",
            side_effect=RuntimeError("logs failed"),
        ) as shutdown, patch(
            "agents.agent_api.app.tools.todoist.client.close_todoist_http_client",
            side_effect=RuntimeError("sync Todoist failed"),
        ) as close_todoist, patch(
            "agents.agent_api.app.tools.todoist.client.close_todoist_async_http_client",
            new_callable=AsyncMock,
            side_effect=RuntimeError("async Todoist failed"),
        ) as close_todoist_async, patch(
            "agents.agent_api.app.db.close_pool",
            side_effect=RuntimeError("database failed"),
        ) as close_pool, patch(
            "agents.agent_api.app.db.close_async_pool",
            new_callable=AsyncMock,
            side_effect=RuntimeError("async database failed"),
        ) as close_async_pool:
            with self.assertRaises(ExceptionGroup) as raised:
                with TestClient(create_app()) as client:
                    self.assertEqual(client.get("/health").status_code, 200)

        self.assertEqual(
            {str(error) for error in raised.exception.exceptions},
            {
                "logs failed",
                "sync Todoist failed",
                "async Todoist failed",
                "database failed",
                "async database failed",
            },
        )
        drain_workers.assert_awaited_once_with(timeout=5.0)
        shutdown.assert_called_once_with(timeout=5.0)
        close_todoist.assert_called_once_with()
        close_todoist_async.assert_awaited_once_with()
        close_pool.assert_called_once_with()
        close_async_pool.assert_awaited_once_with()

    def test_fastapi_lifespan_aggregates_llm_cleanup_failures(self) -> None:
        with patch("agents.agent_api.app.db.verify_database_runtime"), patch(
            "agents.agent_api.app.graph.nodes.orchestrator.close_shared_agent_client",
            side_effect=RuntimeError("sync agent failed"),
        ) as close_agent, patch(
            "agents.agent_api.app.router.client.close_shared_router_client"
        ) as close_router, patch(
            "agents.agent_api.app.graph.nodes.summarize.close_shared_summarizer_client"
        ) as close_summarizer, patch(
            "agents.agent_api.app.graph.nodes.orchestrator.close_shared_async_agent_client",
            new_callable=AsyncMock,
        ) as close_agent_async, patch(
            "agents.agent_api.app.router.client.close_shared_async_router_openai_client",
            new_callable=AsyncMock,
            side_effect=RuntimeError("async router failed"),
        ) as close_router_async, patch(
            "agents.agent_api.app.graph.nodes.summarize.close_shared_async_summarizer_client",
            new_callable=AsyncMock,
        ) as close_summarizer_async, patch(
            "agents.agent_api.app.run_logging.shutdown_run_logs"
        ), patch(
            "agents.agent_api.app.tools.todoist.client.close_todoist_http_client"
        ), patch(
            "agents.agent_api.app.tools.todoist.client.close_todoist_async_http_client",
            new_callable=AsyncMock,
        ), patch("agents.agent_api.app.db.close_pool"):
            with self.assertRaises(ExceptionGroup) as raised:
                with TestClient(create_app()) as client:
                    self.assertEqual(client.get("/health").status_code, 200)

        self.assertEqual(
            {str(error) for error in raised.exception.exceptions},
            {"sync agent failed", "async router failed"},
        )
        close_agent.assert_called_once_with()
        close_router.assert_called_once_with()
        close_summarizer.assert_called_once_with()
        close_agent_async.assert_awaited_once_with()
        close_router_async.assert_awaited_once_with()
        close_summarizer_async.assert_awaited_once_with()

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
        self.assertEqual(
            body["limits"],
            {
                "run_deadline_seconds": 150.0,
                "max_agent_turns": 20,
                "deepseek_request_timeout_seconds": 30.0,
                "model_router_complex_timeout_seconds": 90.0,
            },
        )
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
                "error_details": None,
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

    def test_invoke_awaits_async_runner(self) -> None:
        run = AsyncMock(
            return_value={
                "thread_id": "thread-async",
                "interrupted": False,
                "final_response": "Awaited.",
                "tool_results": [],
                "error": "",
            }
        )

        with patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            run,
        ):
            response = self.client.post(
                "/invoke",
                json={"message": "async please", "user_id": "jerry"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["response"], "Awaited.")
        run.assert_awaited_once()
        self.assertIn("checkpointer", run.await_args.kwargs)

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
            tracer.progress({"phase": "request", "action": "started"})
            tracer.progress({"phase": "lookup", "action": "started", "domains": ["todoist"], "intent": "read"})
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
        progress_facts = [event.get("fact") for event in events if event["type"] == "progress"]
        self.assertIn({"phase": "request", "action": "started"}, progress_facts)
        self.assertIn({"phase": "lookup", "action": "started", "domains": ["todoist"], "intent": "read"}, progress_facts)

    def test_resume_stream_emits_resume_progress(self) -> None:
        def fake_run(**kwargs):
            tracer = kwargs["tracer"]
            tracer.progress({"phase": "request", "action": "started"})
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
        self.assertEqual(events[0]["fact"], {"phase": "request", "action": "started"})
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

    def test_todoist_pool_keepalive_cannot_exceed_total_connections(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "TODOIST_HTTP_MAX_KEEPALIVE_CONNECTIONS": "21",
                "TODOIST_HTTP_MAX_CONNECTIONS": "20",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "must not exceed"):
                load_settings()

    def test_positive_float_settings_reject_non_finite_values(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), patch.dict(
                "os.environ",
                {"TODOIST_RETRY_TOTAL_TIMEOUT_SECONDS": value},
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "finite and greater than zero"):
                    load_settings()

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
            "agents.agent_api.app.middleware.thread_ownership.validate_thread_ownership",
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

    def test_resume_does_not_consume_new_thread_quota(self) -> None:
        with patch(
            "agents.agent_api.app.middleware.thread_ownership.validate_thread_ownership",
        ), patch(
            "agents.agent_api.app.middleware.rate_limit.consume_new_thread_quota",
            side_effect=AssertionError("resume must not consume quota"),
        ), patch(
            "agents.agent_api.app.api.routes.resume.run_jarvis",
            return_value={
                "thread_id": "t-1",
                "interrupted": False,
                "final_response": "Resumed.",
                "tool_results": [],
                "error": "",
            },
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
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["response"], "Resumed.")

    def test_invoke_returns_429_on_rate_limit(self) -> None:
        from fastapi import HTTPException

        with patch(
            "agents.agent_api.app.middleware.rate_limit.consume_new_thread_quota",
            side_effect=HTTPException(
                status_code=429,
                detail="Daily thread limit reached.",
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

    def test_invoke_with_thread_id_does_not_consume_new_thread_quota(self) -> None:
        with patch(
            "agents.agent_api.app.middleware.thread_ownership.validate_thread_ownership",
        ), patch(
            "agents.agent_api.app.middleware.rate_limit.consume_new_thread_quota",
            side_effect=AssertionError("continuations must not consume quota"),
        ), patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value={
                "thread_id": "existing-thread",
                "interrupted": False,
                "final_response": "Continued.",
                "tool_results": [],
                "error": "",
            },
        ):
            response = self.client.post(
                "/invoke",
                json={
                    "message": "continue",
                    "user_id": "jerry",
                    "thread_id": "existing-thread",
                    "telegram_user_id": 42,
                    "request_id": "req-continuation",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["response"], "Continued.")

    def test_invoke_returns_403_on_ownership_violation_with_thread_id(self) -> None:
        from fastapi import HTTPException

        with patch(
            "agents.agent_api.app.middleware.thread_ownership.validate_thread_ownership",
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

    def test_invoke_bulk_charges_per_non_empty_message_and_stops_on_429(self) -> None:
        from fastapi import HTTPException

        with patch(
            "agents.agent_api.app.middleware.rate_limit.consume_new_thread_quota",
            side_effect=[
                None,
                HTTPException(
                    status_code=429,
                    detail="Daily thread limit reached.",
                    headers={"Retry-After": "3600"},
                ),
            ],
        ) as quota, patch(
            "agents.agent_api.app.api.routes.invoke.run_jarvis",
            return_value={
                "thread_id": "thread-1",
                "interrupted": False,
                "final_response": "First done.",
                "tool_results": [],
                "error": "",
            },
        ) as run:
            response = self.client.post(
                "/invoke-bulk",
                json={
                    "messages": ["first prompt", "second prompt", "third prompt"],
                    "user_id": "jerry",
                    "telegram_user_id": 42,
                },
            )

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual([item["status"] for item in results], ["completed", "failed", "failed"])
        self.assertEqual(results[1]["error"], "HTTP 429: Daily thread limit reached.")
        self.assertEqual(results[2]["error"], "HTTP 429: Daily thread limit reached.")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(quota.call_count, 2)


if __name__ == "__main__":
    unittest.main()
