"""Tests for per-run readable file logging of Jarvis agent invocations."""

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest import TestCase, mock

from agents.agent_api.app import run_logging
from agents.agent_api.app.graph import builder
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


class BuildRunLogPathTests(TestCase):
    def test_telegram_username_and_thread_suffix_build_filename(self) -> None:
        path = run_logging.build_run_log_path(
            "abcdef12-3456",
            now=datetime(2026, 6, 21, 1, 2, 3, 4, tzinfo=timezone.utc),
            identity=run_logging.RunLogIdentity(
                request_source="telegram",
                telegram_user_id=701122767,
                telegram_username="Jerry",
            ),
        )

        self.assertEqual(path.parent.name, "jerry-701122767")
        self.assertEqual(path.name, "jerry_23456.log")

    def test_filename_is_stable_for_same_user_and_thread_suffix(self) -> None:
        identity = run_logging.RunLogIdentity(
            request_source="telegram",
            telegram_user_id=701122767,
            telegram_username="Jerry",
        )
        earlier = run_logging.build_run_log_path(
            "abcdef12-3456",
            now=datetime(2026, 6, 21, 9, 0, 0, 1),
            identity=identity,
        )
        later = run_logging.build_run_log_path(
            "abcdef12-3456",
            now=datetime(2026, 6, 21, 9, 0, 0, 2),
            identity=identity,
        )

        self.assertEqual(earlier.name, "jerry_23456.log")
        self.assertEqual(later.name, "jerry_23456.log")

    def test_missing_thread_id_falls_back_to_placeholder(self) -> None:
        path = run_logging.build_run_log_path("", now=datetime(2026, 6, 21, 9, 0, 0))
        self.assertEqual(path.parent.name, "jer_jerryyy-701122767")
        self.assertEqual(path.name, "jer_jerryyy_norun.log")

    def test_missing_username_falls_back_to_first_name(self) -> None:
        path = run_logging.build_run_log_path(
            "tg_fd1ed82cbdaeabbc92afb8b0c57dd28c_385",
            now=datetime(2026, 6, 21, 9, 0, 0),
            identity=run_logging.RunLogIdentity(
                request_source="telegram",
                telegram_user_id=222,
                telegram_first_name="Alex Friend",
            ),
        )

        self.assertEqual(path.parent.name, "alex_friend-222")
        self.assertEqual(path.name, "alex_friend_c_385.log")

    def test_sanitizes_username_for_folder_and_filename(self) -> None:
        path = run_logging.build_run_log_path(
            "thread-12345",
            now=datetime(2026, 6, 21, 9, 0, 0),
            identity=run_logging.RunLogIdentity(
                request_source="telegram",
                telegram_user_id=333,
                telegram_username="  @Bad Name!!  ",
            ),
        )

        self.assertEqual(path.parent.name, "bad_name-333")
        self.assertEqual(path.name, "bad_name_12345.log")

    def test_non_telegram_sources_use_cli_dev_folder(self) -> None:
        path = run_logging.build_run_log_path(
            "local-thread",
            now=datetime(2026, 6, 21, 9, 0, 0),
            identity=run_logging.RunLogIdentity(request_source="cli"),
        )

        self.assertEqual(path.parent.name, "jer_jerryyy-701122767")
        self.assertEqual(path.name, "jer_jerryyy_hread.log")


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
    def test_singapore_timestamp_formatter_converts_aware_datetime(self) -> None:
        timestamp = run_logging.format_singapore_log_timestamp(
            datetime(2026, 6, 21, 1, 2, 3, 456000, tzinfo=timezone.utc)
        )
        iso_timestamp = run_logging.format_singapore_log_iso(
            datetime(2026, 6, 21, 1, 2, 3, 456000, tzinfo=timezone.utc)
        )

        self.assertEqual(timestamp, "2026-06-21 09:02:03.456")
        self.assertEqual(iso_timestamp, "2026-06-21T09:02:03+08:00")

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

    def test_write_line_uses_singapore_log_timestamp_formatter(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            run_logging, "format_singapore_log_timestamp", return_value="2026-06-21 09:02:03.456"
        ):
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path)
            log.write_line("agent.request", "Calling DeepSeek.")
            content = path.read_text(encoding="utf-8")

        self.assertIn("2026-06-21 09:02:03.456 | agent.request", content)


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
    "get_todoist_task",
    "get_tasks",
    "get_tasks_by_filter",
    "update_todoist_task",
    "complete_task",
    "uncomplete_task",
    "delete_todoist_task",
    "get_completed_todoist_tasks_by_completion_date",
    "get_comments",
    "add_comment",
    "get_labels",
    "get_projects",
    "create_project",
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
            result = builder.run_jarvis(
                user_prompt="hello",
                agent_client=agent,
                todoist_client=_FakeTodoistClient(),
                tracer=NULL_TRACE,
                thread_id="feedface-0000",
                request_id="tg_log",
            )
        files = sorted(run_logging.Path(tmp).glob("*/*.log"))
        return files, result

    def test_run_writes_one_readable_file_capturing_client_events(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            files, result = self._run(tmp, enabled=True)
            self.assertEqual(len(files), 1)
            self.assertEqual(result["run_log_path"], str(files[0].resolve()))
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
            files, result = self._run(tmp, enabled=False)
            self.assertEqual(files, [])
            self.assertNotIn("run_log_path", result)
