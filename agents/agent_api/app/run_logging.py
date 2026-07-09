"""Per-run readable file logs for Jarvis agent invocations.

Every call to run_jarvis() gets its own readable log file under logs/,
independent of terminal trace settings (JARVIS_DEBUG), so CLI and
Telegram/API runs alike leave a persistent, human-readable record on disk.
"""

import concurrent.futures
import logging
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from agents.agent_api.app.user_context.identity import TelegramIdentity

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = _PROJECT_ROOT / "logs"
SINGAPORE_TIME_ZONE = ZoneInfo("Asia/Singapore")

PreviewFn = Callable[..., str]

_TRUEY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}
_CLI_LOG_NAME = "jer_jerryyy"
_CLI_TELEGRAM_USER_ID = 701122767
MAX_BUFFER_EVENTS = 500
MAX_BUFFER_BYTES = 2 * 1024 * 1024
MAX_LOG_DIR_MB = 100
MAX_LOG_AGE_DAYS = 14
FLUSH_INTERVAL_EVENTS = 50
BACKPRESSURE_THRESHOLD = 5
MAX_PAYLOAD_BYTES = 64_000
MAX_STRING_LENGTH = 8_000

logger = logging.getLogger(__name__)
_log_writer_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="run-log-writer",
)
_pending_futures: set[concurrent.futures.Future[None]] = set()
_pending_futures_lock = threading.Lock()


@dataclass
class LogWriterStats:
    events_accepted: int = 0
    events_dropped: int = 0
    events_truncated: int = 0
    bytes_accepted: int = 0
    bytes_dropped: int = 0
    writes_completed: int = 0
    writes_failed: int = 0
    last_write_time: float | None = None
    pending_futures: int = 0
    partial_flushes: int = 0
    backpressure_events: int = 0


_writer_stats = LogWriterStats()
_writer_stats_lock = threading.Lock()
_last_backpressure_warning: float = 0.0
_BACKPRESSURE_RATE_LIMIT_SECONDS = 60.0

_CLEANUP_DEBOUNCE_SECONDS = 3600
_last_cleanup_time: float = 0.0
_cleanup_lock = threading.Lock()


def cleanup_old_logs(
    base_dir: Path,
    max_age_days: int = MAX_LOG_AGE_DAYS,
    max_total_mb: int = MAX_LOG_DIR_MB,
) -> int:
    """Delete old/oversized run-log files. Returns count of files deleted."""
    if not base_dir.exists():
        return 0

    now = time.time()
    active_cutoff = now - 3600
    age_cutoff = now - (max_age_days * 86400)
    max_total_bytes = max_total_mb * 1024 * 1024

    deleted = 0
    remaining: list[tuple[float, int, Path]] = []

    for file in base_dir.rglob("*.log"):
        if not file.is_file():
            continue
        try:
            stat = file.stat()
        except OSError:
            continue
        if stat.st_mtime >= active_cutoff:
            remaining.append((stat.st_mtime, stat.st_size, file))
            continue
        if stat.st_mtime < age_cutoff:
            try:
                file.unlink()
                deleted += 1
            except OSError:
                pass
        else:
            remaining.append((stat.st_mtime, stat.st_size, file))

    total_size = sum(size for _, size, _ in remaining)
    if total_size > max_total_bytes:
        remaining.sort(key=lambda entry: entry[0])
        for mtime, size, file in remaining:
            if total_size <= max_total_bytes:
                break
            if mtime >= active_cutoff:
                continue
            try:
                file.unlink()
                total_size -= size
                deleted += 1
            except OSError:
                pass

    return deleted


def _schedule_log_cleanup() -> None:
    """Submit cleanup to the writer pool with an hourly debounce."""
    global _last_cleanup_time
    now = time.time()
    with _cleanup_lock:
        if now - _last_cleanup_time < _CLEANUP_DEBOUNCE_SECONDS:
            return
        _last_cleanup_time = now

    def _do_cleanup() -> None:
        try:
            deleted = cleanup_old_logs(LOG_DIR)
            if deleted > 0:
                logger.info("Log cleanup completed.", extra={"files_deleted": deleted})
        except Exception:
            logger.exception("Log cleanup failed.")

    _submit_log_write(_do_cleanup)


