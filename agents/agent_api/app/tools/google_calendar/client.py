"""Google Calendar API client (v3), mirroring TodoistApiClient's shape.

One class, per-run construction, lazy discovery service, a shared ``_execute``
wrapper that classifies ``HttpError`` into :class:`GoogleCalendarApiError` and
retries transient/rate-limit failures for reads. Mutations are single-attempt
because a timeout or provider error can be ambiguous. Every method returns a
*normalized* dict (the raw Google event carries ~40 fields; we keep the handful
the model needs) to hold down LLM context tokens.

Tracing logs operation + status + attempt only — never request/response bodies
(event summaries and attendee emails are user data; tokens are never present in
these objects anyway).
"""

import copy
from contextlib import suppress
import logging
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from agents.agent_api.app.async_offload import bounded_to_thread
from agents.agent_api.app.tools.google_calendar.auth import (
    GoogleCalendarApiError,
    load_credentials,
)
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_HTTP_TIMEOUT_SECONDS = 30.0
_BASE_DELAY_SECONDS = 0.5
_MAX_DELAY_SECONDS = 4.0
_RETRYABLE_KINDS = {"transient", "rate-limit"}
_MUTATION_OPERATIONS = {
    "calendar.events.insert",
    "calendar.events.patch",
    "calendar.events.delete",
}

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
_AMBIGUOUS_MUTATION_MESSAGE = (
    "Google Calendar could not confirm whether the change completed. "
    "Check the calendar before trying again."
)


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


class _CredentialCoordinator:
    """Serialize refresh state while allowing provider requests to overlap."""

    def __init__(self, credentials: Any) -> None:
        self.credentials = credentials
        self.lock = threading.Lock()
        self.generation = 0

    def proxy(self) -> "_CoordinatedCredentials":
        return _CoordinatedCredentials(self, self.generation)


class _CoordinatedCredentials:
    """AuthorizedHttp credential facade with generation-aware refresh locking."""

    def __init__(self, coordinator: _CredentialCoordinator, generation: int) -> None:
        self._coordinator = coordinator
        self._generation = generation

    def before_request(self, request, method, url, headers) -> None:
        coordinator = self._coordinator
        with coordinator.lock:
            before = (
                getattr(coordinator.credentials, "token", None),
                getattr(coordinator.credentials, "expiry", None),
            )
            coordinator.credentials.before_request(request, method, url, headers)
            after = (
                getattr(coordinator.credentials, "token", None),
                getattr(coordinator.credentials, "expiry", None),
            )
            if after != before:
                coordinator.generation += 1
            self._generation = coordinator.generation

    def refresh(self, request) -> None:
        coordinator = self._coordinator
        with coordinator.lock:
            if self._generation != coordinator.generation:
                self._generation = coordinator.generation
                return
            coordinator.credentials.refresh(request)
            coordinator.generation += 1
            self._generation = coordinator.generation

    def __getattr__(self, name: str) -> Any:
        return getattr(self._coordinator.credentials, name)


