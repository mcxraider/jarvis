"""Todoist REST API client using only the Python standard library."""

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional

from langsmith import traceable

from agents.agent_api.app.config import settings
from agents.agent_api.app.tools.errors import ClassifiedApiError
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter

TODOIST_REST_BASE_URL = settings.todoist_rest_base_url
TODOIST_COMPLETED_BY_COMPLETION_DATE_URL = (
    f"{TODOIST_REST_BASE_URL}/tasks/completed/by_completion_date"
)

# Path segments that look like Todoist resource identifiers (task/section/project
# IDs are numeric or alphanumeric strings). Collapse them to {id} so traces never
# carry raw identifiers, even if input-hiding is later disabled.
_ID_SEGMENT_RE = re.compile(r"^[0-9]+$|^[0-9a-zA-Z]{16,}$")


def todoist_endpoint_template(url: str) -> str:
    """Reduce a Todoist URL to a privacy-safe endpoint template.

    Strips the base URL, drops any query string, and replaces identifier-like
    path segments with ``{id}`` — e.g. ``.../tasks/123/close`` -> ``/tasks/{id}/close``.
    """

    if not url:
        return ""
    path = url.split("?", 1)[0]
    path = path.replace(TODOIST_REST_BASE_URL, "", 1)
    if not path.startswith("/"):
        path = "/" + path
    segments = [
        "{id}" if _ID_SEGMENT_RE.match(segment) else segment
        for segment in path.split("/")
    ]
    return "/".join(segments)

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
class TodoistApiError(ClassifiedApiError):
    """Structured Todoist failure safe to route through tool results.

    Subclasses the domain-neutral :class:`ClassifiedApiError` so the dispatcher
    catches it via the shared base without importing this module.
    """

    kind: str = "transient"
    message: str = ""
    status_code: Optional[int] = None
    retryable: bool = False
    operation: str = "todoist.request"
    url: str = ""
    method: str = "GET"
    attempts: int = 1
    retry_after_seconds: Optional[float] = None
    provider_message: Optional[str] = None
    provider_code: Optional[int] = None
    provider_tag: Optional[str] = None

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
        if self.provider_message is not None:
            payload["provider_message"] = self.provider_message
        if self.provider_code is not None:
            payload["provider_code"] = self.provider_code
        if self.provider_tag is not None:
            payload["provider_tag"] = self.provider_tag
        return payload


class TodoistApiClient:
    """Direct Todoist API client using only the Python stdlib."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        tracer: Optional[TracePrinter] = None,
    ):
        self.api_key = api_key
        self._tracer = tracer or NULL_TRACE

    @property
    def tracer(self) -> TracePrinter:
        return self._tracer

    def with_tracer(self, tracer: TracePrinter) -> "TodoistApiClient":
        clone = copy.copy(self)
        clone._tracer = tracer
        return clone

    @traceable(
        name="todoist_api_request",
        run_type="tool",
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
                endpoint=todoist_endpoint_template(url),
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
                api_error = _todoist_http_error(error, url, method, attempt, body)
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
                    progress = getattr(self.tracer, "progress", None)
                    if callable(progress): progress({
                        "phase": "retrying",
                        "action": "retrying",
                        "domains": ["todoist"],
                        "retry": {"target": "domain", "domain": "todoist", "reason": "rate_limited" if api_error.kind == "rate-limit" else "service_unavailable"},
                    })
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
                    progress = getattr(self.tracer, "progress", None)
                    if callable(progress): progress({
                        "phase": "retrying",
                        "action": "retrying",
                        "domains": ["todoist"],
                        "retry": {"target": "domain", "domain": "todoist", "reason": "temporary_connection"},
                    })
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
        payload = _without_none(arguments)
        _validate_duration_pair(payload)
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks", "POST", payload)

    def get_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks/{arguments['task_id']}")

    def get_tasks(self, arguments: Dict[str, Any]) -> Any:
        params = _query_params(_without_none(arguments), comma_join_keys={"ids"})
        suffix = f"?{params}" if params else ""
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks{suffix}")

    def get_tasks_by_filter(self, arguments: Dict[str, Any]) -> Any:
        params = _query_params(_without_none(arguments))
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks/filter?{params}")

    def update_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        arguments = dict(arguments)
        task_id = arguments.pop("task_id")
        payload = _sanitize_update_payload(arguments)
        _validate_duration_pair(payload)
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks/{task_id}", "POST", payload)

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

    def uncomplete_task(self, arguments: Dict[str, Any]) -> Any:
        self._request(f"{TODOIST_REST_BASE_URL}/tasks/{arguments['task_id']}/reopen", "POST")
        return {"success": True, "message": f"Task {arguments['task_id']} reopened"}

    def get_comments(self, arguments: Dict[str, Any]) -> Any:
        arguments = _without_none(arguments)
        comment_id = arguments.pop("comment_id", None)
        if comment_id is not None:
            return self._request(f"{TODOIST_REST_BASE_URL}/comments/{comment_id}")
        if not arguments.get("task_id") and not arguments.get("project_id"):
            raise ValueError("get_comments requires task_id, project_id, or comment_id")
        params = _query_params(arguments)
        suffix = f"?{params}" if params else ""
        return self._request(f"{TODOIST_REST_BASE_URL}/comments{suffix}")

    def add_comment(self, arguments: Dict[str, Any]) -> Any:
        payload = _without_none(arguments)
        _validate_comment_target(payload)
        return self._request(f"{TODOIST_REST_BASE_URL}/comments", "POST", payload)

    def get_labels(self, arguments: Dict[str, Any]) -> Any:
        arguments = dict(arguments)
        search = arguments.pop("search", None)
        params = _query_params(_without_none(arguments))
        suffix = f"?{params}" if params else ""
        data = self._request(f"{TODOIST_REST_BASE_URL}/labels{suffix}")
        if search is None:
            return data
        return _filter_by_name(data, search)

    def get_projects(self, arguments: Dict[str, Any]) -> Any:
        arguments = dict(arguments)
        search = arguments.pop("search", None)
        params = _query_params(_without_none(arguments))
        suffix = f"?{params}" if params else ""
        data = self._request(f"{TODOIST_REST_BASE_URL}/projects{suffix}")
        if search is None:
            return data
        return _filter_by_name(data, search)

    def create_project(self, arguments: Dict[str, Any]) -> Any:
        payload = _without_none(arguments)
        return self._request(f"{TODOIST_REST_BASE_URL}/projects", "POST", payload)


def _without_none(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop None values before sending arguments to Todoist."""

    return {key: value for key, value in data.items() if value is not None}