def get_log_writer_stats() -> LogWriterStats:
    """Return a snapshot of writer statistics."""
    with _writer_stats_lock:
        with _pending_futures_lock:
            _writer_stats.pending_futures = len(_pending_futures)
        return LogWriterStats(
            events_accepted=_writer_stats.events_accepted,
            events_dropped=_writer_stats.events_dropped,
            events_truncated=_writer_stats.events_truncated,
            bytes_accepted=_writer_stats.bytes_accepted,
            bytes_dropped=_writer_stats.bytes_dropped,
            writes_completed=_writer_stats.writes_completed,
            writes_failed=_writer_stats.writes_failed,
            last_write_time=_writer_stats.last_write_time,
            pending_futures=_writer_stats.pending_futures,
            partial_flushes=_writer_stats.partial_flushes,
            backpressure_events=_writer_stats.backpressure_events,
        )


def log_writer_healthy() -> bool:
    """Return False if the writer thread appears stalled or dead."""
    with _writer_stats_lock:
        with _pending_futures_lock:
            pending = len(_pending_futures)
        if pending == 0:
            return True
        if _writer_stats.last_write_time is None:
            return pending == 0
        stale = (time.monotonic() - _writer_stats.last_write_time) > 30.0
        return not stale


def reset_writer_stats() -> None:
    """Reset stats for test isolation."""
    global _writer_stats, _last_backpressure_warning
    with _writer_stats_lock:
        _writer_stats = LogWriterStats()
    _last_backpressure_warning = 0.0


@dataclass(frozen=True)
class RunLogIdentity:
    request_source: str = "api"
    identity: Optional[TelegramIdentity] = None
    # Deprecated compatibility fields for direct callers and historical tests.
    telegram_user_id: Optional[int] = None
    telegram_username: Optional[str] = None
    telegram_first_name: Optional[str] = None


def to_singapore_time(value: Optional[datetime] = None) -> datetime:
    """Return a datetime converted to Asia/Singapore for log display only."""

    timestamp = value or datetime.now().astimezone()
    if timestamp.tzinfo is None:
        timestamp = timestamp.astimezone()
    return timestamp.astimezone(SINGAPORE_TIME_ZONE)


def format_singapore_log_timestamp(value: Optional[datetime] = None) -> str:
    """Format a log timestamp in Asia/Singapore time."""

    return to_singapore_time(value).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def format_singapore_log_iso(value: datetime) -> str:
    """Format a header/footer timestamp in Asia/Singapore time."""

    return to_singapore_time(value).isoformat(timespec="seconds")


def sanitize_log_segment(value: Optional[str], fallback: str = "telegram") -> str:
    """Return a filesystem-safe lowercase-ish path segment."""

    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (value or "").strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or fallback


def _log_identity_parts(identity: Optional[RunLogIdentity]) -> tuple[str, str]:
    if identity and identity.identity is not None:
        external = identity.identity
        display_name = external.username or "telegram"
        return sanitize_log_segment(display_name), sanitize_log_segment(
            str(external.telegram_id), fallback="unknown"
        )
    if identity and identity.request_source == "telegram" and identity.telegram_user_id is not None:
        display_name = identity.telegram_username or identity.telegram_first_name or "telegram"
        return sanitize_log_segment(display_name), str(identity.telegram_user_id)
    return _CLI_LOG_NAME, str(_CLI_TELEGRAM_USER_ID)


def _thread_suffix(thread_id: str) -> str:
    compact = thread_id.replace("-", "")
    return compact[-5:] if compact else "norun"


def build_run_log_path(
    thread_id: str,
    now: Optional[datetime] = None,
    identity: Optional[RunLogIdentity] = None,
) -> Path:
    """Build a per-run log path grouped by user identity."""

    safe_name, identity_subject = _log_identity_parts(identity)
    folder = f"{safe_name}-{identity_subject}"
    thread_suffix = sanitize_log_segment(_thread_suffix(thread_id), fallback="norun")
    return LOG_DIR / folder / f"{safe_name}_{thread_suffix}.log"


