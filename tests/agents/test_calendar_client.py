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
            "items": [{"id": "primary", "summary": "Jerry", "primary": True, "timeZone": "Asia/Taipei"}],
            "nextPageToken": "next-calendars",
        }
        out = _client(service).list_calendars({"page_token": "current-calendars"})
        assert out["calendars"][0] == {
            "calendar_id": "primary",
            "summary": "Jerry",
            "primary": True,
            "time_zone": "Asia/Taipei",
        }
        _, kwargs = service.calendarList().list.call_args
        assert kwargs == {"maxResults": 50, "pageToken": "current-calendars"}
        assert out["next_page_token"] == "next-calendars"

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
        assert kwargs["maxResults"] == 50
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
        assert kwargs["maxResults"] == 50
        assert "q" not in kwargs

    def test_list_events_preserves_explicit_limit_and_page_token(self):
        service = MagicMock()
        service.events().list().execute.return_value = {"items": []}

        _client(service).list_calendar_events(
            {
                "time_min": "2026-07-02T00:00:00+08:00",
                "time_max": "2026-07-03T00:00:00+08:00",
                "max_results": 125,
                "page_token": "opaque-events",
            }
        )

        _, kwargs = service.events().list.call_args
        assert kwargs["maxResults"] == 125
        assert kwargs["pageToken"] == "opaque-events"

    @pytest.mark.parametrize("max_results", [0, 251, True, "50"])
    def test_invalid_collection_limit_fails_before_api_call(self, max_results):
        service = MagicMock()

        with pytest.raises(ValueError, match="max_results"):
            _client(service).list_calendars({"max_results": max_results})

        service.calendarList().list.assert_not_called()

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


class TestAsyncNativeHTTPX:
    """Verify async methods use httpx directly (no thread pool)."""

    def test_async_list_events_uses_httpx(self, monkeypatch):
        import asyncio
        import httpx as _httpx

        raw_event = {
            "id": "evt-async",
            "summary": "Async test",
            "start": {"dateTime": "2026-07-02T09:00:00+08:00"},
            "end": {"dateTime": "2026-07-02T10:00:00+08:00"},
            "status": "confirmed",
        }
        mock_response = _httpx.Response(
            200,
            json={"items": [raw_event], "nextPageToken": None},
            request=_httpx.Request("GET", "https://example.com"),
        )

        class FakeAsyncClient:
            async def get(self, url, **kwargs):
                return mock_response

        class FakeCreds:
            token = "fake-token"
            valid = True
            expired = False
            refresh_token = "rt"
            client_id = "cid"
            client_secret = "cs"

        import agents.agent_api.app.tools.google_calendar.client as cal_mod
        monkeypatch.setattr(cal_mod, "_get_calendar_async_http_client", lambda: FakeAsyncClient())

        from agents.agent_api.app.tools.google_calendar.client import (
            GoogleCalendarClient,
            _AsyncTokenManager,
            _CredentialCoordinator,
        )

        creds = FakeCreds()
        client = GoogleCalendarClient(service=MagicMock(), credentials=creds)
        client._async_token_manager = _AsyncTokenManager(_CredentialCoordinator(creds))

        result = asyncio.run(client.async_list_calendar_events({
            "time_min": "2026-07-02T00:00:00+08:00",
            "time_max": "2026-07-03T00:00:00+08:00",
        }))

        assert result["events"][0]["event_id"] == "evt-async"
        assert result["events"][0]["summary"] == "Async test"

    def test_async_error_classification(self, monkeypatch):
        import asyncio
        import httpx as _httpx

        class FakeAsyncClient:
            async def get(self, url, **kwargs):
                return _httpx.Response(
                    404,
                    json={"error": {"message": "not found"}},
                    request=_httpx.Request("GET", url),
                )

        class FakeCreds:
            token = "fake-token"
            valid = True
            expired = False
            refresh_token = "rt"
            client_id = "cid"
            client_secret = "cs"

        import agents.agent_api.app.tools.google_calendar.client as cal_mod
        monkeypatch.setattr(cal_mod, "_get_calendar_async_http_client", lambda: FakeAsyncClient())

        from agents.agent_api.app.tools.google_calendar.client import (
            GoogleCalendarClient,
            _AsyncTokenManager,
            _CredentialCoordinator,
        )

        creds = FakeCreds()
        client = GoogleCalendarClient(service=MagicMock(), credentials=creds)
        client._async_token_manager = _AsyncTokenManager(_CredentialCoordinator(creds))

        with pytest.raises(GoogleCalendarApiError) as excinfo:
            asyncio.run(client.async_get_calendar_event({"event_id": "missing"}))
        assert excinfo.value.kind == "not-found"
        assert excinfo.value.retryable is False


class TestAsyncInfraLifecycle:
    """Verify _ensure_async_infra race safety and close_calendar_async_http_client."""

    def test_concurrent_ensure_creates_one_token_manager(self):
        import asyncio

        class FakeCreds:
            token = "t"
            valid = True
            expired = False
            refresh_token = "rt"
            client_id = "cid"
            client_secret = "cs"

        from agents.agent_api.app.tools.google_calendar.client import (
            GoogleCalendarClient,
            _AsyncTokenManager,
        )

        client = GoogleCalendarClient(service=MagicMock(), credentials=FakeCreds())

        async def race():
            results = await asyncio.gather(*[client._ensure_async_infra() for _ in range(10)])
            return results

        managers = asyncio.run(race())
        assert all(m is managers[0] for m in managers)

    def test_close_calendar_async_http_client(self, monkeypatch):
        import asyncio
        import agents.agent_api.app.tools.google_calendar.client as cal_mod

        monkeypatch.setattr(cal_mod, "_shared_async_http_client", None)
        monkeypatch.setattr(cal_mod, "_shared_async_http_client_loop", None)

        async def run():
            client = cal_mod._get_calendar_async_http_client()
            assert client is not None
            assert cal_mod._shared_async_http_client is client
            await cal_mod.close_calendar_async_http_client()
            assert cal_mod._shared_async_http_client is None

        asyncio.run(run())


class TestGcalTraceInputs:
    """Task 3: _gcal_trace_inputs leaks nothing sensitive."""

    def test_keeps_operation_and_method_only(self):
        from agents.agent_api.app.tools.google_calendar.client import _gcal_trace_inputs

        fake_self = object()
        result = _gcal_trace_inputs({
            "self": fake_self,
            "operation": "calendar.events.list",
            "method": "GET",
            "url": "https://www.googleapis.com/calendar/v3/calendars/user%40example.com/events",
            "params": {"timeMin": "2026-08-01T00:00:00Z"},
            "json_body": {"summary": "secret meeting"},
            "request": object(),
        })

        assert result == {"operation": "calendar.events.list", "method": "GET"}
        # url, params, json_body, request, self must not appear
        for banned in ("url", "params", "json_body", "request", "self"):
            assert banned not in result

    def test_execute_shape_no_method_does_not_raise(self):
        """_execute signature has no 'method' arg — process_inputs must not raise."""
        from agents.agent_api.app.tools.google_calendar.client import _gcal_trace_inputs

        result = _gcal_trace_inputs({
            "self": object(),
            "request": object(),
            "operation": "calendar.calendars.list",
        })

        assert result == {"operation": "calendar.calendars.list"}
        assert "method" not in result
