"""Structured terminal tracing and safe user-facing progress events."""

import json
from typing import Any, Callable, Dict, Optional

from agents.agent_api.app.constants import DEBUG_PAYLOADS, DEBUG_TRACE

ProgressCallback = Callable[[Dict[str, Any]], None]


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

    def narration(self, text: str) -> None:
        """Emit intermediate model narration text to the user."""
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

    def narration(self, text: str) -> None:
        if not text or not text.strip():
            return
        self.progress_callback({"narration": text})


__all__ = ["ProgressCallback", "TracePrinter", "UserProgressTracePrinter", "NULL_TRACE"]
