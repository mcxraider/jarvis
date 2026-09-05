"""Todoist REST API client over shared, keep-alive ``httpx`` transports.

Bearer tokens are attached per request rather than to the process-wide clients so
multiple users can safely reuse the same connection pools.
"""

import asyncio
import copy
import json
import random
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Optional

import httpx
from langsmith import traceable

from agents.agent_api.app.config import settings
from agents.agent_api.app.tools.errors import ClassifiedApiError
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter

TODOIST_REST_BASE_URL = settings.todoist_rest_base_url
TODOIST_COMPLETED_BY_COMPLETION_DATE_URL = (
    f"{TODOIST_REST_BASE_URL}/tasks/completed/by_completion_date"
)
DEFAULT_COLLECTION_LIMIT = 50
DEFAULT_COMMENT_LIMIT = 10
TODOIST_COLLECTION_LIMIT_MAX = 200
TODOIST_COMMENT_LIMIT_MAX = 10

_STRIP_RESPONSE_FIELDS = frozenset({
    "user_id",
    "added_by_uid",
    "assigned_by_uid",
    "responsible_uid",
    "is_collapsed",
    "is_deleted",
    "child_order",
    "order_key",
    "day_order",
    "note_count",
    "completed_count",
    "postponed_count",
    "updated_at",
    "added_at",
    "completed_at",
    "completed_by_uid",
})


def _strip_response_fields(obj):
    """Remove noise fields from Todoist responses to reduce LLM context tokens."""
    if isinstance(obj, dict):
        return {k: _strip_response_fields(v) for k, v in obj.items() if k not in _STRIP_RESPONSE_FIELDS}
    if isinstance(obj, list):
        return [_strip_response_fields(item) for item in obj]
    return obj


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