def run_file_log_enabled() -> bool:
    """Whether per-run file logs should be written for this process.

    Enabled by default for real CLI and API/Telegram runs. Disabled
    automatically under pytest so the test suite does not litter ``logs/``.
    Override with ``JARVIS_RUN_FILE_LOG`` ("0"/"false" to force off,
    "1"/"true" to force on even under pytest).
    """

    override = os.getenv("JARVIS_RUN_FILE_LOG")
    if override is not None:
        normalized = override.strip().lower()
        if normalized in _TRUEY:
            return True
        if normalized in _FALSEY:
            return False
    return "PYTEST_CURRENT_TEST" not in os.environ


def open_run_log(thread_id: str, identity: Optional[RunLogIdentity] = None) -> Optional["RunFileLog"]:
    """Open a per-run log file, or return None when file logging is disabled."""

    if not run_file_log_enabled():
        return None
    background = not (
        (identity is not None and identity.request_source == "cli")
        or "PYTEST_CURRENT_TEST" in os.environ
    )
    _schedule_log_cleanup()
    return RunFileLog(build_run_log_path(thread_id, identity=identity), background=background)


def flush_run_logs() -> None:
    """Block until all run-log writes submitted so far have completed."""

    try:
        _submit_log_write(lambda: None).result()
    except RuntimeError:
        return


def shutdown_run_logs(timeout: float = 5.0) -> None:
    """Drain pending run-log writes up to timeout, then stop accepting new work."""

    global _log_writer_pool
    try:
        sentinel = _submit_log_write(lambda: None)
        sentinel.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        logger.warning("Timed out draining run-log writes.", extra={"timeout": timeout})
    except RuntimeError:
        pass
    _log_writer_pool.shutdown(wait=False, cancel_futures=False)


def reset_log_writer() -> None:
    """Replace the run-log executor with a fresh instance for test isolation."""

    global _log_writer_pool, _last_cleanup_time
    _log_writer_pool.shutdown(wait=False, cancel_futures=True)
    with _pending_futures_lock:
        _pending_futures.clear()
    _log_writer_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="run-log-writer",
    )
    with _cleanup_lock:
        _last_cleanup_time = 0.0
    reset_writer_stats()


def _check_writer_backpressure() -> None:
    """Emit a rate-limited warning when the write queue is backed up."""
    global _last_backpressure_warning
    with _pending_futures_lock:
        depth = len(_pending_futures)
    if depth <= BACKPRESSURE_THRESHOLD:
        return
    now = time.monotonic()
    if now - _last_backpressure_warning < _BACKPRESSURE_RATE_LIMIT_SECONDS:
        return
    _last_backpressure_warning = now
    with _writer_stats_lock:
        _writer_stats.backpressure_events += 1
    logger.warning(
        "run_log.writer.backpressure",
        extra={"queue_depth": depth},
    )


def _submit_log_write(fn: Callable[[], None]) -> concurrent.futures.Future[None]:
    _check_writer_backpressure()

    def _run_safely() -> None:
        try:
            fn()
        except Exception:
            logger.exception("Run-log background write failed.")

    future = _log_writer_pool.submit(_run_safely)
    with _pending_futures_lock:
        _pending_futures.add(future)

    def _discard(done: concurrent.futures.Future[None]) -> None:
        with _pending_futures_lock:
            _pending_futures.discard(done)

    future.add_done_callback(_discard)
    return future


