"""Todoist REST API client using only the Python standard library."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from langsmith import traceable

from agents.agent_api.app.config import settings
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter

TODOIST_REST_BASE_URL = settings.todoist_rest_base_url
TODOIST_COMPLETED_BY_COMPLETION_DATE_URL = (
    f"{TODOIST_REST_BASE_URL}/tasks/completed/by_completion_date"
)


class TodoistApiClient:
    """Direct Todoist API client using only the Python stdlib."""

    def __init__(self, api_key: Optional[str] = None, tracer: Optional[TracePrinter] = None):
        self.api_key = api_key or os.getenv("TODOIST_API_KEY")
        self.tracer = tracer or NULL_TRACE

    @traceable(
        name="todoist_api_request",
        run_type="tool",
        process_inputs=lambda inputs: {
            "url": inputs.get("url"),
            "method": inputs.get("method", "GET"),
            "has_payload": inputs.get("payload") is not None,
        },
    )
    def _request(
        self,
        url: str,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
        content_type: bool = True,
    ) -> Any:
        if not self.api_key:
            raise RuntimeError("TODOIST_API_KEY is required for real Todoist tool execution.")

        data = None
        headers = {"Authorization": f"Bearer {self.api_key}"}

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            if content_type:
                headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            self.tracer.event(
                "todoist.request",
                "Sending Todoist API request.",
                method=method,
                url=url,
                has_payload=payload is not None,
            )
            self.tracer.payload("todoist.payload", "request", payload)
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                self.tracer.event(
                    "todoist.response",
                    "Received Todoist API response.",
                    status=response.status,
                    has_body=bool(body),
                )
                if response.status == 204 or not body:
                    return None
                parsed = json.loads(body)
                self.tracer.payload("todoist.payload", "response", parsed)
                return parsed
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            self.tracer.event(
                "todoist.error",
                "Todoist API returned an HTTP error.",
                status=error.code,
            )
            raise RuntimeError(f"Todoist API error ({error.code}): {body}") from error
        except urllib.error.URLError as error:
            self.tracer.event("todoist.error", "Todoist API connection failed.", error=error.reason)
            raise RuntimeError(f"Todoist API connection error: {error.reason}") from error

    def add_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks", "POST", _without_none(arguments))

    def get_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks/{arguments['task_id']}")

    def get_tasks(self, arguments: Dict[str, Any]) -> Any:
        params = _query_params(_without_none(arguments), comma_join_keys={"ids"})
        suffix = f"?{params}" if params else ""
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks{suffix}")

    def update_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        arguments = _without_none(arguments)
        task_id = arguments.pop("task_id")
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks/{task_id}", "POST", arguments)

    def complete_task(self, arguments: Dict[str, Any]) -> Any:
        self._request(f"{TODOIST_REST_BASE_URL}/tasks/{arguments['task_id']}/close", "POST")
        return {"success": True, "message": f"Task {arguments['task_id']} marked as completed"}

    def delete_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        self._request(f"{TODOIST_REST_BASE_URL}/tasks/{arguments['task_id']}", "DELETE")
        return {"success": True, "message": f"Task {arguments['task_id']} deleted permanently"}

    def get_completed_todoist_tasks_by_completion_date(self, arguments: Dict[str, Any]) -> Any:
        arguments = _with_default_completion_date_range(_without_none(arguments))
        params = _query_params(arguments)
        suffix = f"?{params}" if params else ""
        data = self._request(f"{TODOIST_COMPLETED_BY_COMPLETION_DATE_URL}{suffix}")
        if not isinstance(data, dict):
            return {"items": [], "next_cursor": None}
        return {"items": data.get("items", []), "next_cursor": data.get("next_cursor")}


def _without_none(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop None values before sending arguments to Todoist."""

    return {key: value for key, value in data.items() if value is not None}


def _with_default_completion_date_range(data: Dict[str, Any]) -> Dict[str, Any]:
    """Default completed-task queries to the last 30 days in UTC."""

    if "until" in data:
        until = datetime.fromisoformat(data["until"].replace("Z", "+00:00"))
    else:
        until = datetime.now(timezone.utc)

    if "since" in data:
        since = datetime.fromisoformat(data["since"].replace("Z", "+00:00"))
    else:
        since = until - timedelta(days=30)

    return {
        **data,
        "since": since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "until": until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _query_params(
    data: Dict[str, Any],
    comma_join_keys: Optional[set] = None,
) -> str:
    """Build a Todoist query string, comma-joining selected list parameters."""

    comma_join_keys = comma_join_keys or set()
    encoded: Dict[str, Any] = {}
    for key, value in data.items():
        if key in comma_join_keys and isinstance(value, list):
            encoded[key] = ",".join(str(item) for item in value)
        else:
            encoded[key] = value
    return urllib.parse.urlencode(encoded)


__all__ = [
    "TODOIST_COMPLETED_BY_COMPLETION_DATE_URL",
    "TODOIST_REST_BASE_URL",
    "TodoistApiClient",
]
