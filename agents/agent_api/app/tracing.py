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
    """Trace printer that also emits curated progress safe for end users."""

    def __init__(
        self,
        progress_callback: ProgressCallback,
        enabled: bool = DEBUG_TRACE,
        show_payloads: bool = DEBUG_PAYLOADS,
    ):
        super().__init__(enabled=enabled, show_payloads=show_payloads)
        self.progress_callback = progress_callback
        self._last_progress_key: Optional[str] = None

    def event(self, stage: str, message: str, **fields: Any) -> None:
        super().event(stage, message, **fields)
        progress = self._progress_for_event(stage, fields)
        if not progress:
            return

        progress_key = f"{progress['stage']}:{progress['message']}"
        if progress_key == self._last_progress_key:
            return

        self._last_progress_key = progress_key
        self.progress_callback(progress)

    def _progress_for_event(self, stage: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if stage == "runtime.start":
            if fields.get("resuming"):
                return self._progress("run_resumed", "Resuming the previous Jarvis run")
            return self._progress("run_started", "Agent started and opened a Jarvis run")

        if stage == "runtime.graph":
            return self._progress("graph_ready", "Loaded the agent graph")

        if stage == "graph.agent":
            turn = fields.get("turn")
            max_turns = fields.get("max_turns")
            if turn and max_turns:
                return self._progress("thinking", f"Thinking through the request (turn {turn}/{max_turns})")
            return self._progress("thinking", "Thinking through the request")

        if stage == "agent.request":
            return self._progress("model_request", "Asking DeepSeek what to do next")

        if stage == "agent.response":
            tool_calls = int(fields.get("tool_calls") or 0)
            if tool_calls > 0:
                return self._progress("model_tool_decision", f"DeepSeek requested {tool_calls} tool step(s)")
            if fields.get("has_content"):
                return self._progress("model_answer", "DeepSeek drafted an answer")
            return self._progress("model_response", "Received DeepSeek response")

        if stage == "graph.route":
            next_node = fields.get("next")
            if next_node == "tools":
                return self._progress("route_tools", "Routing to Todoist tools")
            if next_node == "hitl":
                return self._progress("route_clarification", "Jarvis needs one clarification")
            if next_node == "agent":
                return self._progress("route_agent", "Checking whether another step is needed")
            if next_node == "end":
                return self._progress("synthesizing", "Writing the final reply")

        if stage == "graph.tools":
            tool_calls = int(fields.get("tool_calls") or 0)
            return self._progress("tools_started", f"Preparing Todoist tool batch ({tool_calls} call(s))")

        if stage == "tools.batch":
            count = int(fields.get("count") or 0)
            return self._progress("tools_calling", f"Calling Todoist ({count} request(s))")

        if stage == "tool.start":
            if fields.get("mutating"):
                return self._progress("tool_update", "Preparing a Todoist update")
            return self._progress("tool_lookup", "Preparing a Todoist lookup")

        if stage == "tool.done":
            return self._progress("tool_done", "Todoist returned a result")

        if stage in {"tool.error", "tool.blocked"}:
            return self._progress("tool_issue", "Todoist step reported an issue")

        if stage == "graph.hitl":
            if "Interrupting" in str(fields):
                return self._progress("clarifying", "Pausing to ask for clarification")
            return self._progress("clarification", "Handling the clarification step")

        if stage == "runtime.done":
            if fields.get("has_error"):
                return self._progress("failed", "Something went wrong while running Jarvis")
            if fields.get("interrupted"):
                return self._progress("paused", "Paused for your clarification")
            return self._progress("done", "Done")

        return None

    @staticmethod
    def _progress(stage: str, message: str) -> Dict[str, Any]:
        return {"stage": stage, "message": message}


__all__ = ["ProgressCallback", "TracePrinter", "UserProgressTracePrinter", "NULL_TRACE"]