class RunFileLog:
    """Appends readable, timestamped lines to one per-run log file."""

    def __init__(self, path: Path, *, background: bool = True):
        self.path = path
        self._background = background
        self._buffer: list[str] = []
        self._buffer_bytes = 0
        self._buffer_events = 0
        self._total_events = 0
        self._events_dropped = 0
        self._partial_flushed = False
        self._partial_flush_count = 0
        self._drop_warned = False
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_header(self, **fields: Any) -> None:
        lines = ["=" * 78, "Jarvis run", "=" * 78]
        lines.extend(f"{key}: {value}" for key, value in fields.items())
        lines.append("-" * 78)
        self._append("\n".join(lines), preserve=True)

    def write_footer(self, **fields: Any) -> None:
        if self._events_dropped > 0:
            fields = {**fields, "events_dropped": self._events_dropped}
        if self._partial_flush_count > 0:
            fields = {**fields, "partial_flushes": self._partial_flush_count}
        with _writer_stats_lock:
            bp = _writer_stats.backpressure_events
        if bp > 0:
            fields = {**fields, "writer_backpressure_events": bp}
        lines = ["-" * 78, "Run finished"]
        lines.extend(f"{key}: {value}" for key, value in fields.items())
        lines.append("=" * 78)
        self._append("\n".join(lines), preserve=True)
        content = self._snapshot_buffer()
        if self._background:
            _submit_log_write(lambda: self._flush_to_disk(content))
        else:
            self._flush_to_disk(content)
        self._buffer.clear()
        self._buffer_bytes = 0

    def write_line(self, stage: str, message: str, extra: str = "") -> None:
        timestamp = format_singapore_log_timestamp()
        self._append(
            f"{timestamp} | {stage:<20} | {message}{extra}",
            preserve=_is_high_value_event(stage, message),
        )

    def write_messages_dump(self, label: str, messages: List[Dict[str, Any]]) -> None:
        """Write a full messages array as indented JSON, clearly demarcated.

        Used for the final LLM call context — the complete conversation the model
        saw when it decided to answer. Not truncated; these logs are local-only.
        """
        import json as _json

        payload = _json.dumps(messages, indent=2, ensure_ascii=False, default=str)
        separator = "~" * 78
        lines = [
            separator,
            f"MESSAGES DUMP: {label}",
            f"message_count: {len(messages)}",
            separator,
            payload,
            separator,
        ]
        self._append("\n".join(lines))

    def write_crash(self, exc: BaseException) -> None:
        """Synchronously persist a partial run log when graph execution crashes."""

        # Drain any in-flight partial flush so we don't interleave on the file.
        try:
            _submit_log_write(lambda: None).result(timeout=2.0)
        except (concurrent.futures.TimeoutError, RuntimeError, Exception):
            pass

        timestamp = format_singapore_log_timestamp()
        separator = "!" * 78
        lines = [
            separator,
            "RUN CRASH",
            f"timestamp: {timestamp}",
            f"error_type: {type(exc).__name__}",
            f"error: {exc}",
            separator,
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip(),
            separator,
        ]
        self._append("\n".join(lines), preserve=True)
        self._flush_to_disk(self._snapshot_buffer())

    def _append(self, text: str, *, preserve: bool = False) -> None:
        entry = text + "\n"
        entry_bytes = len(entry.encode("utf-8"))
        if entry_bytes > MAX_PAYLOAD_BYTES:
            original_bytes = entry_bytes
            entry = text[:MAX_PAYLOAD_BYTES] + f" [truncated, original_bytes={original_bytes}]\n"
            entry_bytes = len(entry.encode("utf-8"))
            with _writer_stats_lock:
                _writer_stats.events_truncated += 1
        would_exceed_events = self._buffer_events >= MAX_BUFFER_EVENTS
        would_exceed_bytes = self._buffer_bytes + entry_bytes > MAX_BUFFER_BYTES
        if not preserve and (would_exceed_events or would_exceed_bytes):
            self._events_dropped += 1
            with _writer_stats_lock:
                _writer_stats.events_dropped += 1
                _writer_stats.bytes_dropped += entry_bytes
            if not self._drop_warned:
                self._drop_warned = True
                reason = "byte cap" if would_exceed_bytes else "event cap"
                logger.warning(
                    "run_log.events_dropped",
                    extra={"path": str(self.path), "reason": reason},
                )
            return
        self._buffer.append(entry)
        self._buffer_events += 1
        self._buffer_bytes += entry_bytes
        self._total_events += 1
        with _writer_stats_lock:
            _writer_stats.events_accepted += 1
            _writer_stats.bytes_accepted += entry_bytes
        if (
            self._background
            and self._buffer_events >= FLUSH_INTERVAL_EVENTS
            and self._buffer_events % FLUSH_INTERVAL_EVENTS == 0
        ):
            snapshot = list(self._buffer)
            self._buffer.clear()
            self._buffer_bytes = 0
            self._buffer_events = 0
            self._partial_flushed = True
            self._partial_flush_count += 1
            _submit_log_write(lambda: self._flush_partial(snapshot))

    def _snapshot_buffer(self) -> str:
        return "".join(self._buffer)

    def _flush_partial(self, lines: list[str]) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.writelines(lines)
            with _writer_stats_lock:
                _writer_stats.partial_flushes += 1
                _writer_stats.writes_completed += 1
                _writer_stats.last_write_time = time.monotonic()
        except Exception:
            with _writer_stats_lock:
                _writer_stats.writes_failed += 1
            logger.warning("Run-log partial flush failed.", extra={"path": str(self.path)})
            raise

    def _flush_to_disk(self, content: Optional[str] = None) -> None:
        payload = self._snapshot_buffer() if content is None else content
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(payload)
            with _writer_stats_lock:
                _writer_stats.writes_completed += 1
                _writer_stats.last_write_time = time.monotonic()
        except Exception:
            with _writer_stats_lock:
                _writer_stats.writes_failed += 1
            logger.warning("Run-log flush failed.", extra={"path": str(self.path)})
            raise


