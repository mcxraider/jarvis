"""Structured terminal tracing and safe user-facing progress events."""

import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from langsmith.run_helpers import get_current_run_tree

from agents.agent_api.app.constants import DEBUG_PAYLOADS, DEBUG_TRACE

ProgressCallback = Callable[[Dict[str, Any]], None]

_MAX_SPAN_EVENTS = 200  # ponytail: per-span cap; raise only if a real run exceeds it


def _emit_span_event(stage: str, message: str, fields: Dict[str, Any]) -> None:
    """Mirror a diagnostic event onto the active LangSmith span. Never raises."""
    try:
        run_tree = get_current_run_tree()
        if run_tree is None:
            return
        if run_tree.events and len(run_tree.events) >= _MAX_SPAN_EVENTS:
            return
        event: Dict[str, Any] = {
            "name": stage,
            "time": datetime.now(timezone.utc).isoformat(),
            "message": message,
        }
        for key, value in fields.items():
            if value is None:
                continue
            event[key] = (
                value
                if isinstance(value, (int, float, bool))
                else TracePrinter._preview(value, 180)
            )
        run_tree.add_event(event)
    except Exception:
        pass


class TracePrinter:
    """Structured terminal trace output for local debugging."""

    def __init__(self, enabled: bool = DEBUG_TRACE, show_payloads: bool = DEBUG_PAYLOADS):
        self.enabled = enabled
        self.show_payloads = show_payloads

    def section(self, title: str) -> None:
        if not self.enabled:
            return
        print(f"\n[{title}]")
        print("-" * (len(title) + 2))

    def event(self, stage: str, message: str, **fields: Any) -> None:
        # Mirrors to LangSmith regardless of self.enabled: `enabled` gates terminal
        # printing (JARVIS_DEBUG), not tracing.
        _emit_span_event(stage, message, fields)
        if not self.enabled:
            return
        suffix = self._format_fields(fields)
        print(f"{stage:<18} {message}{suffix}")

    def progress(self, fact: Dict[str, Any]) -> None:
        """Emit a structured, copy-free user-progress fact.

        Ordinary tracers deliberately ignore this. Streaming tracers override it
        to forward facts to the delivery layer without turning diagnostics into UI
        text.
        """
        return

    def reasoning_summary(self, text: str) -> None:
        """Emit intermediate model reasoning summary text to the user."""
        return

    def payload(self, stage: str, label: str, value: Any, limit: int = 900) -> None:
        if not self.enabled or not self.show_payloads:
            return
        print(f"{stage:<18} {label}: {self._preview(value, limit)}")

    def _format_fields(self, fields: Dict[str, Any]) -> str:
        clean_fields = {key: value for key, value in fields.items() if value is not None}
        if not clean_fields:
            return ""
        pairs = [f"{key}={self._preview(value, 180)}" for key, value in clean_fields.items()]
        return " | " + ", ".join(pairs)

    @staticmethod
    def _preview(value: Any, limit: int) -> str:
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, default=str, sort_keys=True)
            except TypeError:
                text = str(value)
        if len(text) > limit:
            return text[: limit - 3] + "..."
        return text


NULL_TRACE = TracePrinter(enabled=False)


def name_current_run(name: str) -> None:
    """Rename the active LangSmith span so trace names identify the actual work.

    Best-effort: a no-op when tracing is disabled, and never raises into a request.
    """
    try:
        run_tree = get_current_run_tree()
        if run_tree is not None:
            run_tree.name = name
    except Exception:
        pass


class UserProgressTracePrinter(TracePrinter):
    """Trace printer that forwards graph-owned structured progress facts."""

    def __init__(
        self,
        progress_callback: ProgressCallback,
        enabled: bool = DEBUG_TRACE,
        show_payloads: bool = DEBUG_PAYLOADS,
    ):
        super().__init__(enabled=enabled, show_payloads=show_payloads)
        self.progress_callback = progress_callback
        self._last_progress_key: Optional[str] = None

    def progress(self, fact: Dict[str, Any]) -> None:
        # Facts are intentionally small, safe dictionaries. Stable JSON makes
        # repeated loop/tool facts harmless without coupling this tracer to copy.
        progress_key = json.dumps(fact, sort_keys=True, default=str)
        if progress_key == self._last_progress_key:
            return
        self._last_progress_key = progress_key
        self.progress_callback({"fact": fact})

    def reasoning_summary(self, text: str) -> None:
        if not text or not text.strip():
            return
        self.progress_callback({"reasoning_summary": text})


__all__ = ["ProgressCallback", "TracePrinter", "UserProgressTracePrinter", "NULL_TRACE", "name_current_run"]
