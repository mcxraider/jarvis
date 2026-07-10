"""Google Calendar API client (v3), mirroring TodoistApiClient's shape.

One class, per-run construction, lazy discovery service, a shared ``_execute``
wrapper that classifies ``HttpError`` into :class:`GoogleCalendarApiError` and
retries transient/rate-limit failures. Every method returns a *normalized* dict
(the raw Google event carries ~40 fields; we keep the handful the model needs)
to hold down LLM context tokens.

Tracing logs operation + status + attempt only — never request/response bodies
(event summaries and attendee emails are user data; tokens are never present in
these objects anyway).
"""

import logging
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from agents.agent_api.app.tools.google_calendar.auth import (
    GoogleCalendarApiError,
    build_calendar_service,
)
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_BASE_DELAY_SECONDS = 0.5
_MAX_DELAY_SECONDS = 4.0
_RETRYABLE_KINDS = {"transient", "rate-limit"}

# HTTP status -> classified kind. 403 is treated as auth (single-user setup);
# Google also uses 403 for some rate limits, but that is rare here and only
# costs a non-retry, not a wrong result.
_STATUS_KIND = {
    400: "validation",
    401: "auth",
    403: "auth",
    404: "not-found",
    409: "validation",
    410: "not-found",
    422: "validation",
    429: "rate-limit",
    500: "transient",
    502: "transient",
    503: "transient",
    504: "transient",
}

_ERROR_MESSAGES = {
    "auth": (
        "Google Calendar authentication failed. Reconnect with "
        "scripts/connect_google_calendar.py."
    ),
    "not-found": "Google Calendar could not find the requested resource.",
    "rate-limit": "Google Calendar rate limit reached. Please try again shortly.",
    "transient": "Google Calendar is temporarily unavailable. Please try again shortly.",
    "validation": "Google Calendar rejected the request as invalid.",
}