def _is_high_value_event(stage: str, message: str) -> bool:
    text = f"{stage} {message}".lower()
    return any(marker in text for marker in ("error", "exception", "crash", "failed", "fallback"))


class FileLoggingTracer:
    """Wraps any TracePrinter-like tracer to also append to a run log file.

    Terminal output keeps obeying the wrapped tracer's enabled/show_payloads
    settings; the file always receives the full event stream so every run
    leaves a complete record regardless of JARVIS_DEBUG.
    """

    def __init__(self, tracer: Any, run_log: RunFileLog):
        self._tracer = tracer
        self.run_log = run_log

    def section(self, title: str) -> None:
        self._tracer.section(title)
        self.run_log.write_line("section", f"=== {title} ===")

    def event(self, stage: str, message: str, **fields: Any) -> None:
        self._tracer.event(stage, message, **fields)
        extra = _format_event_fields_readable(fields)
        self.run_log.write_line(stage, message, extra)

    def payload(self, stage: str, label: str, value: Any, limit: int = 900) -> None:
        self._tracer.payload(stage, label, value, limit=limit)
        readable = _format_payload_readable(value, limit=limit)
        self.run_log.write_line(stage, f"[payload] {label}:{readable}")

    def _preview_fn(self) -> PreviewFn:
        return getattr(self._tracer, "_preview", _fallback_preview)

    @staticmethod
    def _format_fields(fields: Dict[str, Any], preview: PreviewFn) -> str:
        clean_fields = {key: value for key, value in fields.items() if value is not None}
        if not clean_fields:
            return ""
        pairs = [f"{key}={preview(value, 180)}" for key, value in clean_fields.items()]
        return " | " + ", ".join(pairs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tracer, name)


def _fallback_preview(value: Any, limit: int = 180) -> str:
    text = str(value)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _format_event_fields_readable(fields: Dict[str, Any]) -> str:
    """Format event **fields as multi-line indented key: value lines for file logs."""
    clean = {k: v for k, v in fields.items() if v is not None}
    if not clean:
        return ""
    lines = []
    for key, value in clean.items():
        formatted = _format_value(value, _INDENT)
        if "\n" in formatted:
            lines.append(f"{_INDENT}{key}:{formatted}")
        else:
            lines.append(f"{_INDENT}{key}: {formatted}")
    return "\n" + "\n".join(lines)


_INDENT = "      "
_NESTED_INDENT = "            "
_EMPTY_VALUES = (None, "", [], {}, False)


