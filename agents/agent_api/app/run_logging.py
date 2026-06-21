"""Per-run readable file logs for Jarvis agent invocations.

Every call to run_jarvis() gets its own timestamped log file under logs/,
independent of terminal trace settings (JARVIS_DEBUG), so CLI and
Telegram/API runs alike leave a persistent, human-readable record on disk.
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = _PROJECT_ROOT / "logs"

PreviewFn = Callable[..., str]

_TRUEY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def build_run_log_path(thread_id: str, now: Optional[datetime] = None) -> Path:
    """Build a per-run log path whose filename sorts chronologically."""

    now = now or datetime.now()
    short_thread = (thread_id or "norun").replace("-", "")[:8]
    timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
    return LOG_DIR / f"jarvis_run_{timestamp}_{short_thread}.log"


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


def open_run_log(thread_id: str) -> Optional["RunFileLog"]:
    """Open a per-run log file, or return None when file logging is disabled."""

    if not run_file_log_enabled():
        return None
    return RunFileLog(build_run_log_path(thread_id))


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
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._append(f"{timestamp} | {stage:<20} | {message}{extra}")

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
        self.run_log.write_line(stage, message, self._format_fields(fields, self._preview_fn()))

    def payload(self, stage: str, label: str, value: Any, limit: int = 900) -> None:
        self._tracer.payload(stage, label, value, limit=limit)
        preview = self._preview_fn()(value, limit)
        self.run_log.write_line(stage, f"[payload] {label}: {preview}")

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


__all__ = [
    "RunFileLog",
    "FileLoggingTracer",
    "build_run_log_path",
    "open_run_log",
    "run_file_log_enabled",
    "LOG_DIR",
]
