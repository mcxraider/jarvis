"""Tests for per-run readable file logging of Jarvis agent invocations."""

import concurrent.futures
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
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
            log = run_logging.RunFileLog(path, background=False)
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
            log = run_logging.RunFileLog(path, background=False)
            log.write_line("agent.request", "Calling DeepSeek.")
            log.write_footer()
            content = path.read_text(encoding="utf-8")

        self.assertIn("2026-06-21 09:02:03.456 | agent.request", content)

    def test_buffer_no_file_until_footer(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            log.write_header(thread_id="t1")
            log.write_line("agent.request", "Calling DeepSeek.")

            self.assertFalse(path.exists())

            log.write_footer(turns=1)
            self.assertTrue(path.exists())

    def test_no_open_or_write_during_events(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            with mock.patch("builtins.open", mock.mock_open()) as opened:
                log.write_header(thread_id="t1")
                log.write_line("agent.request", "Calling DeepSeek.")
                self.assertEqual(opened.call_count, 0)
                log.write_footer()

        self.assertEqual(opened.call_count, 1)
        opened.assert_called_once_with(path, "a", encoding="utf-8")

    def test_append_mode_preserves_prior(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            path.write_text("sentinel\n", encoding="utf-8")
            log = run_logging.RunFileLog(path, background=False)
            log.write_line("agent.request", "Calling DeepSeek.")
            log.write_footer()
            content = path.read_text(encoding="utf-8")

        self.assertTrue(content.startswith("sentinel\n"))
        self.assertIn("Calling DeepSeek.", content)

    def test_messages_dump_format(self) -> None:
        import tempfile

        messages = [{"role": "user", "content": "hello"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            log.write_messages_dump("final", messages)
            log._flush_to_disk()
            content = path.read_text(encoding="utf-8")

        self.assertIn("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~", content)
        self.assertIn("MESSAGES DUMP: final", content)
        self.assertIn("message_count: 1", content)
        self.assertIn('"role": "user"', content)

    def test_messages_dump_snapshot_safety(self) -> None:
        import tempfile

        messages = [{"role": "user", "content": {"text": "before"}}]
        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            log.write_messages_dump("snapshot", messages)
            messages.append({"role": "assistant", "content": "after"})
            messages[0]["content"]["text"] = "mutated"
            log._flush_to_disk()
            content = path.read_text(encoding="utf-8")

        self.assertIn('"text": "before"', content)
        self.assertNotIn("mutated", content)
        self.assertNotIn("after", content)

    def test_buffer_byte_cap(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            payload = "x" * (run_logging.MAX_BUFFER_BYTES // 2)
            entry_count = (
                run_logging.MAX_BUFFER_BYTES // run_logging.MAX_PAYLOAD_BYTES
            ) + 2
            for index in range(entry_count):
                log.write_line("payload", f"payload-{index} {payload}")

            self.assertLessEqual(log._buffer_bytes, run_logging.MAX_BUFFER_BYTES)
            self.assertGreater(log._events_dropped, 0)
            log.write_footer()
            content = path.read_text(encoding="utf-8")

        self.assertIn("events_dropped:", content)
        self.assertNotIn(f"payload-{entry_count - 1}", content)

    def test_buffer_event_cap(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            for index in range(run_logging.MAX_BUFFER_EVENTS + 10):
                log.write_line("runtime.event", f"event-{index}")
            log.write_footer()
            content = path.read_text(encoding="utf-8")

        self.assertIn("event-0", content)
        self.assertNotIn(f"event-{run_logging.MAX_BUFFER_EVENTS + 9}", content)
        self.assertIn("events_dropped: 10", content)

    def test_error_events_survive_cap(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            for index in range(run_logging.MAX_BUFFER_EVENTS):
                log.write_line("runtime.event", f"event-{index}")
            log.write_line("runtime.error", "important failure")
            log.write_footer()
            content = path.read_text(encoding="utf-8")

        self.assertIn("important failure", content)

    def test_header_footer_always_present(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            log.write_header(thread_id="t1")
            for index in range(run_logging.MAX_BUFFER_EVENTS + 5):
                log.write_line("runtime.event", f"event-{index}")
            log.write_footer(turns=1)
            content = path.read_text(encoding="utf-8")

        self.assertIn("Jarvis run", content)
        self.assertIn("thread_id: t1", content)
        self.assertIn("Run finished", content)
        self.assertIn("turns: 1", content)

    def test_crash_produces_partial_log(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=True)
            log.write_header(thread_id="t1")
            log.write_line("runtime.start", "Starting.")
            try:
                raise ValueError("boom")
            except ValueError as error:
                log.write_crash(error)
            content = path.read_text(encoding="utf-8")

        self.assertIn("Jarvis run", content)
        self.assertIn("RUN CRASH", content)
        self.assertIn("ValueError: boom", content)
        self.assertIn("Traceback", content)

    def test_crash_after_full_buffer(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=True)
            log.write_header(thread_id="t1")
            for index in range(run_logging.MAX_BUFFER_EVENTS + 5):
                log.write_line("runtime.event", f"event-{index}")
            log.write_crash(RuntimeError("crashed"))
            run_logging.flush_run_logs()
            content = path.read_text(encoding="utf-8")

        self.assertIn("Jarvis run", content)
        self.assertIn("RUN CRASH", content)
        self.assertIn("RuntimeError: crashed", content)

    def test_crash_does_not_corrupt_append(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            path.write_text("sentinel\n", encoding="utf-8")
            log = run_logging.RunFileLog(path, background=True)
            log.write_crash(ValueError("boom"))
            content = path.read_text(encoding="utf-8")

        self.assertTrue(content.startswith("sentinel\n"))
        self.assertIn("RUN CRASH", content)


class RunFileLogBackgroundWritingTests(TestCase):
    def setUp(self) -> None:
        run_logging.reset_log_writer()

    def tearDown(self) -> None:
        run_logging.reset_log_writer()

    def test_background_write_nonblocking(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=True)
            original_flush = log._flush_to_disk

            def slow_flush(content: Optional[str] = None) -> None:
                time.sleep(0.2)
                original_flush(content)

            with mock.patch.object(log, "_flush_to_disk", side_effect=slow_flush):
                log.write_line("runtime.event", "queued")
                started = time.perf_counter()
                log.write_footer()
                elapsed = time.perf_counter() - started

            self.assertLess(elapsed, 0.05)
            self.assertFalse(path.exists())
            run_logging.flush_run_logs()
            self.assertTrue(path.exists())

    def test_cli_mode_writes_synchronously(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            log.write_line("runtime.event", "sync")
            log.write_footer()
            self.assertTrue(path.exists())

    def test_flush_run_logs_blocks(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=True)
            log.write_line("runtime.event", "async")
            log.write_footer()
            run_logging.flush_run_logs()
            self.assertTrue(path.exists())

    def test_shutdown_drains_pending(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for index in range(5):
                path = run_logging.Path(tmp) / f"run-{index}.log"
                paths.append(path)
                log = run_logging.RunFileLog(path, background=True)
                log.write_line("runtime.event", f"run-{index}")
                log.write_footer()

            run_logging.shutdown_run_logs(timeout=2.0)
            self.assertTrue(all(path.exists() for path in paths))

    def test_reset_log_writer_isolation(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            first = run_logging.Path(tmp) / "first.log"
            log = run_logging.RunFileLog(first, background=True)
            log.write_line("runtime.event", "first")
            log.write_footer()
            run_logging.flush_run_logs()
            self.assertTrue(first.exists())

            run_logging.reset_log_writer()
            second = run_logging.Path(tmp) / "second.log"
            log = run_logging.RunFileLog(second, background=True)
            log.write_line("runtime.event", "second")
            log.write_footer()
            run_logging.flush_run_logs()
            self.assertTrue(second.exists())

    def test_executor_exception_does_not_propagate(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=True)
            with mock.patch.object(log, "_flush_to_disk", side_effect=IOError("disk")), \
                self.assertLogs(run_logging.__name__, level="ERROR") as captured:
                log.write_footer()
                run_logging.flush_run_logs()

        self.assertIn("Run-log background write failed.", "\n".join(captured.output))

    def test_ordering_preserved(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = run_logging.Path(tmp) / "run.log"
            for index in range(10):
                log = run_logging.RunFileLog(path, background=True)
                log.write_line("runtime.event", f"ordered-{index}")
                log.write_footer()
            run_logging.flush_run_logs()
            content = path.read_text(encoding="utf-8")

        positions = [content.index(f"ordered-{index}") for index in range(10)]
        self.assertEqual(positions, sorted(positions))


class FileLoggingTracerTests(TestCase):
    def _tracer(self, tmp: str):
        path = run_logging.Path(tmp) / "run.log"
        run_log = run_logging.RunFileLog(path, background=False)
        wrapped = run_logging.FileLoggingTracer(TracePrinter(enabled=False), run_log)
        return wrapped, path

    def test_event_and_payload_are_mirrored_to_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tracer, path = self._tracer(tmp)
            tracer.section("Run")
            tracer.event("tool.done", "Tool call completed.", name="get_tasks")
            tracer.payload("tool.result", "get_tasks", {"results": []})
            tracer.run_log.write_footer()
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
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], **kwargs
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

    def test_builder_crash_integration(self) -> None:
        import tempfile

        class _CrashingGraph:
            def invoke(self, *_args: Any, **_kwargs: Any) -> Dict[str, Any]:
                raise ValueError("graph boom")

        agent = _FakeAgentClientWithTracer({"role": "assistant", "content": "Done."})
        with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(run_logging, "run_file_log_enabled", return_value=True), \
            mock.patch.object(run_logging, "LOG_DIR", run_logging.Path(tmp)), \
            mock.patch.object(builder, "create_jarvis_graph", return_value=_CrashingGraph()):
            with self.assertRaisesRegex(ValueError, "graph boom"):
                builder.run_jarvis(
                    user_prompt="hello",
                    agent_client=agent,
                    todoist_client=_FakeTodoistClient(),
                    tracer=NULL_TRACE,
                    thread_id="feedface-0000",
                    request_id="tg_crash",
                )
            files = sorted(run_logging.Path(tmp).glob("*/*.log"))
            self.assertEqual(len(files), 1)
            content = files[0].read_text(encoding="utf-8")

        self.assertIn("Jarvis run", content)
        self.assertIn("RUN CRASH", content)
        self.assertIn("ValueError: graph boom", content)


class LogCleanupTests(TestCase):
    """Stage 1: Log rotation and retention tests."""

    def _create_log_file(self, directory: Path, name: str, size: int, age_seconds: float) -> Path:
        """Helper: create a log file with specific size and mtime."""
        file = directory / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"x" * size)
        mtime = time.time() - age_seconds
        os.utime(file, (mtime, mtime))
        return file

    def test_cleanup_deletes_old_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old_file = self._create_log_file(
                base, "user/old.log", 1024, age_seconds=15 * 86400
            )
            self.assertTrue(old_file.exists())

            deleted = run_logging.cleanup_old_logs(base, max_age_days=14, max_total_mb=100)

            self.assertEqual(deleted, 1)
            self.assertFalse(old_file.exists())

    def test_cleanup_preserves_recent_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            recent_file = self._create_log_file(
                base, "user/recent.log", 1024, age_seconds=2 * 86400
            )

            deleted = run_logging.cleanup_old_logs(base, max_age_days=14, max_total_mb=100)

            self.assertEqual(deleted, 0)
            self.assertTrue(recent_file.exists())

    def test_cleanup_respects_size_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = []
            for i in range(15):
                f = self._create_log_file(
                    base, f"user/run_{i:02d}.log",
                    size=10 * 1024 * 1024,
                    age_seconds=(7200 + i * 100),
                )
                files.append(f)

            deleted = run_logging.cleanup_old_logs(base, max_age_days=14, max_total_mb=100)

            self.assertGreater(deleted, 0)
            total_remaining = sum(
                f.stat().st_size for f in base.rglob("*.log") if f.is_file()
            )
            self.assertLessEqual(total_remaining, 100 * 1024 * 1024)

    def test_cleanup_skips_active_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            active_file = self._create_log_file(
                base, "user/active.log",
                size=60 * 1024 * 1024,
                age_seconds=300,
            )
            old_non_active = self._create_log_file(
                base, "user/old_big.log",
                size=60 * 1024 * 1024,
                age_seconds=7200,
            )

            deleted = run_logging.cleanup_old_logs(base, max_age_days=14, max_total_mb=50)

            self.assertTrue(active_file.exists())
            self.assertFalse(old_non_active.exists())
            self.assertEqual(deleted, 1)

    def test_cleanup_debounce(self) -> None:
        run_logging.reset_log_writer()
        with mock.patch.object(run_logging, "cleanup_old_logs", return_value=0) as mock_cleanup:
            run_logging._schedule_log_cleanup()
            run_logging._schedule_log_cleanup()
            run_logging.flush_run_logs()

        self.assertEqual(mock_cleanup.call_count, 1)
        run_logging.reset_log_writer()

    def test_cleanup_runs_in_background(self) -> None:
        run_logging.reset_log_writer()
        barrier = threading.Event()

        def slow_cleanup(*_args, **_kwargs) -> int:
            barrier.wait(timeout=2.0)
            return 0

        with mock.patch.object(run_logging, "cleanup_old_logs", side_effect=slow_cleanup):
            start = time.perf_counter()
            run_logging._schedule_log_cleanup()
            elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.05)
        barrier.set()
        run_logging.flush_run_logs()
        run_logging.reset_log_writer()


class PeriodicFlushTests(TestCase):
    """Stage 2: Periodic flush for long runs."""

    def setUp(self) -> None:
        run_logging.reset_log_writer()

    def tearDown(self) -> None:
        run_logging.reset_log_writer()

    def test_periodic_flush_writes_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=True)
            for i in range(60):
                log.write_line("runtime.event", f"event-{i}")
            run_logging.flush_run_logs()
            content = path.read_text(encoding="utf-8")

        self.assertIn("event-0", content)
        self.assertIn("event-49", content)

    def test_final_flush_appends_remainder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=True)
            for i in range(60):
                log.write_line("runtime.event", f"event-{i}")
            log.write_footer(turns=1)
            run_logging.flush_run_logs()
            content = path.read_text(encoding="utf-8")

        for i in range(60):
            self.assertIn(f"event-{i}", content)
        self.assertIn("Run finished", content)
        self.assertIn("turns: 1", content)

    def test_no_periodic_flush_in_cli_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            for i in range(100):
                log.write_line("runtime.event", f"event-{i}")

            self.assertFalse(path.exists())

            log.write_footer()
            self.assertTrue(path.exists())

    def test_crash_after_partial_flush(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=True)
            for i in range(50):
                log.write_line("runtime.event", f"event-{i}")
            run_logging.flush_run_logs()
            for i in range(50, 60):
                log.write_line("runtime.event", f"event-{i}")
            log.write_crash(RuntimeError("boom"))
            content = path.read_text(encoding="utf-8")

        self.assertIn("event-0", content)
        self.assertIn("event-49", content)
        self.assertIn("event-59", content)
        self.assertIn("RUN CRASH", content)
        self.assertIn("RuntimeError: boom", content)

    def test_periodic_flush_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=True)
            original_partial = log._flush_partial

            def slow_partial(lines: list[str]) -> None:
                time.sleep(0.3)
                original_partial(lines)

            with mock.patch.object(log, "_flush_partial", side_effect=slow_partial):
                start = time.perf_counter()
                for i in range(60):
                    log.write_line("runtime.event", f"event-{i}")
                elapsed = time.perf_counter() - start

            self.assertLess(elapsed, 0.05)
            run_logging.flush_run_logs()

    def test_buffer_cap_resets_after_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=True)
            for i in range(50):
                log.write_line("runtime.event", f"event-{i}")
            run_logging.flush_run_logs()

            self.assertEqual(log._buffer_events, 0)
            self.assertEqual(log._buffer_bytes, 0)
            self.assertEqual(len(log._buffer), 0)

            for i in range(50, 100):
                log.write_line("runtime.event", f"event-{i}")
            run_logging.flush_run_logs()
            content = path.read_text(encoding="utf-8")

        self.assertIn("event-99", content)


class LogWriterStatsTests(TestCase):
    """Stage 3: Logger self-metrics and health."""

    def setUp(self) -> None:
        run_logging.reset_log_writer()

    def tearDown(self) -> None:
        run_logging.reset_log_writer()

    def test_stats_track_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            for i in range(10):
                log.write_line("runtime.event", f"event-{i}")

        stats = run_logging.get_log_writer_stats()
        self.assertEqual(stats.events_accepted, 10)

    def test_stats_track_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            for i in range(run_logging.MAX_BUFFER_EVENTS + 5):
                log.write_line("runtime.event", f"event-{i}")

        stats = run_logging.get_log_writer_stats()
        self.assertEqual(stats.events_dropped, 5)

    def test_stats_track_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            log.write_line("runtime.event", "hello")
            log.write_footer()

        stats = run_logging.get_log_writer_stats()
        self.assertGreaterEqual(stats.writes_completed, 1)

    def test_stats_track_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            log.write_line("runtime.event", "hello")
            with mock.patch("builtins.open", side_effect=IOError("disk full")):
                with self.assertRaises(IOError):
                    log._flush_to_disk()

        stats = run_logging.get_log_writer_stats()
        self.assertGreaterEqual(stats.writes_failed, 1)

    def test_stats_last_write_time(self) -> None:
        before = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            log.write_line("runtime.event", "hello")
            log.write_footer()
        after = time.monotonic()

        stats = run_logging.get_log_writer_stats()
        self.assertIsNotNone(stats.last_write_time)
        self.assertGreaterEqual(stats.last_write_time, before)
        self.assertLessEqual(stats.last_write_time, after)

    def test_healthy_returns_true_normally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            log.write_line("runtime.event", "hello")
            log.write_footer()

        self.assertTrue(run_logging.log_writer_healthy())

    def test_healthy_returns_false_when_stale(self) -> None:
        with run_logging._writer_stats_lock:
            run_logging._writer_stats.last_write_time = time.monotonic() - 60.0
        fake_future = concurrent.futures.Future()
        with run_logging._pending_futures_lock:
            run_logging._pending_futures.add(fake_future)
        try:
            self.assertFalse(run_logging.log_writer_healthy())
        finally:
            with run_logging._pending_futures_lock:
                run_logging._pending_futures.discard(fake_future)

    def test_write_failure_logs_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            log.write_line("runtime.event", "hello")
            with mock.patch("builtins.open", side_effect=IOError("disk full")), \
                self.assertLogs(run_logging.__name__, level="WARNING") as captured:
                with self.assertRaises(IOError):
                    log._flush_to_disk()

        self.assertTrue(any("flush failed" in msg for msg in captured.output))


class DropVisibilityAndBackpressureTests(TestCase):
    """Stage 4: Drop warnings and backpressure signals."""

    def setUp(self) -> None:
        run_logging.reset_log_writer()

    def tearDown(self) -> None:
        run_logging.reset_log_writer()

    def test_first_drop_emits_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            for i in range(run_logging.MAX_BUFFER_EVENTS):
                log.write_line("runtime.event", f"event-{i}")
            with self.assertLogs(run_logging.__name__, level="WARNING") as captured:
                log.write_line("runtime.event", "dropped")

        self.assertTrue(any("events_dropped" in msg for msg in captured.output))

    def test_subsequent_drops_no_extra_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            for i in range(run_logging.MAX_BUFFER_EVENTS):
                log.write_line("runtime.event", f"event-{i}")
            with self.assertLogs(run_logging.__name__, level="WARNING") as captured:
                for i in range(10):
                    log.write_line("runtime.event", f"dropped-{i}")

        drop_warnings = [msg for msg in captured.output if "events_dropped" in msg]
        self.assertEqual(len(drop_warnings), 1)

    def test_backpressure_warning_on_queue_depth(self) -> None:
        fakes = [concurrent.futures.Future() for _ in range(6)]
        with run_logging._pending_futures_lock:
            for f in fakes:
                run_logging._pending_futures.add(f)
        try:
            with self.assertLogs(run_logging.__name__, level="WARNING") as captured:
                run_logging._check_writer_backpressure()
            self.assertTrue(any("backpressure" in msg for msg in captured.output))
        finally:
            with run_logging._pending_futures_lock:
                for f in fakes:
                    run_logging._pending_futures.discard(f)

    def test_backpressure_rate_limited(self) -> None:
        fakes = [concurrent.futures.Future() for _ in range(6)]
        with run_logging._pending_futures_lock:
            for f in fakes:
                run_logging._pending_futures.add(f)
        try:
            with self.assertLogs(run_logging.__name__, level="WARNING") as captured:
                for _ in range(5):
                    run_logging._check_writer_backpressure()
            bp_warnings = [msg for msg in captured.output if "backpressure" in msg]
            self.assertEqual(len(bp_warnings), 1)
        finally:
            with run_logging._pending_futures_lock:
                for f in fakes:
                    run_logging._pending_futures.discard(f)

    def test_footer_includes_drop_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            for i in range(run_logging.MAX_BUFFER_EVENTS + 3):
                log.write_line("runtime.event", f"event-{i}")
            log.write_footer()
            content = path.read_text(encoding="utf-8")

        self.assertIn("events_dropped: 3", content)

    def test_footer_includes_partial_flush_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=True)
            for i in range(60):
                log.write_line("runtime.event", f"event-{i}")
            log.write_footer()
            run_logging.flush_run_logs()
            content = path.read_text(encoding="utf-8")

        self.assertIn("partial_flushes: 1", content)

    def test_footer_omits_zero_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            log = run_logging.RunFileLog(path, background=False)
            log.write_line("runtime.event", "hello")
            log.write_footer()
            content = path.read_text(encoding="utf-8")

        self.assertNotIn("events_dropped", content)
        self.assertNotIn("partial_flushes", content)
        self.assertNotIn("backpressure", content)