def _format_value(value: Any, indent: str = _INDENT) -> str:
    """Format a single value for the readable payload output."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        nested_indent = indent + "      "
        return _format_dict_block(value, nested_indent)
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(isinstance(item, (str, int, float, bool)) for item in value):
            return "[" + ", ".join(str(item) for item in value) + "]"
        if all(isinstance(item, dict) for item in value):
            nested_indent = indent + "      "
            blocks = []
            for item in value[:10]:
                blocks.append(_format_dict_block(item, nested_indent))
            header = f"\n{indent}({len(value)} items)"
            result = header + "\n".join(blocks)
            if len(value) > 10:
                result += f"\n{indent}... and {len(value) - 10} more"
            return result
        import json as _json
        return _json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _format_dict_block(data: Dict[str, Any], indent: str) -> str:
    """Format a nested dict as multi-line indented output, stripping empty values."""
    lines = []
    for key, value in data.items():
        if value in _EMPTY_VALUES:
            continue
        formatted = _format_value(value, indent)
        if "\n" in formatted:
            lines.append(f"\n{indent}{key}:{formatted}")
        else:
            lines.append(f"\n{indent}{key}: {formatted}")
    return "".join(lines) if lines else " (empty)"


def _format_dict_readable(data: Dict[str, Any]) -> str:
    """Format a dict as indented key: value lines, stripping empty values."""
    lines = []
    for key, value in data.items():
        if value in _EMPTY_VALUES:
            continue
        formatted = _format_value(value, _INDENT)
        if "\n" in formatted:
            lines.append(f"{_INDENT}{key}:{formatted}")
        else:
            lines.append(f"{_INDENT}{key}: {formatted}")
    return "\n".join(lines) if lines else f"{_INDENT}(empty)"


def _format_payload_readable(value: Any, limit: int = 900) -> str:
    """Convert a payload value to a human-readable multi-line format.

    Used by FileLoggingTracer for per-run file logs. Strips null/empty fields
    from dicts and formats as indented YAML-like output.
    """
    if isinstance(value, dict):
        return "\n" + _format_dict_readable(value)

    if isinstance(value, list):
        if not value:
            return " (empty list)"
        if all(isinstance(item, dict) for item in value):
            header = f"\n{_INDENT}({len(value)} items)"
            blocks = []
            for item in value[:10]:
                blocks.append(_format_dict_readable(item))
            result = header + "\n" + f"\n\n".join(blocks)
            if len(value) > 10:
                result += f"\n{_INDENT}... and {len(value) - 10} more"
            return result
        import json as _json
        text = _json.dumps(value, ensure_ascii=False, default=str)
        if len(text) <= 120:
            return " " + text
        if limit <= 0 or len(text) <= limit:
            return "\n" + _INDENT + text
        return "\n" + _INDENT + text[:limit] + "..."

    if isinstance(value, str):
        if len(value) <= 120:
            return " " + value
        if limit <= 0 or len(value) <= limit:
            return "\n" + _INDENT + value
        return "\n" + _INDENT + value[:limit] + "..."

    text = str(value)
    if limit > 0 and len(text) > limit:
        return " " + text[:limit] + "..."
    return " " + text


__all__ = [
    "FLUSH_INTERVAL_EVENTS",
    "LogWriterStats",
    "MAX_BUFFER_BYTES",
    "MAX_BUFFER_EVENTS",
    "MAX_LOG_AGE_DAYS",
    "MAX_LOG_DIR_MB",
    "RunFileLog",
    "RunLogIdentity",
    "FileLoggingTracer",
    "build_run_log_path",
    "cleanup_old_logs",
    "flush_run_logs",
    "format_singapore_log_iso",
    "format_singapore_log_timestamp",
    "get_log_writer_stats",
    "log_writer_healthy",
    "open_run_log",
    "reset_log_writer",
    "reset_writer_stats",
    "run_file_log_enabled",
    "shutdown_run_logs",
    "to_singapore_time",
    "LOG_DIR",
]
