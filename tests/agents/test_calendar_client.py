"""Stage 2: GoogleCalendarClient method routing, normalization, errors, retry.

A MagicMock stands in for the Google discovery service, so no network is used.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from agents.agent_api.app.tools.google_calendar.auth import GoogleCalendarApiError
from agents.agent_api.app.tools.google_calendar.client import GoogleCalendarClient


def _http_error(status: int) -> HttpError:
    resp = type("Resp", (), {"status": status, "reason": "err"})()
    return HttpError(resp, b'{"error": {"message": "boom"}}')


def _client(service: MagicMock) -> GoogleCalendarClient:
    return GoogleCalendarClient(service=service)


class TestReadMethods:
    def test_list_calendars(self):
        service = MagicMock()
        service.calendarList().list().execute.return_value = {
            "items": [{"id": "primary", "summary": "Jerry", "primary": True, "timeZone": "Asia/Taipei"}]
        }
        out = _client(service).list_calendars({})
        assert out["calendars"][0] == {
            "calendar_id": "primary",
            "summary": "Jerry",
            "primary": True,
            "time_zone": "Asia/Taipei",
        }

    def test_list_events_defaults_and_normalizes(self):
        service = MagicMock()
        raw = {
            "id": "evt1",
            "summary": "Standup",
            "start": {"dateTime": "2026-07-02T09:00:00+08:00"},
            "end": {"dateTime": "2026-07-02T09:30:00+08:00"},
            "location": "Zoom",
            "attendees": [{"email": "a@co.com"}, {"displayName": "no-email"}],
            "status": "confirmed",
            "etag": "SHOULD_BE_STRIPPED",
            "iCalUID": "SHOULD_BE_STRIPPED",
            "creator": {"email": "x"},
        }
        service.events().list().execute.return_value = {"items": [raw], "nextPageToken": "tok"}

        out = _client(service).list_calendar_events(
            {"time_min": "2026-07-02T00:00:00+08:00", "time_max": "2026-07-03T00:00:00+08:00"}
        )

        # Correct resource + default calendar_id + orderBy only when singleEvents.
        _, kwargs = service.events().list.call_args
        assert kwargs["calendarId"] == "primary"
        assert kwargs["singleEvents"] is True
        assert kwargs["orderBy"] == "startTime"
        # Normalized shape — verbose Google fields stripped.
        event = out["events"][0]
        assert event == {
            "event_id": "evt1",
            "summary": "Standup",
            "start": "2026-07-02T09:00:00+08:00",
            "end": "2026-07-02T09:30:00+08:00",
            "location": "Zoom",
            "attendees": ["a@co.com"],
            "status": "confirmed",
        }
        assert out["next_page_token"] == "tok"

    def test_list_events_explicit_none_single_events_defaults_true(self):
        # The LangChain wrapper passes omitted optionals as an explicit None
        # (not absent), so the client must still default singleEvents=True and
        # set orderBy=startTime — otherwise recurrences silently stop expanding.
        service = MagicMock()
        service.events().list().execute.return_value = {"items": []}
        _client(service).list_calendar_events(
            {
                "time_min": "2026-07-02T00:00:00+08:00",
                "time_max": "2026-07-03T00:00:00+08:00",
                "single_events": None,
                "calendar_id": None,
                "max_results": None,
                "q": None,
            }
        )
        _, kwargs = service.events().list.call_args
        assert kwargs["singleEvents"] is True
        assert kwargs["orderBy"] == "startTime"
        assert kwargs["calendarId"] == "primary"
        assert "maxResults" not in kwargs
        assert "q" not in kwargs

    def test_list_events_without_single_events_omits_orderby(self):
        service = MagicMock()
        service.events().list().execute.return_value = {"items": []}
        _client(service).list_calendar_events(
            {
                "time_min": "2026-07-02T00:00:00+08:00",
                "time_max": "2026-07-03T00:00:00+08:00",
                "single_events": False,
            }
        )
        _, kwargs = service.events().list.call_args
        assert "orderBy" not in kwargs

    def test_get_event_normalizes(self):
        service = MagicMock()
        service.events().get().execute.return_value = {
            "id": "evt9",
            "summary": "1:1",
            "start": {"dateTime": "2026-07-02T14:00:00+08:00"},
            "end": {"dateTime": "2026-07-02T14:30:00+08:00"},
            "status": "confirmed",
        }
        out = _client(service).get_calendar_event({"event_id": "evt9"})
        assert out["event_id"] == "evt9"
        _, kwargs = service.events().get.call_args
        assert kwargs["calendarId"] == "primary"
        assert kwargs["eventId"] == "evt9"

    def test_freebusy(self):
        service = MagicMock()
        service.freebusy().query().execute.return_value = {
            "calendars": {"primary": {"busy": [{"start": "s", "end": "e"}]}}
        }
        out = _client(service).get_freebusy(
            {"time_min": "2026-07-02T00:00:00+08:00", "time_max": "2026-07-03T00:00:00+08:00"}
        )
        assert out["calendars"]["primary"]["busy"] == [{"start": "s", "end": "e"}]
        _, kwargs = service.freebusy().query.call_args
        assert kwargs["body"]["items"] == [{"id": "primary"}]


class TestMutations:
    def test_create_builds_timed_body(self):
        service = MagicMock()
        service.events().insert().execute.return_value = {"id": "new1", "summary": "Meet"}
        _client(service).create_calendar_event(
            {
                "summary": "Meet",
                "start_datetime": "2026-07-02T14:00:00+08:00",
                "end_datetime": "2026-07-02T15:00:00+08:00",
                "timezone": "Asia/Taipei",
                "attendees": ["a@co.com"],
            }
        )
        _, kwargs = service.events().insert.call_args
        body = kwargs["body"]
        assert body["summary"] == "Meet"
        assert body["start"] == {"dateTime": "2026-07-02T14:00:00+08:00", "timeZone": "Asia/Taipei"}
        assert body["attendees"] == [{"email": "a@co.com"}]

    def test_create_builds_all_day_body(self):
        service = MagicMock()
        service.events().insert().execute.return_value = {"id": "n", "summary": "Holiday"}
        _client(service).create_calendar_event(
            {"summary": "Holiday", "start_date": "2026-07-02", "end_date": "2026-07-03"}
        )
        _, kwargs = service.events().insert.call_args
        assert kwargs["body"]["start"] == {"date": "2026-07-02"}
        assert kwargs["body"]["end"] == {"date": "2026-07-03"}

    def test_update_uses_patch_and_pops_ids(self):
        service = MagicMock()
        service.events().patch().execute.return_value = {"id": "e1", "summary": "New"}
        _client(service).update_calendar_event({"event_id": "e1", "summary": "New"})
        _, kwargs = service.events().patch.call_args
        assert kwargs["eventId"] == "e1"
        assert kwargs["calendarId"] == "primary"
        assert "event_id" not in kwargs["body"]
        assert kwargs["body"]["summary"] == "New"

    def test_delete_returns_success_envelope(self):
        service = MagicMock()
        service.events().delete().execute.return_value = ""
        out = _client(service).delete_calendar_event({"event_id": "e5"})
        assert out == {"success": True, "message": "Event e5 deleted", "event_id": "e5"}


class TestErrorClassification:
    def test_404_is_not_found(self):
        service = MagicMock()
        service.events().get().execute.side_effect = _http_error(404)
        with pytest.raises(GoogleCalendarApiError) as excinfo:
            _client(service).get_calendar_event({"event_id": "missing"})
        assert excinfo.value.kind == "not-found"
        assert excinfo.value.retryable is False

    def test_401_is_auth_reconnect(self):
        service = MagicMock()
        service.calendarList().list().execute.side_effect = _http_error(401)
        with pytest.raises(GoogleCalendarApiError) as excinfo:
            _client(service).list_calendars({})
        assert excinfo.value.kind == "auth"
        assert excinfo.value.reconnect is True

    def test_429_retried_then_raised(self, monkeypatch):
        monkeypatch.setattr(
            "agents.agent_api.app.tools.google_calendar.client.GoogleCalendarClient._sleep_before_retry",
            lambda self, attempt: None,
        )
        service = MagicMock()
        service.events().list().execute.side_effect = _http_error(429)
        with pytest.raises(GoogleCalendarApiError) as excinfo:
            _client(service).list_calendar_events(
                {"time_min": "2026-07-02T00:00:00+08:00", "time_max": "2026-07-03T00:00:00+08:00"}
            )
        assert excinfo.value.kind == "rate-limit"
        # Retried up to the max attempt count.
        assert service.events().list().execute.call_count == 3

    @pytest.mark.parametrize(
        "operation",
        [
            "calendar.events.insert",
            "calendar.events.patch",
            "calendar.events.delete",
        ],
    )
    @pytest.mark.parametrize("failure", [_http_error(503), OSError("connection reset")])
    def test_mutations_never_retry_ambiguous_or_retryable_failures(
        self,
        operation,
        failure,
    ):
        request = MagicMock()
        request.execute.side_effect = failure

        with pytest.raises(GoogleCalendarApiError) as excinfo:
            _client(MagicMock())._execute(request, operation)

        assert excinfo.value.kind == "transient"
        assert excinfo.value.retryable is False
        assert "Check the calendar before trying again" in excinfo.value.message
        request.execute.assert_called_once_with()


class _OverlapProbe:
    """A fake Google request whose execute() records concurrent overlap.

    ``execute`` briefly sleeps *outside* the counter mutex to widen the window in
    which a second thread could enter — so ``max_concurrent`` reflects the true
    peak overlap seen on the shared socket, not a serialization artifact of the
    probe itself.
    """

    def __init__(self, state: dict) -> None:
        self._state = state

    def execute(self, http=None) -> dict:
        state = self._state
        with state["mutex"]:
            state["active"] += 1
            state["max_concurrent"] = max(state["max_concurrent"], state["active"])
        time.sleep(0.01)
        with state["mutex"]:
            state["active"] -= 1
        return {"items": []}


class _OverlapService:
    """Minimal stand-in for the discovery service returning ``_OverlapProbe``s."""

    def __init__(self, state: dict) -> None:
        self._state = state

    def events(self) -> "_OverlapService":
        return self

    def list(self, **_kwargs) -> _OverlapProbe:
        return _OverlapProbe(self._state)


class TestThreadSafety:
    def test_concurrent_injected_service_calls_are_not_serialized(self):
        # Injected services have no OAuth credential/transport. They retain the
        # bare execute() seam, but network execution is no longer guarded by the
        # discovery-construction lock.
        state = {"mutex": threading.Lock(), "active": 0, "max_concurrent": 0}
        client = GoogleCalendarClient(service=_OverlapService(state))
        args = {
            "time_min": "2026-07-06T00:00:00+08:00",
            "time_max": "2026-07-07T00:00:00+08:00",
        }

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(
                executor.map(lambda _: client.list_calendar_events(args), range(10))
            )

        assert state["max_concurrent"] > 1
        assert all(result["events"] == [] for result in results)
