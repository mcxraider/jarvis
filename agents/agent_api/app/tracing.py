"""Structured terminal tracing for local Jarvis debugging."""

import json
from typing import Any, Dict

from agents.agent_api.app.constants import DEBUG_PAYLOADS, DEBUG_TRACE


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

__all__ = ["TracePrinter", "NULL_TRACE"]
