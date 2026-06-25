"""Tests for per-run readable file logging of Jarvis agent invocations."""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest import TestCase, mock

from agents.agent_api.app import run_logging
from agents.agent_api.app.graph import builder
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


class BuildRunLogPathTests(TestCase):
    def test_filename_sorts_chronologically_and_tags_thread(self) -> None:
        earlier = run_logging.build_run_log_path(
            "abcdef12-3456", now=datetime(2026, 6, 21, 9, 0, 0, 1)
        )
        later = run_logging.build_run_log_path(
            "abcdef12-3456", now=datetime(2026, 6, 21, 9, 0, 0, 2)
        )

        self.assertTrue(earlier.name.startswith("jarvis_run_2026"))
        self.assertTrue(earlier.name.endswith("_abcdef12.log"))
        # Lexical filename order matches chronological run order.
        self.assertLess(earlier.name, later.name)

    def test_missing_thread_id_falls_back_to_placeholder(self) -> None:
        path = run_logging.build_run_log_path("", now=datetime(2026, 6, 21, 9, 0, 0))
        self.assertTrue(path.name.endswith("_norun.log"))


class RunFileLogEnabledTests(TestCase):
    def test_env_override_forces_on_even_under_pytest(self) -> None:
        with mock.patch.dict(os.environ, {"JARVIS_RUN_FILE_LOG": "1"}, clear=False):
            self.assertTrue(run_logging.run_file_log_enabled())

    def test_env_override_forces_off(self) -> None:
        with mock.patch.dict(os.environ, {"JARVIS_RUN_FILE_LOG": "false"}, clear=False):
            self.assertFalse(run_logging.run_file_log_enabled())

    def test_disabled_under_pytest_by_default(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "JARVIS_RUN_FILE_LOG"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["PYTEST_CURRENT_TEST"] = "x"
            self.assertFalse(run_logging.run_file_log_enabled())

    def test_open_run_log_returns_none_when_disabled(self) -> None:
        with mock.patch.object(run_logging, "run_file_log_enabled", return_value=False):
            self.assertIsNone(run_logging.open_run_log("thread"))


class RunFileLogWritingTests(TestCase):
    def test_header_lines_and_footer_are_human_readable(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path)
            log.write_header(thread_id="t1", request_source="cli")
            log.write_line("agent.request", "Calling DeepSeek.")
            log.write_footer(turns=2)

            content = path.read_text(encoding="utf-8")

        self.assertIn("Jarvis run", content)
        self.assertIn("thread_id: t1", content)
        self.assertIn("agent.request", content)
        self.assertIn("Calling DeepSeek.", content)
        self.assertIn("Run finished", content)
        self.assertIn("turns: 2", content)


class FileLoggingTracerTests(TestCase):
    def _tracer(self, tmp: str):
        path = run_logging.Path(tmp) / "run.log"
        run_log = run_logging.RunFileLog(path)
        wrapped = run_logging.FileLoggingTracer(TracePrinter(enabled=False), run_log)
        return wrapped, path

    def test_event_and_payload_are_mirrored_to_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tracer, path = self._tracer(tmp)
            tracer.section("Run")
            tracer.event("tool.done", "Tool call completed.", name="get_tasks")
            tracer.payload("tool.result", "get_tasks", {"results": []})
            content = path.read_text(encoding="utf-8")

        self.assertIn("=== Run ===", content)
        self.assertIn("tool.done", content)
        self.assertIn("name: get_tasks", content)
        self.assertIn("[payload] get_tasks", content)

    def test_unknown_attribute_delegates_to_wrapped_tracer(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tracer, _ = self._tracer(tmp)
            # `enabled` is only defined on the wrapped TracePrinter.
            self.assertFalse(tracer.enabled)


class _FakeAgentClientWithTracer:
    """Fake LLM that emits a trace event through its own tracer attribute."""

    def __init__(self, response: Dict[str, Any]):
        self.response = response
        self.tracer = NULL_TRACE

    def create_message(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        self.tracer.event("agent.request", "Calling DeepSeek chat completions.")
        return dict(self.response)


_TODOIST_METHODS = (
    "add_todoist_task",
    "bulk_add_todoist_tasks",
    "get_todoist_task",
    "get_tasks",
    "get_tasks_by_filter",
    "update_todoist_task",
    "complete_task",
    "delete_todoist_task",
    "get_completed_todoist_tasks_by_completion_date",
)


class _FakeTodoistClient:
    """Minimal Todoist client; never exercised on the direct-answer path."""

    def __getattr__(self, name: str):
        if name in _TODOIST_METHODS:
            return lambda arguments: {}
        raise AttributeError(name)


class RunJarvisFileLoggingTests(TestCase):
    def _run(self, tmp: str, *, enabled: bool) -> Optional[Any]:
        agent = _FakeAgentClientWithTracer({"role": "assistant", "content": "Done."})
        with mock.patch.object(run_logging, "run_file_log_enabled", return_value=enabled), \
            mock.patch.object(run_logging, "LOG_DIR", run_logging.Path(tmp)):
            builder.run_jarvis(
                user_prompt="hello",
                agent_client=agent,
                todoist_client=_FakeTodoistClient(),
                tracer=NULL_TRACE,
                thread_id="feedface-0000",
                request_id="tg_log",
            )
        files = sorted(run_logging.Path(tmp).glob("jarvis_run_*.log"))
        return files

    def test_run_writes_one_readable_file_capturing_client_events(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            files = self._run(tmp, enabled=True)
            self.assertEqual(len(files), 1)
            content = files[0].read_text(encoding="utf-8")

        # Run boundaries plus node-level and redirected client-level events.
        self.assertIn("Jarvis run", content)
        self.assertIn("runtime.start", content)
        self.assertIn("agent.request", content)  # emitted via the redirected client tracer
        self.assertIn("Run finished", content)
        # Correlation id in the header and token/duration totals in the footer.
        self.assertIn("request_id: tg_log", content)
        self.assertIn("duration_seconds:", content)
        self.assertIn("total_tokens:", content)

    def test_no_file_written_when_disabled(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            files = self._run(tmp, enabled=False)
            self.assertEqual(files, [])