def _normalize_event(event: Any) -> Any:
    """Reduce a raw Google event to the fields the model needs (drops empties)."""

    if not isinstance(event, dict):
        return event
    start = event.get("start") or {}
    end = event.get("end") or {}
    normalized = {
        "event_id": event.get("id"),
        "summary": event.get("summary"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "location": event.get("location"),
        "attendees": [
            attendee.get("email")
            for attendee in event.get("attendees", [])
            if isinstance(attendee, dict) and attendee.get("email")
        ],
        "status": event.get("status"),
    }
    return {key: value for key, value in normalized.items() if value not in (None, [], "")}


def _build_event_body(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Translate flat tool arguments into a Google event resource body.

    Timed events use start_datetime/end_datetime (+ optional timezone); all-day
    events use start_date/end_date. Only supplied fields are included so
    ``patch`` updates stay partial.
    """

    body: Dict[str, Any] = {}
    for key in ("summary", "description", "location"):
        if arguments.get(key) is not None:
            body[key] = arguments[key]

    timezone = arguments.get("timezone")
    if arguments.get("start_datetime"):
        body["start"] = {"dateTime": arguments["start_datetime"]}
        if timezone:
            body["start"]["timeZone"] = timezone
    elif arguments.get("start_date"):
        body["start"] = {"date": arguments["start_date"]}

    if arguments.get("end_datetime"):
        body["end"] = {"dateTime": arguments["end_datetime"]}
        if timezone:
            body["end"]["timeZone"] = timezone
    elif arguments.get("end_date"):
        body["end"] = {"date": arguments["end_date"]}

    if arguments.get("attendees"):
        body["attendees"] = [{"email": email} for email in arguments["attendees"]]
    if arguments.get("recurrence"):
        body["recurrence"] = list(arguments["recurrence"])
    if arguments.get("reminders_minutes"):
        body["reminders"] = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": minutes}
                for minutes in arguments["reminders_minutes"]
            ],
        }
    return body


class GoogleCalendarClient:
    """Direct Google Calendar v3 client built on the discovery service."""

    def __init__(
        self,
        tracer: Optional[TracePrinter] = None,
        service: Any = None,
        token_path: Optional[str] = None,
        credential_json: Optional[str] = None,
        persist_callback: Optional[Callable[..., None]] = None,
    ):
        self.tracer = tracer or NULL_TRACE
        # Injected in tests; lazily built from local credentials in production.
        self._service = service
        self._token_path = token_path
        self._credential_json = credential_json
        self._persist_callback = persist_callback
        # ToolNode runs a batch of tool calls on a ThreadPoolExecutor, so several
        # threads share this one client. The underlying httplib2.Http/OpenSSL
        # socket is NOT thread-safe: concurrent request.execute() calls corrupt
        # the connection and crash the process (SIGTRAP). This lock serializes
        # both the lazy service build and every API call so only one thread ever
        # touches the shared socket. A single-user assistant makes rare calendar
        # calls, so serializing them costs nothing meaningful.
        self._lock = threading.Lock()

    @property
    def service(self) -> Any:
        # Double-checked locking: the common path (already built) stays lock-free,
        # while the first concurrent batch builds the service exactly once.
        if self._service is None:
            with self._lock:
                if self._service is None:
                    self._service = build_calendar_service(
                        self._token_path,
                        credential_json=self._credential_json,
                        persist_callback=self._persist_callback,
                    )
        return self._service

    # -- shared execution wrapper -------------------------------------------------

    def _execute(self, request: Any, operation: str) -> Any:
        from googleapiclient.errors import HttpError

        last_error: Optional[GoogleCalendarApiError] = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self.tracer.event(
                "calendar.request",
                "Sending Calendar API request.",
                operation=operation,
                attempt=attempt,
                max_attempts=_MAX_ATTEMPTS,
            )
            try:
                # Serialize the non-thread-safe httplib2/OpenSSL socket. Held
                # only across the network call itself — never across the retry
                # sleep below, so a backoff on one thread can't stall others.
                with self._lock:
                    result = request.execute()
                self.tracer.event(
                    "calendar.response",
                    "Received Calendar API response.",
                    operation=operation,
                    attempt=attempt,
                )
                return result
            except HttpError as error:
                api_error = self._classify_http_error(error, operation, attempt)
            except GoogleCalendarApiError:
                raise
            except Exception as error:  # network/transport failure
                api_error = GoogleCalendarApiError(
                    kind="transient",
                    message=_ERROR_MESSAGES["transient"],
                    retryable=True,
                    attempts=attempt,
                    operation=operation,
                )
                self.tracer.event(
                    "calendar.error",
                    "Calendar API connection failed.",
                    operation=operation,
                    attempt=attempt,
                    error=type(error).__name__,
                )
                if attempt < _MAX_ATTEMPTS:
                    last_error = api_error
                    progress = getattr(self.tracer, "progress", None)
                    if callable(progress): progress({
                        "phase": "retrying",
                        "action": "retrying",
                        "domains": ["calendar"],
                        "retry": {"target": "domain", "domain": "calendar", "reason": "temporary_connection"},
                    })
                    self._sleep_before_retry(attempt)
                    continue
                raise api_error from error

            self.tracer.event(
                "calendar.error",
                "Calendar API returned an error.",
                operation=operation,
                kind=api_error.kind,
                status=api_error.status_code,
                retryable=api_error.retryable,
                attempt=attempt,
            )
            if api_error.retryable and attempt < _MAX_ATTEMPTS:
                last_error = api_error
                retry_reason = "rate_limited" if api_error.kind == "rate-limit" else "service_unavailable"
                progress = getattr(self.tracer, "progress", None)
                if callable(progress): progress({
                    "phase": "retrying",
                    "action": "retrying",
                    "domains": ["calendar"],
                    "retry": {"target": "domain", "domain": "calendar", "reason": retry_reason},
                })
                self._sleep_before_retry(attempt)
                continue
            raise api_error

        raise last_error or GoogleCalendarApiError(
            kind="transient",
            message=_ERROR_MESSAGES["transient"],
            retryable=True,
            operation=operation,
        )

    def _classify_http_error(
        self,
        error: Any,
        operation: str,
        attempt: int,
    ) -> GoogleCalendarApiError:
        status = getattr(error, "status_code", None)
        if status is None:
            status = getattr(getattr(error, "resp", None), "status", None)
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None

        kind = _STATUS_KIND.get(status)
        if kind is None:
            if status and 500 <= status < 600:
                kind = "transient"
            elif status and 400 <= status < 500:
                kind = "validation"
            else:
                kind = "transient"

        return GoogleCalendarApiError(
            kind=kind,
            message=_ERROR_MESSAGES.get(kind, _ERROR_MESSAGES["transient"]),
            status_code=status,
            retryable=kind in _RETRYABLE_KINDS,
            attempts=attempt,
            operation=operation,
            reconnect=(kind == "auth"),
        )

    @staticmethod
    def _sleep_before_retry(attempt: int) -> None:
        delay = min(_MAX_DELAY_SECONDS, _BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
        time.sleep(delay + random.uniform(0, min(0.25, delay / 2)))

    # -- tools --------------------------------------------------------------------

    def list_calendars(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = self._execute(
            self.service.calendarList().list(),
            "calendar.calendars.list",
        )
        items = result.get("items", []) if isinstance(result, dict) else []
        return {
            "calendars": [
                {
                    "calendar_id": item.get("id"),
                    "summary": item.get("summary"),
                    "primary": item.get("primary", False),
                    "time_zone": item.get("timeZone"),
                }
                for item in items
            ]
        }

    def list_calendar_events(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        calendar_id = arguments.get("calendar_id") or "primary"
        # The LangChain wrapper passes omitted optionals as an explicit None, so
        # `.get("single_events", True)` would yield None (key present), not the
        # default. Treat missing OR None as the intended default (True) so
        # recurring events expand and orderBy=startTime applies by default.
        single_events = arguments.get("single_events")
        if single_events is None:
            single_events = True
        params: Dict[str, Any] = {
            "calendarId": calendar_id,
            "timeMin": arguments["time_min"],
            "timeMax": arguments["time_max"],
            "singleEvents": single_events,
        }
        # orderBy=startTime is only valid when recurring events are expanded.
        if single_events:
            params["orderBy"] = "startTime"
        if arguments.get("max_results") is not None:
            params["maxResults"] = arguments["max_results"]
        if arguments.get("q"):
            params["q"] = arguments["q"]

        result = self._execute(
            self.service.events().list(**params),
            "calendar.events.list",
        )
        items = result.get("items", []) if isinstance(result, dict) else []
        return {
            "events": [_normalize_event(event) for event in items],
            "next_page_token": result.get("nextPageToken") if isinstance(result, dict) else None,
        }

    def get_calendar_event(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        calendar_id = arguments.get("calendar_id") or "primary"
        result = self._execute(
            self.service.events().get(
                calendarId=calendar_id,
                eventId=arguments["event_id"],
            ),
            "calendar.events.get",
        )
        return _normalize_event(result)

    def create_calendar_event(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        calendar_id = arguments.get("calendar_id") or "primary"
        body = _build_event_body(arguments)
        result = self._execute(
            self.service.events().insert(calendarId=calendar_id, body=body),
            "calendar.events.insert",
        )
        return _normalize_event(result)

    def update_calendar_event(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        arguments = dict(arguments)
        calendar_id = arguments.pop("calendar_id", None) or "primary"
        event_id = arguments.pop("event_id")
        body = _build_event_body(arguments)
        result = self._execute(
            self.service.events().patch(
                calendarId=calendar_id,
                eventId=event_id,
                body=body,
            ),
            "calendar.events.patch",
        )
        return _normalize_event(result)

    def delete_calendar_event(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        calendar_id = arguments.get("calendar_id") or "primary"
        event_id = arguments["event_id"]
        self._execute(
            self.service.events().delete(calendarId=calendar_id, eventId=event_id),
            "calendar.events.delete",
        )
        return {
            "success": True,
            "message": f"Event {event_id} deleted",
            "event_id": event_id,
        }

    def get_freebusy(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        calendar_ids: List[str] = arguments.get("calendar_ids") or ["primary"]
        body = {
            "timeMin": arguments["time_min"],
            "timeMax": arguments["time_max"],
            "items": [{"id": calendar_id} for calendar_id in calendar_ids],
        }
        result = self._execute(
            self.service.freebusy().query(body=body),
            "calendar.freebusy.query",
        )
        calendars = result.get("calendars", {}) if isinstance(result, dict) else {}
        return {
            "calendars": {
                calendar_id: {"busy": info.get("busy", [])}
                for calendar_id, info in calendars.items()
            }
        }


__all__ = ["GoogleCalendarClient"]
