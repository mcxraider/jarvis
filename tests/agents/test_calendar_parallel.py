"""Calendar transport isolation and async leaf-adapter tests."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.agent_api.app.tools.google_calendar.auth import GoogleCalendarApiError
from agents.agent_api.app.tools.google_calendar.client import GoogleCalendarClient


class _BarrierRequest:
    def __init__(self, barrier: threading.Barrier, seen_http: list[object]) -> None:
        self._barrier = barrier
        self._seen_http = seen_http

    def execute(self, http=None) -> dict:
        self._seen_http.append(http)
        self._barrier.wait(timeout=2.0)
        return {"items": []}


class _BarrierService:
    def __init__(self, barrier: threading.Barrier, seen_http: list[object]) -> None:
        self._barrier = barrier
        self._seen_http = seen_http

    def events(self) -> "_BarrierService":
        return self

    def list(self, **_kwargs) -> _BarrierRequest:
        return _BarrierRequest(self._barrier, self._seen_http)


def test_parallel_calls_use_distinct_authorized_http_transports() -> None:
    """Every overlapping request owns its httplib2 socket and authorized wrapper."""

    call_count = 4
    barrier = threading.Barrier(call_count)
    seen_http: list[object] = []
    raw_transports: list[object] = []
    authorized_transports: list[object] = []
    credentials = object()
    client = GoogleCalendarClient(
        service=_BarrierService(barrier, seen_http),
        credentials=credentials,
    )
    arguments = {
        "time_min": "2026-07-06T00:00:00+08:00",
        "time_max": "2026-07-07T00:00:00+08:00",
    }

    def make_http(*, timeout) -> object:
        assert timeout == 30.0
        transport = object()
        raw_transports.append(transport)
        return transport

    class FakeAuthorizedHttp:
        def __init__(self, received_credentials, *, http) -> None:
            assert received_credentials._coordinator.credentials is credentials
            assert http in raw_transports
            self.http = http
            self.closed = False
            authorized_transports.append(self)

        def close(self) -> None:
            self.closed = True

    with patch("httplib2.Http", side_effect=make_http), patch(
        "google_auth_httplib2.AuthorizedHttp",
        FakeAuthorizedHttp,
    ):
        with ThreadPoolExecutor(max_workers=call_count) as executor:
            results = list(
                executor.map(
                    lambda _: client.list_calendar_events(arguments),
                    range(call_count),
                )
            )

    assert all(result["events"] == [] for result in results)
    assert len({id(item) for item in raw_transports}) == call_count
    assert len({id(item) for item in authorized_transports}) == call_count
    assert {id(item) for item in seen_http} == {
        id(item) for item in authorized_transports
    }
    assert all(item.closed for item in authorized_transports)


def test_request_failure_closes_every_retry_transport() -> None:
    client = GoogleCalendarClient(service=object(), credentials=object())
    authorized_transports = []
    observed_timeouts = []

    class FailingRequest:
        def execute(self, *, http):
            raise OSError("network failed")

    class FakeAuthorizedHttp:
        def __init__(self, _credentials, *, http) -> None:
            self.http = http
            self.closed = False
            authorized_transports.append(self)

        def close(self) -> None:
            self.closed = True

    def make_http(*, timeout):
        observed_timeouts.append(timeout)
        return object()

    with patch(
        "httplib2.Http",
        side_effect=make_http,
    ), patch(
        "google_auth_httplib2.AuthorizedHttp",
        FakeAuthorizedHttp,
    ), patch("time.sleep"):
        with pytest.raises(GoogleCalendarApiError, match="temporarily unavailable"):
            client._execute(FailingRequest(), "calendar.events.list")

    assert len(authorized_transports) == 3
    assert observed_timeouts == [30.0, 30.0, 30.0]
    assert all(item.closed for item in authorized_transports)


def test_concurrent_credential_refresh_happens_once_per_generation() -> None:
    class Credentials:
        token = "old-token"
        expiry = None

        def __init__(self) -> None:
            self.refresh_count = 0

        def before_request(self, *_args) -> None:
            pass

        def refresh(self, _request) -> None:
            self.refresh_count += 1
            self.token = f"new-token-{self.refresh_count}"

    credentials = Credentials()
    client = GoogleCalendarClient(service=object(), credentials=credentials)
    proxies = [client._credential_coordinator.proxy() for _ in range(2)]
    barrier = threading.Barrier(2)

    def refresh(proxy) -> None:
        barrier.wait(timeout=2.0)
        proxy.refresh(object())

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(refresh, proxies))

    assert credentials.refresh_count == 1
    assert credentials.token == "new-token-1"


def test_async_refresh_coordinates_with_sync_via_shared_generation() -> None:
    """Async token refresh bumps coordinator generation, preventing redundant sync refresh."""
    from datetime import datetime, timedelta, timezone
    from agents.agent_api.app.tools.google_calendar.client import (
        _AsyncTokenManager,
        _CredentialCoordinator,
    )

    class FakeCredentials:
        token = "expired-token"
        expiry = datetime(2020, 1, 1, tzinfo=timezone.utc)
        refresh_token = "rt"
        client_id = "cid"
        client_secret = "cs"
        valid = False
        refresh_count = 0

        def refresh(self, _request):
            self.refresh_count += 1
            self.token = f"sync-token-{self.refresh_count}"

    creds = FakeCredentials()
    coordinator = _CredentialCoordinator(creds)
    token_mgr = _AsyncTokenManager(coordinator)

    assert coordinator.generation == 0

    # Create a sync proxy BEFORE async refresh (simulates concurrent access)
    proxy = coordinator.proxy()

    async def exercise():
        with patch("httpx.AsyncClient") as MockClient:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "access_token": "new-token",
                "expires_in": 3600,
            }
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_http

            token = await token_mgr.get_access_token()

        assert token == "new-token"
        assert creds.token == "new-token"
        assert coordinator.generation == 1

        # Sync proxy created at generation 0 sees mismatch and skips refresh
        proxy.refresh(object())
        assert creds.refresh_count == 0  # sync refresh was skipped
        assert coordinator.generation == 1  # not incremented again

    asyncio.run(exercise())


def test_async_token_manager_skips_refresh_when_sync_already_refreshed() -> None:
    """If sync refreshes first (making creds valid), async skips the HTTP call."""
    from datetime import datetime, timedelta, timezone
    from agents.agent_api.app.tools.google_calendar.client import (
        _AsyncTokenManager,
        _CredentialCoordinator,
    )

    class FakeCredentials:
        token = "fresh-token"
        refresh_token = "rt"
        client_id = "cid"
        client_secret = "cs"

        @property
        def valid(self):
            return True

    creds = FakeCredentials()
    coordinator = _CredentialCoordinator(creds)
    token_mgr = _AsyncTokenManager(coordinator)

    async def exercise():
        with patch("httpx.AsyncClient") as MockClient:
            token = await token_mgr.get_access_token()
            MockClient.assert_not_called()
            assert token == "fresh-token"

    asyncio.run(exercise())


def test_lazy_service_retains_credentials_for_per_call_transport() -> None:
    credentials = object()
    service = object()
    client = GoogleCalendarClient(credential_json="credential-json")

    with patch(
        "agents.agent_api.app.tools.google_calendar.client.load_credentials",
        return_value=credentials,
    ) as load_credentials, patch(
        "googleapiclient.discovery.build",
        return_value=service,
    ) as build:
        assert client.service is service

    load_credentials.assert_called_once_with(
        None,
        credential_json="credential-json",
        persist_callback=None,
    )
    build.assert_called_once_with(
        "calendar",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )
    assert client._credentials is credentials


def test_with_tracer_clone_shares_transport_inputs() -> None:
    credentials = object()
    service = object()
    client = GoogleCalendarClient(service=service, credentials=credentials)

    clone = client.with_tracer(MagicMock())

    assert clone is not client
    assert clone._service is service
    assert clone._credentials is credentials
    assert clone._lock is client._lock


def test_async_calendar_methods_are_native_coroutines() -> None:
    import inspect

    client = GoogleCalendarClient(service=MagicMock())
    method_pairs = (
        ("async_list_calendars", "list_calendars"),
        ("async_list_calendar_events", "list_calendar_events"),
        ("async_get_calendar_event", "get_calendar_event"),
        ("async_create_calendar_event", "create_calendar_event"),
        ("async_update_calendar_event", "update_calendar_event"),
        ("async_delete_calendar_event", "delete_calendar_event"),
        ("async_get_freebusy", "get_freebusy"),
    )

    # Calendar leaf calls now use the native async HTTP transport; they must
    # remain true coroutine entry points and never fall back to a thread pool.
    for async_name, _sync_name in method_pairs:
        assert inspect.iscoroutinefunction(getattr(client, async_name))