_NULL_CLEARABLE_UPDATE_FIELDS = {
    "assignee_id",
    "duration",
    "duration_unit",
    "deadline_date",
}


def _sanitize_update_payload(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Drop synthetic null defaults while retaining supported explicit clears."""

    if "labels" in arguments and arguments["labels"] is None:
        raise ValueError("labels must be an array of labels")
    return {
        key: value
        for key, value in arguments.items()
        if value is not None or key in _NULL_CLEARABLE_UPDATE_FIELDS
    }


def _validate_duration_pair(data: Dict[str, Any]) -> None:
    if ("duration" in data) != ("duration_unit" in data):
        raise ValueError("duration and duration_unit must be provided together")
    duration = data.get("duration")
    duration_unit = data.get("duration_unit")
    if duration is not None and duration_unit is None:
        raise ValueError("duration_unit is required when duration is set")
    if duration_unit is not None and duration is None:
        raise ValueError("duration is required when duration_unit is set")
    if duration is not None and (not isinstance(duration, int) or duration <= 0):
        raise ValueError("duration must be a positive integer")
    if duration_unit is not None and duration_unit not in {"minute", "day"}:
        raise ValueError("duration_unit must be minute or day")


def _validate_comment_target(data: Dict[str, Any]) -> None:
    """A comment must attach to exactly one of a task or a project."""

    has_task = bool(data.get("task_id"))
    has_project = bool(data.get("project_id"))
    if has_task == has_project:
        raise ValueError("add_comment requires exactly one of task_id or project_id")


def _filter_by_name(data: Any, search: str) -> Any:
    """Filter a labels/projects response to names containing ``search`` (case-insensitive).

    Works for any resource whose items carry a ``name`` field (labels, projects).
    Handles both the paginated ``{"results": [...], "next_cursor": ...}`` shape and a
    bare list, so the tool keeps working if the API response shape varies.
    """

    needle = search.casefold()

    def _matches(label: Any) -> bool:
        name = label.get("name", "") if isinstance(label, dict) else str(label)
        return needle in name.casefold()

    if isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, list):
            return {**data, "results": [label for label in results if _matches(label)]}
        return data
    if isinstance(data, list):
        return [label for label in data if _matches(label)]
    return data


def _with_default_completion_date_range(data: Dict[str, Any]) -> Dict[str, Any]:
    """Default completed-task queries to the last 30 days in UTC."""

    if "until" in data:
        until = _parse_rfc3339(data["until"], "until")
    else:
        until = datetime.now(timezone.utc)

    if "since" in data:
        since = _parse_rfc3339(data["since"], "since")
    else:
        since = until - timedelta(days=30)

    if since >= until:
        raise ValueError("Todoist completed-task until must be later than since")
    if until > _add_months(since, 3):
        raise ValueError("Todoist completed-task range cannot exceed three months")

    return {
        **data,
        "since": since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "until": until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _parse_rfc3339(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$",
        value,
    ):
        raise ValueError(f"Todoist completed-task {field} must be an RFC3339 timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Todoist completed-task {field} must include a timezone")
    return parsed


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    days_in_month = [
        31,
        29 if _is_leap_year(year) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ]
    return value.replace(year=year, month=month, day=min(value.day, days_in_month[month - 1]))


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


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
    body: str = "",
) -> TodoistApiError:
    kind = TODOIST_HTTP_ERROR_KIND_BY_STATUS.get(error.code)
    if kind is None and 500 <= error.code <= 599:
        kind = "transient"
    if kind is None:
        kind = "validation" if 400 <= error.code <= 499 else "transient"

    retry_after_header = error.headers.get("Retry-After") if error.headers else None
    retry_after = _parse_retry_after(retry_after_header)
    provider_message, provider_code, provider_tag = _parse_provider_error(body)
    message = TODOIST_ERROR_MESSAGES[kind]
    if provider_message:
        message = f"{message.rstrip('.')}: {provider_message}"
    return TodoistApiError(
        kind=kind,
        message=message,
        status_code=error.code,
        retryable=kind in {"rate-limit", "transient"},
        url=url,
        method=method,
        attempts=attempts,
        retry_after_seconds=retry_after,
        provider_message=provider_message,
        provider_code=provider_code,
        provider_tag=provider_tag,
    )


def _parse_provider_error(body: str) -> tuple[Optional[str], Optional[int], Optional[str]]:
    """Extract only allowlisted, model-safe diagnostics from a Todoist error."""

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None, None, None
    if not isinstance(payload, dict):
        return None, None, None

    provider_message = payload.get("error")
    provider_code = payload.get("error_code")
    provider_tag = payload.get("error_tag")
    return (
        provider_message if isinstance(provider_message, str) else None,
        provider_code if isinstance(provider_code, int) else None,
        provider_tag if isinstance(provider_tag, str) else None,
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
    "todoist_endpoint_template",
]
