"""Per-run readable file logs for Jarvis agent invocations.

Every call to run_jarvis() gets its own readable log file under logs/,
independent of terminal trace settings (JARVIS_DEBUG), so CLI and
Telegram/API runs alike leave a persistent, human-readable record on disk.
"""

import os
import re
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
    return RunFileLog(build_run_log_path(thread_id, identity=identity))


class RunFileLog:
    """Appends readable, timestamped lines to one per-run log file."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_header(self, **fields: Any) -> None:
        lines = ["=" * 78, "Jarvis run", "=" * 78]
        lines.extend(f"{key}: {value}" for key, value in fields.items())
        lines.append("-" * 78)
        self._append("\n".join(lines))

    def write_footer(self, **fields: Any) -> None:
        lines = ["-" * 78, "Run finished"]
        lines.extend(f"{key}: {value}" for key, value in fields.items())
        lines.append("=" * 78)
        self._append("\n".join(lines))

    def write_line(self, stage: str, message: str, extra: str = "") -> None:
        timestamp = format_singapore_log_timestamp()
        self._append(f"{timestamp} | {stage:<20} | {message}{extra}")

    def write_messages_dump(self, label: str, messages: List[Dict[str, Any]]) -> None:
        """Write a full messages array as indented JSON, clearly demarcated.

        Used for the final LLM call context — the complete conversation the model
        saw when it decided to answer. Not truncated; these logs are local-only.
        """
        import json as _json

        separator = "~" * 78
        lines = [
            separator,
            f"MESSAGES DUMP: {label}",
            f"message_count: {len(messages)}",
            separator,
            _json.dumps(messages, indent=2, ensure_ascii=False, default=str),
            separator,
        ]
        self._append("\n".join(lines))

    def _append(self, text: str) -> None:
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


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
        readable = _format_payload_readable(value)
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


def _format_payload_readable(value: Any) -> str:
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
        return "\n" + _INDENT + text[:500] + ("..." if len(text) > 500 else "")

    if isinstance(value, str):
        if len(value) <= 120:
            return " " + value
        return "\n" + _INDENT + value[:500] + "..."

    text = str(value)
    if len(text) > 900:
        return " " + text[:897] + "..."
    return " " + text


__all__ = [
    "RunFileLog",
    "RunLogIdentity",
    "FileLoggingTracer",
    "build_run_log_path",
    "format_singapore_log_iso",
    "format_singapore_log_timestamp",
    "open_run_log",
    "run_file_log_enabled",
    "to_singapore_time",
    "LOG_DIR",
]
