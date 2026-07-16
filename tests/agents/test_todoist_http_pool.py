"""Transport and async-parity tests for the pooled Todoist HTTP client."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from agents.agent_api.app.config import load_settings
from agents.agent_api.app.tools.todoist import client as todoist_client_module
from agents.agent_api.app.tools.todoist.client import (
    TODOIST_REST_BASE_URL,
    TodoistApiClient,
    TodoistApiError,
    close_todoist_async_http_client,
    close_todoist_http_client,
    get_todoist_async_http_client,
    get_todoist_http_client,
)


FAST_SETTINGS = SimpleNamespace(
    todoist_max_retry_attempts=3,
    todoist_retry_total_timeout_seconds=8.0,
    todoist_retry_base_delay_seconds=0.1,
    todoist_retry_max_delay_seconds=1.0,
    todoist_http_timeout_seconds=30.0,
    todoist_http_max_keepalive_connections=10,
    todoist_http_max_connections=20,
)


class RecordingTracer:
    def __init__(self) -> None:
        self.events = []
        self.payloads = []
        self.progress_events = []

    def event(self, event_type, message, **details) -> None:
        self.events.append((event_type, message, details))

    def payload(self, event_type, direction, payload) -> None:
        self.payloads.append((event_type, direction, payload))

    def progress(self, event) -> None:
        self.progress_events.append(event)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.now += delay

    async def async_sleep(self, delay: float) -> None:
        self.now += delay


class ExpiringAsyncTimeout:
    def __init__(self, delay: float, observed: list[float]) -> None:
        self.delay = delay
        self.observed = observed

    async def __aenter__(self):
        self.observed.append(self.delay)
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is None:
            raise TimeoutError
        return False


def test_config_defaults_todoist_pool_limits(monkeypatch):
    monkeypatch.delenv("TODOIST_HTTP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("TODOIST_HTTP_MAX_KEEPALIVE_CONNECTIONS", raising=False)
    monkeypatch.delenv("TODOIST_HTTP_MAX_CONNECTIONS", raising=False)

    configured = load_settings()

    assert configured.todoist_http_timeout_seconds == 30.0
    assert configured.todoist_http_max_keepalive_connections == 10
    assert configured.todoist_http_max_connections == 20


def _sync_client(handler, api_key="token"):
    transport_client = httpx.Client(transport=httpx.MockTransport(handler))
    return TodoistApiClient(api_key=api_key, http_client=transport_client), transport_client


def test_sync_request_dispatches_json_with_per_request_bearer_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["authorization"] = request.headers.get("Authorization")
        captured["content_type"] = request.headers.get("Content-Type")
        captured["body"] = request.content
        return httpx.Response(200, json={"id": "new-task"})

    client, transport_client = _sync_client(handler, api_key="alice-token")
    try:
        result = client._request(
            f"{TODOIST_REST_BASE_URL}/tasks",
            "POST",
            {"content": "Buy milk"},
        )
    finally:
        transport_client.close()

    assert result == {"id": "new-task"}
    assert captured == {
        "method": "POST",
        "authorization": "Bearer alice-token",
        "content_type": "application/json",
        "body": b'{"content": "Buy milk"}',
    }


def test_shared_sync_transport_never_leaks_tokens_between_users():
    observed = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            observed.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"ok": True})

    shared = httpx.Client(transport=httpx.MockTransport(handler))
    alice = TodoistApiClient(api_key="alice-key", http_client=shared)
    bob = TodoistApiClient(api_key="bob-key", http_client=shared)

    def make_requests(client: TodoistApiClient) -> None:
        for _ in range(20):
            client._request(f"{TODOIST_REST_BASE_URL}/tasks")

    threads = [
        threading.Thread(target=make_requests, args=(alice,)),
        threading.Thread(target=make_requests, args=(bob,)),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        shared.close()

    assert observed.count("Bearer alice-key") == 20
    assert observed.count("Bearer bob-key") == 20


def test_shared_sync_pool_is_reused_and_recreated_after_close():
    close_todoist_http_client()
    first = Mock(spec=httpx.Client)
    second = Mock(spec=httpx.Client)

    with (
        patch.object(todoist_client_module, "settings", FAST_SETTINGS),
        patch.object(
            todoist_client_module.httpx,
            "Client",
            side_effect=[first, second],
        ) as constructor,
    ):
        assert get_todoist_http_client() is first
        assert get_todoist_http_client() is first
        assert constructor.call_count == 1
        close_todoist_http_client()
        first.close.assert_called_once_with()
        assert get_todoist_http_client() is second
        close_todoist_http_client()
        second.close.assert_called_once_with()


def test_sync_retry_deadline_caps_attempt_and_prevents_post_sleep_request():
    clock = FakeClock()
    bounded_settings = SimpleNamespace(
        **{
            **FAST_SETTINGS.__dict__,
            "todoist_retry_total_timeout_seconds": 1.0,
            "todoist_retry_base_delay_seconds": 2.0,
            "todoist_retry_max_delay_seconds": 2.0,
        }
    )
    http_client = Mock(spec=httpx.Client)
    http_client.request.return_value = httpx.Response(503, text="unavailable")
    client = TodoistApiClient(api_key="token", http_client=http_client)

    with (
        patch.object(todoist_client_module, "settings", bounded_settings),
        patch.object(todoist_client_module, "time", clock),
        patch.object(todoist_client_module.random, "uniform", return_value=0),
    ):
        with pytest.raises(TodoistApiError) as raised:
            client._request(f"{TODOIST_REST_BASE_URL}/tasks")

    assert raised.value.kind == "transient"
    assert raised.value.attempts == 1
    assert http_client.request.call_count == 1
    assert http_client.request.call_args.kwargs["timeout"] == 1.0
    assert clock.now == 101.0


def test_sync_rejects_success_response_that_arrives_after_deadline():
    clock = FakeClock()
    bounded_settings = SimpleNamespace(
        **{
            **FAST_SETTINGS.__dict__,
            "todoist_retry_total_timeout_seconds": 1.0,
        }
    )
    http_client = Mock(spec=httpx.Client)

    def late_response(*_args, **_kwargs):
        clock.now += 2.0
        return httpx.Response(200, json={"too_late": True})

    http_client.request.side_effect = late_response
    client = TodoistApiClient(api_key="token", http_client=http_client)

    with (
        patch.object(todoist_client_module, "settings", bounded_settings),
        patch.object(todoist_client_module, "time", clock),
    ):
        with pytest.raises(TodoistApiError) as raised:
            client._request(f"{TODOIST_REST_BASE_URL}/tasks")

    assert raised.value.kind == "transient"
    assert raised.value.attempts == 1
    assert http_client.request.call_count == 1


def test_sync_mutation_late_success_is_classified_as_ambiguous():
    clock = FakeClock()
    bounded_settings = SimpleNamespace(
        **{
            **FAST_SETTINGS.__dict__,
            "todoist_retry_total_timeout_seconds": 1.0,
        }
    )
    http_client = Mock(spec=httpx.Client)

    def late_response(*_args, **_kwargs):
        clock.now += 2.0
        return httpx.Response(200, json={"id": "possibly-created"})

    http_client.request.side_effect = late_response
    client = TodoistApiClient(api_key="token", http_client=http_client)

    with (
        patch.object(todoist_client_module, "settings", bounded_settings),
        patch.object(todoist_client_module, "time", clock),
    ):
        with pytest.raises(TodoistApiError) as raised:
            client._request(
                f"{TODOIST_REST_BASE_URL}/tasks",
                "POST",
                {"content": "do not duplicate"},
            )

    assert raised.value.ambiguous_commit is True
    assert raised.value.to_classifier_payload()["ambiguous_commit"] is True
    assert http_client.request.call_count == 1


@pytest.mark.parametrize("failure_kind", ["transport", "http"])
def test_sync_mutation_failure_is_never_retried(failure_kind):
    http_client = Mock(spec=httpx.Client)
    if failure_kind == "transport":
        request = httpx.Request("POST", f"{TODOIST_REST_BASE_URL}/tasks")
        http_client.request.side_effect = httpx.ReadTimeout("timed out", request=request)
    else:
        http_client.request.return_value = httpx.Response(503, text="unavailable")
    client = TodoistApiClient(api_key="token", http_client=http_client)

    with patch.object(todoist_client_module, "settings", FAST_SETTINGS):
        with pytest.raises(TodoistApiError) as raised:
            client._request(
                f"{TODOIST_REST_BASE_URL}/tasks",
                "POST",
                {"content": "do not duplicate"},
            )

    assert raised.value.kind == "transient"
    assert raised.value.retryable is False
    assert raised.value.ambiguous_commit is True
    assert raised.value.to_classifier_payload()["ambiguous_commit"] is True
    assert raised.value.attempts == 1
    assert http_client.request.call_count == 1


def test_sync_mutation_malformed_success_body_is_ambiguous():
    http_client = Mock(spec=httpx.Client)
    http_client.request.return_value = httpx.Response(200, text="{not-json")
    client = TodoistApiClient(api_key="token", http_client=http_client)

    with patch.object(todoist_client_module, "settings", FAST_SETTINGS):
        with pytest.raises(TodoistApiError) as raised:
            client._request(
                f"{TODOIST_REST_BASE_URL}/tasks",
                "POST",
                {"content": "do not duplicate"},
            )

    assert raised.value.ambiguous_commit is True
    assert raised.value.retryable is False
    assert http_client.request.call_count == 1


@pytest.mark.parametrize(
    ("request_error", "expected_kind", "expected_retryable", "expected_calls"),
    [
        (httpx.InvalidURL("invalid URL"), "validation", False, 1),
        (
            httpx.TooManyRedirects(
                "redirect loop",
                request=httpx.Request("GET", f"{TODOIST_REST_BASE_URL}/tasks"),
            ),
            "transient",
            True,
            3,
        ),
    ],
)
def test_sync_request_errors_are_classified(
    request_error,
    expected_kind,
    expected_retryable,
    expected_calls,
):
    http_client = Mock(spec=httpx.Client)
    http_client.request.side_effect = request_error
    client = TodoistApiClient(api_key="token", http_client=http_client)

    with (
        patch.object(todoist_client_module, "settings", FAST_SETTINGS),
        patch.object(todoist_client_module.time, "sleep"),
    ):
        with pytest.raises(TodoistApiError) as raised:
            client._request(f"{TODOIST_REST_BASE_URL}/tasks")

    assert raised.value.kind == expected_kind
    assert raised.value.retryable is expected_retryable
    assert http_client.request.call_count == expected_calls


def test_async_request_dispatches_and_parses_without_sync_transport():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["authorization"] = request.headers.get("Authorization")
        captured["content_type"] = request.headers.get("Content-Type")
        captured["body"] = request.content
        return httpx.Response(200, json={"id": "async-task"})

    async def exercise():
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sync_client = Mock(spec=httpx.Client)
        client = TodoistApiClient(
            api_key="bob-token",
            http_client=sync_client,
            async_http_client=async_client,
        )
        try:
            result = await client.async_request(
                f"{TODOIST_REST_BASE_URL}/tasks",
                "POST",
                {"content": "Async task"},
            )
        finally:
            await async_client.aclose()
        return result, sync_client

    result, sync_client = asyncio.run(exercise())

    assert result == {"id": "async-task"}
    assert captured == {
        "method": "POST",
        "authorization": "Bearer bob-token",
        "content_type": "application/json",
        "body": b'{"content": "Async task"}',
    }
    sync_client.request.assert_not_called()


def test_async_retry_matches_sync_classification_tracing_and_progress():
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "0"}, text="limited"),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    async def exercise():
        tracer = RecordingTracer()
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = TodoistApiClient(
            api_key="token",
            tracer=tracer,
            async_http_client=async_client,
        )
        try:
            with (
                patch.object(todoist_client_module, "settings", FAST_SETTINGS),
                patch.object(
                    todoist_client_module.asyncio,
                    "sleep",
                    new=AsyncMock(),
                ) as sleep,
            ):
                result = await client.async_request(f"{TODOIST_REST_BASE_URL}/tasks")
        finally:
            await async_client.aclose()
        return result, sleep, tracer

    result, sleep, tracer = asyncio.run(exercise())

    assert result == {"ok": True}
    sleep.assert_awaited_once_with(0.0)
    assert tracer.progress_events == [
        {
            "phase": "retrying",
            "action": "retrying",
            "domains": ["todoist"],
            "retry": {
                "target": "domain",
                "domain": "todoist",
                "reason": "rate_limited",
            },
        }
    ]
    assert [payload[1] for payload in tracer.payloads] == [
        "request",
        "error",
        "request",
        "response",
    ]


def test_async_transport_failure_retries_then_raises_transient():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    async def exercise():
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = TodoistApiClient(api_key="token", async_http_client=async_client)
        try:
            with (
                patch.object(todoist_client_module, "settings", FAST_SETTINGS),
                patch.object(todoist_client_module.asyncio, "sleep", new=AsyncMock()),
            ):
                with pytest.raises(TodoistApiError) as raised:
                    await client.async_request(f"{TODOIST_REST_BASE_URL}/tasks")
        finally:
            await async_client.aclose()
        return raised.value

    error = asyncio.run(exercise())

    assert error.kind == "transient"
    assert error.retryable is True
    assert error.attempts == 3
    assert calls == 3


def test_async_retry_deadline_caps_attempt_and_prevents_post_sleep_request():
    async def exercise():
        clock = FakeClock()
        bounded_settings = SimpleNamespace(
            **{
                **FAST_SETTINGS.__dict__,
                "todoist_retry_total_timeout_seconds": 1.0,
                "todoist_retry_base_delay_seconds": 2.0,
                "todoist_retry_max_delay_seconds": 2.0,
            }
        )
        async_client = AsyncMock(spec=httpx.AsyncClient)
        async_client.request.return_value = httpx.Response(503, text="unavailable")
        client = TodoistApiClient(api_key="token", async_http_client=async_client)

        with (
            patch.object(todoist_client_module, "settings", bounded_settings),
            patch.object(todoist_client_module, "time", clock),
            patch.object(todoist_client_module.random, "uniform", return_value=0),
            patch.object(
                todoist_client_module.asyncio,
                "sleep",
                side_effect=clock.async_sleep,
            ),
        ):
            with pytest.raises(TodoistApiError) as raised:
                await client.async_request(f"{TODOIST_REST_BASE_URL}/tasks")
        return raised.value, async_client, clock

    error, async_client, clock = asyncio.run(exercise())

    assert error.kind == "transient"
    assert error.attempts == 1
    assert async_client.request.await_count == 1
    assert async_client.request.call_args.kwargs["timeout"] == 1.0
    assert clock.now == 101.0


def test_async_outer_timeout_rejects_late_success_response():
    async def exercise():
        observed_timeouts: list[float] = []
        bounded_settings = SimpleNamespace(
            **{
                **FAST_SETTINGS.__dict__,
                "todoist_max_retry_attempts": 1,
                "todoist_retry_total_timeout_seconds": 1.0,
            }
        )
        async_client = AsyncMock(spec=httpx.AsyncClient)
        async_client.request.return_value = httpx.Response(200, json={"too_late": True})
        client = TodoistApiClient(api_key="token", async_http_client=async_client)

        with (
            patch.object(todoist_client_module, "settings", bounded_settings),
            patch.object(
                todoist_client_module.asyncio,
                "timeout",
                side_effect=lambda delay: ExpiringAsyncTimeout(delay, observed_timeouts),
            ),
        ):
            with pytest.raises(TodoistApiError) as raised:
                await client.async_request(f"{TODOIST_REST_BASE_URL}/tasks")
        return raised.value, async_client, observed_timeouts

    error, async_client, observed_timeouts = asyncio.run(exercise())

    assert error.kind == "transient"
    assert error.attempts == 1
    assert async_client.request.await_count == 1
    assert observed_timeouts == pytest.approx([1.0], abs=0.01)


@pytest.mark.parametrize("failure_kind", ["transport", "http"])
def test_async_mutation_failure_is_never_retried(failure_kind):
    async def exercise():
        async_client = AsyncMock(spec=httpx.AsyncClient)
        if failure_kind == "transport":
            request = httpx.Request("POST", f"{TODOIST_REST_BASE_URL}/tasks")
            async_client.request.side_effect = httpx.ReadTimeout(
                "timed out",
                request=request,
            )
        else:
            async_client.request.return_value = httpx.Response(503, text="unavailable")
        client = TodoistApiClient(api_key="token", async_http_client=async_client)

        with patch.object(todoist_client_module, "settings", FAST_SETTINGS):
            with pytest.raises(TodoistApiError) as raised:
                await client.async_request(
                    f"{TODOIST_REST_BASE_URL}/tasks",
                    "POST",
                    {"content": "do not duplicate"},
                )
        return raised.value, async_client

    error, async_client = asyncio.run(exercise())

    assert error.kind == "transient"
    assert error.retryable is False
    assert error.ambiguous_commit is True
    assert error.to_classifier_payload()["ambiguous_commit"] is True
    assert error.attempts == 1
    assert async_client.request.await_count == 1


def test_async_mutation_malformed_success_body_is_ambiguous():
    async def exercise():
        async_client = AsyncMock(spec=httpx.AsyncClient)
        async_client.request.return_value = httpx.Response(200, text="{not-json")
        client = TodoistApiClient(api_key="token", async_http_client=async_client)

        with patch.object(todoist_client_module, "settings", FAST_SETTINGS):
            with pytest.raises(TodoistApiError) as raised:
                await client.async_request(
                    f"{TODOIST_REST_BASE_URL}/tasks",
                    "POST",
                    {"content": "do not duplicate"},
                )
        return raised.value, async_client

    error, async_client = asyncio.run(exercise())

    assert error.ambiguous_commit is True
    assert error.retryable is False
    assert async_client.request.await_count == 1


@pytest.mark.parametrize(
    ("request_error", "expected_kind", "expected_retryable", "expected_calls"),
    [
        (
            httpx.UnsupportedProtocol(
                "unsupported protocol",
                request=httpx.Request("GET", "ftp://api.todoist.com/tasks"),
            ),
            "validation",
            False,
            1,
        ),
        (
            httpx.TooManyRedirects(
                "redirect loop",
                request=httpx.Request("GET", f"{TODOIST_REST_BASE_URL}/tasks"),
            ),
            "transient",
            True,
            3,
        ),
    ],
)
def test_async_request_errors_are_classified(
    request_error,
    expected_kind,
    expected_retryable,
    expected_calls,
):
    async def exercise():
        async_client = AsyncMock(spec=httpx.AsyncClient)
        async_client.request.side_effect = request_error
        client = TodoistApiClient(api_key="token", async_http_client=async_client)

        with (
            patch.object(todoist_client_module, "settings", FAST_SETTINGS),
            patch.object(todoist_client_module.asyncio, "sleep", new=AsyncMock()),
        ):
            with pytest.raises(TodoistApiError) as raised:
                await client.async_request(f"{TODOIST_REST_BASE_URL}/tasks")
        return raised.value, async_client

    error, async_client = asyncio.run(exercise())

    assert error.kind == expected_kind
    assert error.retryable is expected_retryable
    assert async_client.request.await_count == expected_calls


def test_async_auth_failure_does_not_construct_or_call_transport():
    async def exercise():
        async_client = AsyncMock(spec=httpx.AsyncClient)
        client = TodoistApiClient(api_key="", async_http_client=async_client)
        with pytest.raises(TodoistApiError) as raised:
            await client.async_request(f"{TODOIST_REST_BASE_URL}/tasks")
        return raised.value, async_client

    error, async_client = asyncio.run(exercise())

    assert error.kind == "auth"
    assert error.retryable is False
    async_client.request.assert_not_awaited()


def test_shared_async_pool_is_reused_and_recreated_after_close():
    async def exercise():
        await close_todoist_async_http_client()
        first = AsyncMock(spec=httpx.AsyncClient)
        second = AsyncMock(spec=httpx.AsyncClient)

        with (
            patch.object(todoist_client_module, "settings", FAST_SETTINGS),
            patch.object(
                todoist_client_module.httpx,
                "AsyncClient",
                side_effect=[first, second],
            ) as constructor,
        ):
            assert get_todoist_async_http_client() is first
            assert get_todoist_async_http_client() is first
            assert constructor.call_count == 1
            await close_todoist_async_http_client()
            first.aclose.assert_awaited_once_with()
            assert get_todoist_async_http_client() is second
            await close_todoist_async_http_client()
            second.aclose.assert_awaited_once_with()

    asyncio.run(exercise())


def test_shared_async_pool_is_not_reused_across_event_loops():
    first = AsyncMock(spec=httpx.AsyncClient)
    second = AsyncMock(spec=httpx.AsyncClient)

    async def get_client():
        return get_todoist_async_http_client()

    first_loop = asyncio.new_event_loop()
    second_loop = asyncio.new_event_loop()
    try:
        with (
            patch.object(todoist_client_module, "settings", FAST_SETTINGS),
            patch.object(
                todoist_client_module.httpx,
                "AsyncClient",
                side_effect=[first, second],
            ) as constructor,
        ):
            first_result = first_loop.run_until_complete(get_client())
            with pytest.raises(RuntimeError, match="another event loop"):
                second_loop.run_until_complete(get_client())

            first_loop.run_until_complete(close_todoist_async_http_client())
            second_result = second_loop.run_until_complete(get_client())
            second_loop.run_until_complete(close_todoist_async_http_client())

        assert first_result is first
        assert second_result is second
        assert second_result is not first_result
        assert constructor.call_count == 2
        first.aclose.assert_awaited_once_with()
        second.aclose.assert_awaited_once_with()
    finally:
        first_loop.close()
        second_loop.close()


def test_with_tracer_preserves_injected_transports_without_mutating_original():
    sync_client = Mock(spec=httpx.Client)
    async_client = AsyncMock(spec=httpx.AsyncClient)
    original_tracer = RecordingTracer()
    bound_tracer = RecordingTracer()
    original = TodoistApiClient(
        api_key="token",
        tracer=original_tracer,
        http_client=sync_client,
        async_http_client=async_client,
    )

    bound = original.with_tracer(bound_tracer)

    assert bound is not original
    assert original.tracer is original_tracer
    assert bound.tracer is bound_tracer
    assert bound._http_client is sync_client
    assert bound._async_http_client is async_client