class GoogleCalendarClient:
    """Direct Google Calendar v3 client built on the discovery service."""

    def __init__(
        self,
        tracer: Optional[TracePrinter] = None,
        service: Any = None,
        token_path: Optional[str] = None,
        credential_json: Optional[str] = None,
        persist_callback: Optional[Callable[..., None]] = None,
        credentials: Any = None,
    ):
        self._tracer = tracer or NULL_TRACE
        # Injected in tests; lazily built from local credentials in production.
        self._service = service
        self._credentials = credentials
        self._credential_coordinator = (
            _CredentialCoordinator(credentials) if credentials is not None else None
        )
        self._token_path = token_path
        self._credential_json = credential_json
        self._persist_callback = persist_callback
        # Only lazy discovery construction is serialized. Each request gets its
        # own AuthorizedHttp/httplib2 transport in _execute(), so concurrent
        # Calendar calls never share the discovery service's socket state.
        self._lock = threading.Lock()

    @property
    def tracer(self) -> TracePrinter:
        return self._tracer

    def with_tracer(self, tracer: TracePrinter) -> "GoogleCalendarClient":
        clone = copy.copy(self)
        clone._tracer = tracer
        return clone

    @property
    def service(self) -> Any:
        # Double-checked locking: the common path (already built) stays lock-free,
        # while the first concurrent batch builds the service exactly once.
        if self._service is None:
            with self._lock:
                if self._service is None:
                    from googleapiclient.discovery import build

                    credentials = load_credentials(
                        self._token_path,
                        credential_json=self._credential_json,
                        persist_callback=self._persist_callback,
                    )
                    self._credentials = credentials
                    self._credential_coordinator = _CredentialCoordinator(credentials)
                    self._service = build(
                        "calendar",
                        "v3",
                        credentials=credentials,
                        cache_discovery=False,
                    )
        return self._service

    # -- shared execution wrapper -------------------------------------------------

    def _execute(self, request: Any, operation: str) -> Any:
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.errors import HttpError

        mutation = operation in _MUTATION_OPERATIONS
        max_attempts = 1 if mutation else _MAX_ATTEMPTS
        last_error: Optional[GoogleCalendarApiError] = None
        for attempt in range(1, max_attempts + 1):
            self.tracer.event(
                "calendar.request",
                "Sending Calendar API request.",
                operation=operation,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            try:
                # Discovery requests otherwise share one non-thread-safe
                # httplib2/OpenSSL socket. A fresh authorized transport per
                # attempt permits safe overlap without serializing network I/O.
                # Tests may inject a service without credentials; retain their
                # established bare execute() seam.
                if self._credentials is not None:
                    credentials = self._credential_coordinator.proxy()
                    # Bound every provider attempt. Reads can make up to three
                    # attempts and still remain comfortably below the outer
                    # 120-second graph deadline, including retry backoff.
                    raw_http = httplib2.Http(timeout=_HTTP_TIMEOUT_SECONDS)
                    http = None
                    try:
                        http = AuthorizedHttp(credentials, http=raw_http)
                        result = request.execute(http=http)
                    finally:
                        close = getattr(http or raw_http, "close", None)
                        if callable(close):
                            with suppress(Exception):
                                close()
                else:
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
                if mutation and api_error.retryable:
                    # A provider-side retryable response can arrive after the
                    # mutation was accepted. Do not advertise it as safe for a
                    # caller to replay automatically.
                    api_error.message = _AMBIGUOUS_MUTATION_MESSAGE
                    api_error.retryable = False
                    api_error.ambiguous_commit = True
            except GoogleCalendarApiError:
                raise
            except Exception as error:  # network/transport failure
                api_error = GoogleCalendarApiError(
                    kind="transient",
                    message=(
                        _AMBIGUOUS_MUTATION_MESSAGE
                        if mutation
                        else _ERROR_MESSAGES["transient"]
                    ),
                    retryable=not mutation,
                    attempts=attempt,
                    operation=operation,
                    ambiguous_commit=mutation,
                )
                self.tracer.event(
                    "calendar.error",
                    "Calendar API connection failed.",
                    operation=operation,
                    attempt=attempt,
                    error=type(error).__name__,
                )
                if attempt < max_attempts:
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
            if api_error.retryable and attempt < max_attempts:
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

    # -- async leaf adapters ------------------------------------------------------

    async def async_list_calendars(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return await bounded_to_thread(self.list_calendars, arguments)

    async def async_list_calendar_events(
        self,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await bounded_to_thread(self.list_calendar_events, arguments)

    async def async_get_calendar_event(
        self,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await bounded_to_thread(self.get_calendar_event, arguments)

    async def async_create_calendar_event(
        self,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await bounded_to_thread(self.create_calendar_event, arguments)

    async def async_update_calendar_event(
        self,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await bounded_to_thread(self.update_calendar_event, arguments)

    async def async_delete_calendar_event(
        self,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await bounded_to_thread(self.delete_calendar_event, arguments)

    async def async_get_freebusy(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return await bounded_to_thread(self.get_freebusy, arguments)


__all__ = ["GoogleCalendarClient"]