def _todoist_trace_inputs(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Keep provider traces useful without recording IDs, payloads, or secrets."""

    return {
        "method": inputs.get("method", "GET"),
        "endpoint": todoist_endpoint_template(str(inputs.get("url", ""))),
        "has_payload": inputs.get("payload") is not None,
    }


def _todoist_trace_outputs(output: Any) -> Dict[str, Any]:
    """Never copy provider response content into LangSmith traces."""

    return {
        "has_result": output is not None,
        "result_type": type(output).__name__,
    }


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

TODOIST_AMBIGUOUS_MUTATION_MESSAGE = (
    "Todoist could not confirm whether the change completed. "
    "Check Todoist before trying again."
)


_shared_http_client: Optional[httpx.Client] = None
_shared_http_client_lock = threading.Lock()
_shared_async_http_client: Optional[httpx.AsyncClient] = None
_shared_async_http_client_loop: Optional[asyncio.AbstractEventLoop] = None
_shared_async_http_client_closing = False
_shared_async_http_client_lock = threading.Lock()


def _http_setting(name: str, default: Any) -> Any:
    """Read pool settings with defaults during rolling configuration upgrades."""

    return getattr(settings, name, default)


def get_todoist_http_client() -> httpx.Client:
    """Return the lazily-created process-wide synchronous connection pool."""

    global _shared_http_client
    client = _shared_http_client
    if client is not None:
        return client
    with _shared_http_client_lock:
        if _shared_http_client is None:
            _shared_http_client = httpx.Client(
                timeout=httpx.Timeout(_http_setting("todoist_http_timeout_seconds", 30.0)),
                limits=httpx.Limits(
                    max_keepalive_connections=_http_setting(
                        "todoist_http_max_keepalive_connections", 10
                    ),
                    max_connections=_http_setting("todoist_http_max_connections", 20),
                ),
                follow_redirects=True,
            )
        return _shared_http_client


def close_todoist_http_client() -> None:
    """Close and clear the shared synchronous pool, if one exists."""

    global _shared_http_client
    with _shared_http_client_lock:
        client = _shared_http_client
        _shared_http_client = None
    if client is not None:
        client.close()


def get_todoist_async_http_client() -> httpx.AsyncClient:
    """Return the pool owned by the active loop, refusing cross-loop reuse."""

    global _shared_async_http_client, _shared_async_http_client_loop
    loop = asyncio.get_running_loop()
    with _shared_async_http_client_lock:
        if _shared_async_http_client is not None:
            if _shared_async_http_client_loop is not loop:
                raise RuntimeError(
                    "Todoist async HTTP client belongs to another event loop; "
                    "close it on its owner loop before creating a new one."
                )
            if _shared_async_http_client_closing:
                raise RuntimeError("Todoist async HTTP client is closing.")
            return _shared_async_http_client

        _shared_async_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(_http_setting("todoist_http_timeout_seconds", 30.0)),
            limits=httpx.Limits(
                max_keepalive_connections=_http_setting(
                    "todoist_http_max_keepalive_connections", 10
                ),
                max_connections=_http_setting("todoist_http_max_connections", 20),
            ),
            follow_redirects=True,
        )
        _shared_async_http_client_loop = loop
        return _shared_async_http_client


async def close_todoist_async_http_client() -> None:
    """Close the pool on its owner loop before allowing a new loop to claim it."""

    global _shared_async_http_client, _shared_async_http_client_loop
    global _shared_async_http_client_closing
    loop = asyncio.get_running_loop()
    with _shared_async_http_client_lock:
        client = _shared_async_http_client
        if client is None:
            return
        if _shared_async_http_client_loop is not loop:
            raise RuntimeError(
                "Todoist async HTTP client must be closed on its owner event loop."
            )
        if _shared_async_http_client_closing:
            raise RuntimeError("Todoist async HTTP client is already closing.")
        _shared_async_http_client_closing = True

    try:
        await client.aclose()
    except BaseException:
        with _shared_async_http_client_lock:
            if _shared_async_http_client is client:
                _shared_async_http_client_closing = False
        raise
    else:
        with _shared_async_http_client_lock:
            if _shared_async_http_client is client:
                _shared_async_http_client = None
                _shared_async_http_client_loop = None
                _shared_async_http_client_closing = False


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
    ambiguous_commit: bool = False

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
        if self.ambiguous_commit:
            payload["ambiguous_commit"] = True
        return payload


class TodoistApiClient:
    """Direct Todoist API client using shared sync and async HTTP pools."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        tracer: Optional[TracePrinter] = None,
        http_client: Optional[httpx.Client] = None,
        async_http_client: Optional[httpx.AsyncClient] = None,
        response_filter: Optional[Callable[[str, Any], Any]] = None,
    ):
        self.api_key = api_key
        self._tracer = tracer or NULL_TRACE
        self._http_client = http_client
        self._async_http_client = async_http_client
        self._response_filter = response_filter

    @property
    def tracer(self) -> TracePrinter:
        return self._tracer

    def with_tracer(self, tracer: TracePrinter) -> "TodoistApiClient":
        clone = copy.copy(self)
        clone._tracer = tracer
        return clone

    def with_response_filter(
        self,
        response_filter: Callable[[str, Any], Any],
    ) -> "TodoistApiClient":
        """Return a request-local clone that sanitizes responses before tracing."""

        clone = copy.copy(self)
        clone._response_filter = response_filter
        return clone

    def _filter_response(self, url: str, parsed: Any) -> Any:
        result = parsed if self._response_filter is None else self._response_filter(url, parsed)
        return _strip_response_fields(result)

    def _authorize_task_mutation(self, task_id: Any) -> None:
        """Fail closed on project restrictions before mutating a task by id."""

        if self._response_filter is not None:
            self._request(f"{TODOIST_REST_BASE_URL}/tasks/{task_id}")

    async def _async_authorize_task_mutation(self, task_id: Any) -> None:
        if self._response_filter is not None:
            await self.async_request(f"{TODOIST_REST_BASE_URL}/tasks/{task_id}")

    @traceable(
        name="api.todoist",
        run_type="tool",
        process_inputs=_todoist_trace_inputs,
        process_outputs=_todoist_trace_outputs,
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

        data: Optional[bytes] = None
        headers = {"Authorization": f"Bearer {self.api_key}"}

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            if content_type:
                headers["Content-Type"] = "application/json"

        http_client = self._http_client or get_todoist_http_client()
        max_attempts = max(1, settings.todoist_max_retry_attempts)
        retry_deadline = time.monotonic() + max(0.0, settings.todoist_retry_total_timeout_seconds)
        last_error: Optional[TodoistApiError] = None

        for attempt in range(1, max_attempts + 1):
            attempt_timeout = _attempt_timeout_seconds(retry_deadline)
            if attempt_timeout <= 0:
                if last_error is not None:
                    raise last_error
                raise _todoist_deadline_error(url, method, attempt - 1)
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

            try:
                response = http_client.request(
                    method,
                    url,
                    headers=headers,
                    content=data,
                    timeout=attempt_timeout,
                )
                if _remaining_retry_seconds(retry_deadline) <= 0:
                    raise _todoist_deadline_error(
                        url,
                        method,
                        attempt,
                        dispatched=True,
                    )
            except (httpx.RequestError, httpx.InvalidURL) as error:
                api_error = _todoist_request_error(error, url, method, attempt)
                last_error = api_error
                self.tracer.event(
                    "todoist.error",
                    "Todoist API connection failed.",
                    kind=api_error.kind,
                    retryable=api_error.retryable,
                    attempt=attempt,
                    error=str(error),
                )
                if _should_retry(api_error, attempt, max_attempts, retry_deadline):
                    _emit_retry_progress(self.tracer, "temporary_connection")
                    _sleep_before_retry(api_error, attempt, retry_deadline)
                    if _remaining_retry_seconds(retry_deadline) <= 0:
                        raise api_error from error
                    continue
                raise api_error from error

            status = response.status_code
            body = response.text
            if status >= 400:
                api_error = _todoist_http_error(
                    status,
                    response.headers,
                    url,
                    method,
                    attempt,
                    body,
                )
                last_error = api_error
                self.tracer.event(
                    "todoist.error",
                    "Todoist API returned an HTTP error.",
                    status=status,
                    kind=api_error.kind,
                    retryable=api_error.retryable,
                    attempt=attempt,
                )
                self.tracer.payload(
                    "todoist.payload",
                    "error",
                    {"status": status, "body": body},
                )
                if _should_retry(api_error, attempt, max_attempts, retry_deadline):
                    reason = (
                        "rate_limited"
                        if api_error.kind == "rate-limit"
                        else "service_unavailable"
                    )
                    _emit_retry_progress(self.tracer, reason)
                    _sleep_before_retry(api_error, attempt, retry_deadline)
                    if _remaining_retry_seconds(retry_deadline) <= 0:
                        raise api_error
                    continue
                raise api_error

            self.tracer.event(
                "todoist.response",
                "Received Todoist API response.",
                status=status,
                has_body=bool(body),
                attempt=attempt,
            )
            if status == 204 or not body:
                return None
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as error:
                raise _todoist_response_decode_error(
                    url,
                    method,
                    attempt,
                ) from error
            filtered = self._filter_response(url, parsed)
            self.tracer.payload("todoist.payload", "response", filtered)
            return filtered

        raise TodoistApiError(
            kind="transient",
            message=TODOIST_ERROR_MESSAGES["transient"],
            retryable=_is_retry_safe_method(method),
            url=url,
            method=method,
            attempts=max_attempts,
        )

    @traceable(
        name="api.todoist",
        run_type="tool",
        process_inputs=_todoist_trace_inputs,
        process_outputs=_todoist_trace_outputs,
    )
    async def async_request(
        self,
        url: str,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
        content_type: bool = True,
        *,
        async_http_client: Optional[httpx.AsyncClient] = None,
    ) -> Any:
        """Send a Todoist request without blocking the event loop."""

        if not self.api_key:
            raise TodoistApiError(
                kind="auth",
                message=TODOIST_ERROR_MESSAGES["auth"],
                retryable=False,
                url=url,
                method=method,
            )

        data: Optional[bytes] = None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            if content_type:
                headers["Content-Type"] = "application/json"

        http_client = (
            async_http_client
            or self._async_http_client
            or get_todoist_async_http_client()
        )
        max_attempts = max(1, settings.todoist_max_retry_attempts)
        retry_deadline = time.monotonic() + max(
            0.0, settings.todoist_retry_total_timeout_seconds
        )
        last_error: Optional[TodoistApiError] = None

        for attempt in range(1, max_attempts + 1):
            attempt_timeout = _attempt_timeout_seconds(retry_deadline)
            if attempt_timeout <= 0:
                if last_error is not None:
                    raise last_error
                raise _todoist_deadline_error(url, method, attempt - 1)
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

            try:
                async with asyncio.timeout(attempt_timeout):
                    response = await http_client.request(
                        method,
                        url,
                        headers=headers,
                        content=data,
                        timeout=attempt_timeout,
                    )
            except (httpx.RequestError, httpx.InvalidURL, TimeoutError) as error:
                api_error = _todoist_request_error(error, url, method, attempt)
                last_error = api_error
                self.tracer.event(
                    "todoist.error",
                    "Todoist API connection failed.",
                    kind=api_error.kind,
                    retryable=api_error.retryable,
                    attempt=attempt,
                    error=str(error),
                )
                if _should_retry(api_error, attempt, max_attempts, retry_deadline):
                    _emit_retry_progress(self.tracer, "temporary_connection")
                    await _async_sleep_before_retry(api_error, attempt, retry_deadline)
                    if _remaining_retry_seconds(retry_deadline) <= 0:
                        raise api_error from error
                    continue
                raise api_error from error

            status = response.status_code
            body = response.text
            if status >= 400:
                api_error = _todoist_http_error(
                    status,
                    response.headers,
                    url,
                    method,
                    attempt,
                    body,
                )
                last_error = api_error
                self.tracer.event(
                    "todoist.error",
                    "Todoist API returned an HTTP error.",
                    status=status,
                    kind=api_error.kind,
                    retryable=api_error.retryable,
                    attempt=attempt,
                )
                self.tracer.payload(
                    "todoist.payload",
                    "error",
                    {"status": status, "body": body},
                )
                if _should_retry(api_error, attempt, max_attempts, retry_deadline):
                    reason = (
                        "rate_limited"
                        if api_error.kind == "rate-limit"
                        else "service_unavailable"
                    )
                    _emit_retry_progress(self.tracer, reason)
                    await _async_sleep_before_retry(api_error, attempt, retry_deadline)
                    if _remaining_retry_seconds(retry_deadline) <= 0:
                        raise api_error
                    continue
                raise api_error

            self.tracer.event(
                "todoist.response",
                "Received Todoist API response.",
                status=status,
                has_body=bool(body),
                attempt=attempt,
            )
            if status == 204 or not body:
                return None
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as error:
                raise _todoist_response_decode_error(
                    url,
                    method,
                    attempt,
                ) from error
            filtered = self._filter_response(url, parsed)
            self.tracer.payload("todoist.payload", "response", filtered)
            return filtered

        raise TodoistApiError(
            kind="transient",
            message=TODOIST_ERROR_MESSAGES["transient"],
            retryable=_is_retry_safe_method(method),
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
        arguments = _with_collection_limit(
            arguments,
            default=DEFAULT_COLLECTION_LIMIT,
            maximum=TODOIST_COLLECTION_LIMIT_MAX,
        )
        params = _query_params(_without_none(arguments), comma_join_keys={"ids"})
        suffix = f"?{params}" if params else ""
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks{suffix}")

    def get_tasks_by_filter(self, arguments: Dict[str, Any]) -> Any:
        arguments = _with_collection_limit(
            arguments,
            default=DEFAULT_COLLECTION_LIMIT,
            maximum=TODOIST_COLLECTION_LIMIT_MAX,
        )
        params = _query_params(_without_none(arguments))
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks/filter?{params}")

    def update_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        arguments = dict(arguments)
        task_id = arguments.pop("task_id")
        self._authorize_task_mutation(task_id)
        payload = _sanitize_update_payload(arguments)
        _validate_duration_pair(payload)
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks/{task_id}", "POST", payload)

    def complete_task(self, arguments: Dict[str, Any]) -> Any:
        self._authorize_task_mutation(arguments["task_id"])
        self._request(f"{TODOIST_REST_BASE_URL}/tasks/{arguments['task_id']}/close", "POST")
        return {"success": True, "message": f"Task {arguments['task_id']} marked as completed"}

    def delete_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        self._authorize_task_mutation(arguments["task_id"])
        self._request(f"{TODOIST_REST_BASE_URL}/tasks/{arguments['task_id']}", "DELETE")
        return {"success": True, "message": f"Task {arguments['task_id']} deleted permanently"}

    def get_completed_todoist_tasks_by_completion_date(self, arguments: Dict[str, Any]) -> Any:
        arguments = _with_collection_limit(
            arguments,
            default=DEFAULT_COLLECTION_LIMIT,
            maximum=TODOIST_COLLECTION_LIMIT_MAX,
        )
        arguments = _with_default_completion_date_range(_without_none(arguments))
        params = _query_params(arguments)
        suffix = f"?{params}" if params else ""
        data = self._request(f"{TODOIST_COMPLETED_BY_COMPLETION_DATE_URL}{suffix}")
        if not isinstance(data, dict):
            return {"items": [], "next_cursor": None}
        return {"items": data.get("items", []), "next_cursor": data.get("next_cursor")}

    def uncomplete_task(self, arguments: Dict[str, Any]) -> Any:
        self._authorize_task_mutation(arguments["task_id"])
        self._request(f"{TODOIST_REST_BASE_URL}/tasks/{arguments['task_id']}/reopen", "POST")
        return {"success": True, "message": f"Task {arguments['task_id']} reopened"}

    def get_comments(self, arguments: Dict[str, Any]) -> Any:
        arguments = dict(arguments)
        comment_id = arguments.pop("comment_id", None)
        if comment_id is not None:
            return self._request(f"{TODOIST_REST_BASE_URL}/comments/{comment_id}")
        arguments = _with_collection_limit(
            arguments,
            default=DEFAULT_COMMENT_LIMIT,
            maximum=TODOIST_COMMENT_LIMIT_MAX,
        )
        arguments = _without_none(arguments)
        if not arguments.get("task_id") and not arguments.get("project_id"):
            raise ValueError("get_comments requires task_id, project_id, or comment_id")
        params = _query_params(arguments)
        suffix = f"?{params}" if params else ""
        return self._request(f"{TODOIST_REST_BASE_URL}/comments{suffix}")

    def add_comment(self, arguments: Dict[str, Any]) -> Any:
        payload = _without_none(arguments)
        _validate_comment_target(payload)
        if payload.get("task_id") is not None:
            self._authorize_task_mutation(payload["task_id"])
        return self._request(f"{TODOIST_REST_BASE_URL}/comments", "POST", payload)

    def get_labels(self, arguments: Dict[str, Any]) -> Any:
        arguments = dict(arguments)
        search = arguments.pop("search", None)
        arguments = _with_collection_limit(
            arguments,
            default=DEFAULT_COLLECTION_LIMIT,
            maximum=TODOIST_COLLECTION_LIMIT_MAX,
        )
        params = _query_params(_without_none(arguments))
        suffix = f"?{params}" if params else ""
        data = self._request(f"{TODOIST_REST_BASE_URL}/labels{suffix}")
        if search is None:
            return data
        return _filter_by_name(data, search)

    def get_projects(self, arguments: Dict[str, Any]) -> Any:
        arguments = dict(arguments)
        search = arguments.pop("search", None)
        arguments = _with_collection_limit(
            arguments,
            default=DEFAULT_COLLECTION_LIMIT,
            maximum=TODOIST_COLLECTION_LIMIT_MAX,
        )
        params = _query_params(_without_none(arguments))
        suffix = f"?{params}" if params else ""
        data = self._request(f"{TODOIST_REST_BASE_URL}/projects{suffix}")
        if search is None:
            return data
        return _filter_by_name(data, search)

    def create_project(self, arguments: Dict[str, Any]) -> Any:
        payload = _without_none(arguments)
        return self._request(f"{TODOIST_REST_BASE_URL}/projects", "POST", payload)

    def create_section(self, arguments: Dict[str, Any]) -> Any:
        payload = _without_none(arguments)
        return self._request(f"{TODOIST_REST_BASE_URL}/sections", "POST", payload)

    # Native async tool handlers -------------------------------------------------
    # These intentionally mirror the synchronous methods above, including their
    # validation and response shaping. They call ``async_request`` directly so a
    # native-async dispatcher never falls back to a worker thread or the sync
    # httpx transport.

    async def async_add_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        payload = _without_none(arguments)
        _validate_duration_pair(payload)
        return await self.async_request(
            f"{TODOIST_REST_BASE_URL}/tasks",
            "POST",
            payload,
        )

    async def async_get_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return await self.async_request(
            f"{TODOIST_REST_BASE_URL}/tasks/{arguments['task_id']}"
        )

    async def async_get_tasks(self, arguments: Dict[str, Any]) -> Any:
        arguments = _with_collection_limit(
            arguments,
            default=DEFAULT_COLLECTION_LIMIT,
            maximum=TODOIST_COLLECTION_LIMIT_MAX,
        )
        params = _query_params(_without_none(arguments), comma_join_keys={"ids"})
        suffix = f"?{params}" if params else ""
        return await self.async_request(f"{TODOIST_REST_BASE_URL}/tasks{suffix}")

    async def async_get_tasks_by_filter(self, arguments: Dict[str, Any]) -> Any:
        arguments = _with_collection_limit(
            arguments,
            default=DEFAULT_COLLECTION_LIMIT,
            maximum=TODOIST_COLLECTION_LIMIT_MAX,
        )
        params = _query_params(_without_none(arguments))
        return await self.async_request(
            f"{TODOIST_REST_BASE_URL}/tasks/filter?{params}"
        )

    async def async_update_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        arguments = dict(arguments)
        task_id = arguments.pop("task_id")
        await self._async_authorize_task_mutation(task_id)
        payload = _sanitize_update_payload(arguments)
        _validate_duration_pair(payload)
        return await self.async_request(
            f"{TODOIST_REST_BASE_URL}/tasks/{task_id}",
            "POST",
            payload,
        )

    async def async_complete_task(self, arguments: Dict[str, Any]) -> Any:
        task_id = arguments["task_id"]
        await self._async_authorize_task_mutation(task_id)
        await self.async_request(
            f"{TODOIST_REST_BASE_URL}/tasks/{task_id}/close",
            "POST",
        )
        return {"success": True, "message": f"Task {task_id} marked as completed"}

    async def async_delete_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        task_id = arguments["task_id"]
        await self._async_authorize_task_mutation(task_id)
        await self.async_request(
            f"{TODOIST_REST_BASE_URL}/tasks/{task_id}",
            "DELETE",
        )
        return {"success": True, "message": f"Task {task_id} deleted permanently"}

    async def async_get_completed_todoist_tasks_by_completion_date(
        self,
        arguments: Dict[str, Any],
    ) -> Any:
        arguments = _with_collection_limit(
            arguments,
            default=DEFAULT_COLLECTION_LIMIT,
            maximum=TODOIST_COLLECTION_LIMIT_MAX,
        )
        arguments = _with_default_completion_date_range(_without_none(arguments))
        params = _query_params(arguments)
        suffix = f"?{params}" if params else ""
        data = await self.async_request(
            f"{TODOIST_COMPLETED_BY_COMPLETION_DATE_URL}{suffix}"
        )
        if not isinstance(data, dict):
            return {"items": [], "next_cursor": None}
        return {"items": data.get("items", []), "next_cursor": data.get("next_cursor")}

    async def async_uncomplete_task(self, arguments: Dict[str, Any]) -> Any:
        task_id = arguments["task_id"]
        await self._async_authorize_task_mutation(task_id)
        await self.async_request(
            f"{TODOIST_REST_BASE_URL}/tasks/{task_id}/reopen",
            "POST",
        )
        return {"success": True, "message": f"Task {task_id} reopened"}

    async def async_get_comments(self, arguments: Dict[str, Any]) -> Any:
        arguments = dict(arguments)
        comment_id = arguments.pop("comment_id", None)
        if comment_id is not None:
            return await self.async_request(
                f"{TODOIST_REST_BASE_URL}/comments/{comment_id}"
            )
        arguments = _with_collection_limit(
            arguments,
            default=DEFAULT_COMMENT_LIMIT,
            maximum=TODOIST_COMMENT_LIMIT_MAX,
        )
        arguments = _without_none(arguments)
        if not arguments.get("task_id") and not arguments.get("project_id"):
            raise ValueError("get_comments requires task_id, project_id, or comment_id")
        params = _query_params(arguments)
        suffix = f"?{params}" if params else ""
        return await self.async_request(f"{TODOIST_REST_BASE_URL}/comments{suffix}")

    async def async_add_comment(self, arguments: Dict[str, Any]) -> Any:
        payload = _without_none(arguments)
        _validate_comment_target(payload)
        if payload.get("task_id") is not None:
            await self._async_authorize_task_mutation(payload["task_id"])
        return await self.async_request(
            f"{TODOIST_REST_BASE_URL}/comments",
            "POST",
            payload,
        )

    async def async_get_labels(self, arguments: Dict[str, Any]) -> Any:
        arguments = dict(arguments)
        search = arguments.pop("search", None)
        arguments = _with_collection_limit(
            arguments,
            default=DEFAULT_COLLECTION_LIMIT,
            maximum=TODOIST_COLLECTION_LIMIT_MAX,
        )
        params = _query_params(_without_none(arguments))
        suffix = f"?{params}" if params else ""
        data = await self.async_request(f"{TODOIST_REST_BASE_URL}/labels{suffix}")
        if search is None:
            return data
        return _filter_by_name(data, search)

    async def async_get_projects(self, arguments: Dict[str, Any]) -> Any:
        arguments = dict(arguments)
        search = arguments.pop("search", None)
        arguments = _with_collection_limit(
            arguments,
            default=DEFAULT_COLLECTION_LIMIT,
            maximum=TODOIST_COLLECTION_LIMIT_MAX,
        )
        params = _query_params(_without_none(arguments))
        suffix = f"?{params}" if params else ""
        data = await self.async_request(f"{TODOIST_REST_BASE_URL}/projects{suffix}")
        if search is None:
            return data
        return _filter_by_name(data, search)

    async def async_create_project(self, arguments: Dict[str, Any]) -> Any:
        payload = _without_none(arguments)
        return await self.async_request(
            f"{TODOIST_REST_BASE_URL}/projects",
            "POST",
            payload,
        )

    async def async_create_section(self, arguments: Dict[str, Any]) -> Any:
        payload = _without_none(arguments)
        return await self.async_request(
            f"{TODOIST_REST_BASE_URL}/sections",
            "POST",
            payload,
        )


def _without_none(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop None and empty-string values before sending arguments to Todoist."""

    return {key: value for key, value in data.items() if value is not None and value != ""}


def _with_collection_limit(
    arguments: Dict[str, Any],
    *,
    default: int,
    maximum: int,
) -> Dict[str, Any]:
    """Return copied arguments with a validated per-page limit."""

    resolved = dict(arguments)
    limit = resolved.get("limit")
    if limit is None:
        limit = default
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    resolved["limit"] = limit
    return resolved


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
        if (value is not None and value != "") or key in _NULL_CLEARABLE_UPDATE_FIELDS
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
    status_code: int,
    headers: Any,
    url: str,
    method: str,
    attempts: int,
    body: str = "",
) -> TodoistApiError:
    kind = TODOIST_HTTP_ERROR_KIND_BY_STATUS.get(status_code)
    if kind is None and 500 <= status_code <= 599:
        kind = "transient"
    if kind is None:
        kind = "validation" if 400 <= status_code <= 499 else "transient"

    retry_after_header = headers.get("Retry-After") if headers else None
    retry_after = _parse_retry_after(retry_after_header)
    provider_message, provider_code, provider_tag = _parse_provider_error(body)
    message = TODOIST_ERROR_MESSAGES[kind]
    if provider_message:
        message = f"{message.rstrip('.')}: {provider_message}"
    ambiguous_commit = (
        not _is_retry_safe_method(method)
        and (status_code == 408 or 500 <= status_code <= 599)
    )
    return TodoistApiError(
        kind=kind,
        message=TODOIST_AMBIGUOUS_MUTATION_MESSAGE if ambiguous_commit else message,
        status_code=status_code,
        retryable=(
            kind in {"rate-limit", "transient"}
            and _is_retry_safe_method(method)
        ),
        url=url,
        method=method,
        attempts=attempts,
        retry_after_seconds=retry_after,
        provider_message=provider_message,
        provider_code=provider_code,
        provider_tag=provider_tag,
        ambiguous_commit=ambiguous_commit,
    )


def _todoist_request_error(
    error: Exception,
    url: str,
    method: str,
    attempts: int,
) -> TodoistApiError:
    invalid_request = isinstance(
        error,
        (httpx.InvalidURL, httpx.UnsupportedProtocol, httpx.LocalProtocolError),
    )
    kind = "validation" if invalid_request else "transient"
    ambiguous_commit = not invalid_request and not _is_retry_safe_method(method)
    return TodoistApiError(
        kind=kind,
        message=(
            TODOIST_AMBIGUOUS_MUTATION_MESSAGE
            if ambiguous_commit
            else TODOIST_ERROR_MESSAGES[kind]
        ),
        retryable=not invalid_request and _is_retry_safe_method(method),
        url=url,
        method=method,
        attempts=attempts,
        ambiguous_commit=ambiguous_commit,
    )


def _todoist_deadline_error(
    url: str,
    method: str,
    attempts: int,
    *,
    dispatched: bool = False,
) -> TodoistApiError:
    ambiguous_commit = dispatched and not _is_retry_safe_method(method)
    return TodoistApiError(
        kind="transient",
        message=(
            TODOIST_AMBIGUOUS_MUTATION_MESSAGE
            if ambiguous_commit
            else TODOIST_ERROR_MESSAGES["transient"]
        ),
        retryable=_is_retry_safe_method(method),
        url=url,
        method=method,
        attempts=max(1, attempts),
        ambiguous_commit=ambiguous_commit,
    )


def _todoist_response_decode_error(
    url: str,
    method: str,
    attempts: int,
) -> TodoistApiError:
    ambiguous_commit = not _is_retry_safe_method(method)
    return TodoistApiError(
        kind="transient",
        message=(
            TODOIST_AMBIGUOUS_MUTATION_MESSAGE
            if ambiguous_commit
            else TODOIST_ERROR_MESSAGES["transient"]
        ),
        retryable=_is_retry_safe_method(method),
        url=url,
        method=method,
        attempts=attempts,
        ambiguous_commit=ambiguous_commit,
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
    error_extra = payload.get("error_extra")
    if isinstance(provider_message, str) and isinstance(error_extra, dict):
        argument = error_extra.get("argument")
        if isinstance(argument, str):
            provider_message = f"{provider_message} (argument: {argument})"
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
    remaining = _remaining_retry_seconds(retry_deadline)
    if remaining <= 0:
        return False
    if error.retry_after_seconds is not None and error.retry_after_seconds > remaining:
        return False
    return True


def _sleep_before_retry(error: TodoistApiError, attempt: int, retry_deadline: float) -> None:
    delay = _retry_delay_seconds(error, attempt)
    remaining = _remaining_retry_seconds(retry_deadline)
    if remaining <= 0:
        return
    time.sleep(min(delay, remaining))


async def _async_sleep_before_retry(
    error: TodoistApiError,
    attempt: int,
    retry_deadline: float,
) -> None:
    delay = _retry_delay_seconds(error, attempt)
    remaining = _remaining_retry_seconds(retry_deadline)
    if remaining <= 0:
        return
    await asyncio.sleep(min(delay, remaining))


def _remaining_retry_seconds(retry_deadline: float) -> float:
    return max(0.0, retry_deadline - time.monotonic())


def _attempt_timeout_seconds(retry_deadline: float) -> float:
    return min(
        _http_setting("todoist_http_timeout_seconds", 30.0),
        _remaining_retry_seconds(retry_deadline),
    )


def _is_retry_safe_method(method: str) -> bool:
    return method.upper() in {"GET", "HEAD", "OPTIONS"}


def _emit_retry_progress(tracer: TracePrinter, reason: str) -> None:
    progress = getattr(tracer, "progress", None)
    if callable(progress):
        progress(
            {
                "phase": "retrying",
                "action": "retrying",
                "domains": ["todoist"],
                "retry": {
                    "target": "domain",
                    "domain": "todoist",
                    "reason": reason,
                },
            }
        )


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
    "close_todoist_async_http_client",
    "close_todoist_http_client",
    "get_todoist_async_http_client",
    "get_todoist_http_client",
]
