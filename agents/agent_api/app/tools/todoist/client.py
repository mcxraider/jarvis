"""Todoist REST API client using only the Python standard library."""

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional

from langsmith import traceable

from agents.agent_api.app.config import settings
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter

TODOIST_REST_BASE_URL = settings.todoist_rest_base_url
TODOIST_COMPLETED_BY_COMPLETION_DATE_URL = (
    f"{TODOIST_REST_BASE_URL}/tasks/completed/by_completion_date"
)

TODOIST_HTTP_ERROR_KIND_BY_STATUS = {
    400: "validation",
    401: "auth",
    403: "auth",
    404: "not-found",
    408: "transient",
    410: "deprecated",
    422: "validation",
    429: "rate-limit",
    500: "transient",
    502: "transient",
    503: "transient",
    504: "transient",
}

TODOIST_ERROR_MESSAGES = {
    "rate-limit": "Todoist rate limit reached. Please try again shortly.",
    "transient": "Todoist is temporarily unavailable. Please try again shortly.",
    "auth": "Todoist authentication failed. Check the Todoist API configuration.",
    "validation": "Todoist rejected the request as invalid.",
    "not-found": "Todoist could not find the requested resource.",
    "deprecated": "This Todoist endpoint is no longer available.",
}


@dataclass
class TodoistApiError(Exception):
    """Structured Todoist failure safe to route through tool results."""

    kind: str
    message: str
    status_code: Optional[int] = None
    retryable: bool = False
    operation: str = "todoist.request"
    url: str = ""
    method: str = "GET"
    attempts: int = 1
    retry_after_seconds: Optional[float] = None

    def __str__(self) -> str:
        return self.message

    def to_classifier_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "source": "todoist",
            "kind": self.kind,
            "retryable": self.retryable,
            "operation": self.operation,
            "method": self.method,
            "attempts": self.attempts,
        }
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.retry_after_seconds is not None:
            payload["retry_after_seconds"] = self.retry_after_seconds
        return payload


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
            raise TodoistApiError(
                kind="auth",
                message=TODOIST_ERROR_MESSAGES["auth"],
                retryable=False,
                url=url,
                method=method,
            )

        data = None
        headers = {"Authorization": f"Bearer {self.api_key}"}

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            if content_type:
                headers["Content-Type"] = "application/json"

        max_attempts = max(1, settings.todoist_max_retry_attempts)
        retry_deadline = time.monotonic() + max(0.0, settings.todoist_retry_total_timeout_seconds)

        for attempt in range(1, max_attempts + 1):
            self.tracer.event(
                "todoist.request",
                "Sending Todoist API request.",
                method=method,
                url=url,
                has_payload=payload is not None,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            self.tracer.payload("todoist.payload", "request", payload)

            request = urllib.request.Request(url, data=data, headers=headers, method=method)

            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read().decode("utf-8")
                    self.tracer.event(
                        "todoist.response",
                        "Received Todoist API response.",
                        status=response.status,
                        has_body=bool(body),
                        attempt=attempt,
                    )
                    if response.status == 204 or not body:
                        return None
                    parsed = json.loads(body)
                    self.tracer.payload("todoist.payload", "response", parsed)
                    return parsed
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
                api_error = _todoist_http_error(error, url, method, attempt)
                self.tracer.event(
                    "todoist.error",
                    "Todoist API returned an HTTP error.",
                    status=error.code,
                    kind=api_error.kind,
                    retryable=api_error.retryable,
                    attempt=attempt,
                )
                self.tracer.payload(
                    "todoist.payload",
                    "error",
                    {"status": error.code, "body": body},
                )
                if _should_retry(api_error, attempt, max_attempts, retry_deadline):
                    _sleep_before_retry(api_error, attempt, retry_deadline)
                    continue
                raise api_error from error
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                api_error = TodoistApiError(
                    kind="transient",
                    message=TODOIST_ERROR_MESSAGES["transient"],
                    retryable=True,
                    url=url,
                    method=method,
                    attempts=attempt,
                )
                self.tracer.event(
                    "todoist.error",
                    "Todoist API connection failed.",
                    kind=api_error.kind,
                    retryable=api_error.retryable,
                    attempt=attempt,
                    error=str(getattr(error, "reason", error)),
                )
                if _should_retry(api_error, attempt, max_attempts, retry_deadline):
                    _sleep_before_retry(api_error, attempt, retry_deadline)
                    continue
                raise api_error from error

        raise TodoistApiError(
            kind="transient",
            message=TODOIST_ERROR_MESSAGES["transient"],
            retryable=True,
            url=url,
            method=method,
            attempts=max_attempts,
        )

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


def _todoist_http_error(
    error: urllib.error.HTTPError,
    url: str,
    method: str,
    attempts: int,
) -> TodoistApiError:
    kind = TODOIST_HTTP_ERROR_KIND_BY_STATUS.get(error.code)
    if kind is None and 500 <= error.code <= 599:
        kind = "transient"
    if kind is None:
        kind = "validation" if 400 <= error.code <= 499 else "transient"

    retry_after_header = error.headers.get("Retry-After") if error.headers else None
    retry_after = _parse_retry_after(retry_after_header)
    return TodoistApiError(
        kind=kind,
        message=TODOIST_ERROR_MESSAGES[kind],
        status_code=error.code,
        retryable=kind in {"rate-limit", "transient"},
        url=url,
        method=method,
        attempts=attempts,
        retry_after_seconds=retry_after,
    )


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None

    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _should_retry(
    error: TodoistApiError,
    attempt: int,
    max_attempts: int,
    retry_deadline: float,
) -> bool:
    if not error.retryable or attempt >= max_attempts:
        return False
    remaining = retry_deadline - time.monotonic()
    if remaining <= 0:
        return False
    if error.retry_after_seconds is not None and error.retry_after_seconds > remaining:
        return False
    return True


def _sleep_before_retry(error: TodoistApiError, attempt: int, retry_deadline: float) -> None:
    delay = _retry_delay_seconds(error, attempt)
    remaining = retry_deadline - time.monotonic()
    if remaining <= 0:
        return
    time.sleep(min(delay, remaining))


def _retry_delay_seconds(error: TodoistApiError, attempt: int) -> float:
    if error.retry_after_seconds is not None:
        return error.retry_after_seconds
    exponential_delay = settings.todoist_retry_base_delay_seconds * (2 ** max(0, attempt - 1))
    capped_delay = min(settings.todoist_retry_max_delay_seconds, exponential_delay)
    return capped_delay + random.uniform(0, min(0.25, capped_delay / 2))


__all__ = [
    "TODOIST_COMPLETED_BY_COMPLETION_DATE_URL",
    "TODOIST_REST_BASE_URL",
    "TodoistApiError",
    "TodoistApiClient",
]
