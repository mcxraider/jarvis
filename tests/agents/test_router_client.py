"""Unit tests for RouterClient retry/backoff, tracing, and response parsing.

Mirrors ``test_deepseek_client.py``: the router reuses the same OpenAI-compatible
retry scaffolding, so the transport-error tests are structurally identical. The
extra surface is response parsing — a malformed or schema-invalid body must fail
terminally (non-retryable) so the caller falls back to the static selector.
"""

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Disable tracing before importing anything that touches LangSmith/LangChain.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

# Clear proxy env vars so httpx doesn't try to use a SOCKS/HTTP proxy when
# instantiating the OpenAI client in unit tests.
for _proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_proxy_var, None)

from httpx import ConnectTimeout, Request, Response as HttpxResponse
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

# Patch wrap_openai before importing the client so it does not decorate the
# OpenAI client with actual LangSmith instrumentation during tests.
with patch("langsmith.wrappers.wrap_openai", side_effect=lambda c, **_: c):
    from agents.agent_api.app.router import client as router_client_module
    from agents.agent_api.app.router.client import (
        RouterClient,
        RouterClientError,
        close_shared_async_router_openai_client,
        close_shared_router_client,
        get_shared_async_router_openai_client,
        get_shared_router_client,
    )
    from agents.agent_api.app.router.prompt import RouterDecision

from agents.agent_api.app.graph.nodes.orchestrator import UsageSummary
from agents.agent_api.app.llm.chat import UsageLedger
from agents.agent_api.app.llm.provider import OpenAIChatProfile
from tests.agents.runtime_helpers import make_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NO_SLEEP = lambda _: None  # noqa: E731 — skip real delays in retry loops

VALID_DECISION_JSON = '{"outcome": "routed", "domains": ["todoist"], "uncertain": false, "candidate_domains": [], "complexity": "low"}'

SNAPSHOT = make_snapshot()


@pytest.fixture(autouse=True)
def _reset_shared_router_clients():
    close_shared_router_client()
    asyncio.run(close_shared_async_router_openai_client())
    yield
    close_shared_router_client()
    asyncio.run(close_shared_async_router_openai_client())


def make_response(content=VALID_DECISION_JSON, prompt_tokens=12, completion_tokens=6, cached_tokens=0):
    """Build a mock OpenAI-compatible chat completion whose message.content is JSON."""
    message = MagicMock()
    message.content = content
    message.tool_calls = None
    message.refusal = None
    message.reasoning_content = None

    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = prompt_tokens + completion_tokens
    usage.prompt_cache_hit_tokens = cached_tokens
    usage.prompt_cache_miss_tokens = 0
    usage.prompt_tokens_details = None
    usage.completion_tokens_details = None

    response = MagicMock()
    response.choices = [MagicMock(message=message, finish_reason="stop")]
    response.usage = usage
    response.model = "deepseek-v4-flash"
    response.service_tier = None
    response._request_id = None
    response.request_id = None
    response.id = None
    return response


def make_timeout_error(cause=None):
    error = APITimeoutError(request=Request("POST", "https://api.deepseek.com/v1/chat/completions"))
    if cause is not None:
        error.__cause__ = cause
    return error


def make_connection_error():
    return APIConnectionError(
        request=Request("POST", "https://api.deepseek.com/v1/chat/completions"),
        message="Connection refused",
    )


def make_rate_limit_error():
    request = Request("POST", "https://api.deepseek.com/v1/chat/completions")
    response = HttpxResponse(429, json={"error": {"message": "rate limited"}}, request=request)
    return RateLimitError(
        message="Rate limit exceeded",
        response=response,
        body={"error": {"message": "rate limited"}},
    )


def make_status_error(status_code: int, message: str = "Error", headers=None):
    request = Request("POST", "https://api.deepseek.com/v1/chat/completions")
    response = HttpxResponse(
        status_code,
        json={"error": {"message": message}},
        request=request,
        headers=headers,
    )
    return APIStatusError(message=message, response=response, body={"error": {"message": message}})


def build_client(max_retry_attempts=2):
    """Build an explicit DeepSeek RouterClient, patching wrap_openai."""
    with patch("langsmith.wrappers.wrap_openai", side_effect=lambda c, **_: c):
        client = RouterClient(
            api_key="test-key",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com/v1",
            reasoning_effort="off",
            max_retry_attempts=max_retry_attempts,
            retry_sleep=NO_SLEEP,
        )
    return client


class RecordingTracer:
    def __init__(self):
        self.events = []
        self.payloads = []

    def event(self, stage, message, **fields):
        self.events.append({"stage": stage, "message": message, "fields": fields})

    def payload(self, stage, label, value, limit=900):
        self.payloads.append(
            {"stage": stage, "label": label, "value": value, "limit": limit}
        )


def classify(client):
    return client.classify("add buy milk", SNAPSHOT)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestMissingApiKey:
    def test_missing_api_key_raises(self, monkeypatch):
        """No key argument, no ROUTER_API_KEY constant, no env -> RuntimeError."""
        monkeypatch.setattr("agents.agent_api.app.router.client.ROUTER_API_KEY", None)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with patch("langsmith.wrappers.wrap_openai", side_effect=lambda c, **_: c):
            with pytest.raises(RuntimeError, match="required to run the router"):
                RouterClient(api_key=None)


# ---------------------------------------------------------------------------
# Retryable transport errors
# ---------------------------------------------------------------------------


class TestRetryableErrors:
    def test_timeout_triggers_retry(self):
        client = build_client(max_retry_attempts=3)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[make_timeout_error(), make_response()],
        ) as mock_create:
            decision = classify(client)
        assert decision.domains == ["todoist"]
        assert mock_create.call_count == 2

    def test_connection_error_triggers_retry(self):
        client = build_client(max_retry_attempts=3)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[make_connection_error(), make_response()],
        ) as mock_create:
            classify(client)
        assert mock_create.call_count == 2

    def test_rate_limit_429_triggers_retry(self):
        client = build_client(max_retry_attempts=3)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[make_rate_limit_error(), make_response()],
        ) as mock_create:
            classify(client)
        assert mock_create.call_count == 2

    def test_status_500_triggers_retry(self):
        client = build_client(max_retry_attempts=3)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[make_status_error(500, "Internal Server Error"), make_response()],
        ) as mock_create:
            classify(client)
        assert mock_create.call_count == 2

    def test_retry_trace_includes_attempt_metadata(self):
        tracer = RecordingTracer()
        client = RouterClient(
            api_key="test-key",
            max_retry_attempts=3,
            retry_sleep=NO_SLEEP,
            tracer=tracer,
        )
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[make_timeout_error(), make_response()],
        ):
            classify(client)
        retry_events = [event for event in tracer.events if event["stage"] == "router.retry"]
        assert len(retry_events) == 1
        fields = retry_events[0]["fields"]
        assert fields["attempt"] == 1
        assert fields["error_type"] == "timeout"
        assert fields["exception_type"] == "APITimeoutError"
        assert fields["timeout_kind"] == "timeout"
        assert "retry_sleep_seconds" in fields


# ---------------------------------------------------------------------------
# Non-retryable transport errors
# ---------------------------------------------------------------------------


class TestNonRetryableErrors:
    def test_status_400_does_not_retry(self):
        client = build_client(max_retry_attempts=3)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=make_status_error(400, "Bad Request"),
        ):
            with pytest.raises(RouterClientError) as exc_info:
                classify(client)
        assert exc_info.value.payload["attempts"] == 1
        assert exc_info.value.payload["retryable"] is False
        assert exc_info.value.payload["source"] == "router"

    def test_status_401_does_not_retry(self):
        client = build_client(max_retry_attempts=3)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=make_status_error(401, "Unauthorized"),
        ):
            with pytest.raises(RouterClientError) as exc_info:
                classify(client)
        assert exc_info.value.payload["attempts"] == 1
        assert exc_info.value.payload["retryable"] is False


class TestExhaustedRetries:
    def test_exhausted_retries_raises_error(self):
        """3 consecutive timeouts exhaust retries -> RouterClientError."""
        client = build_client(max_retry_attempts=3)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[make_timeout_error(), make_timeout_error(), make_timeout_error()],
        ):
            with pytest.raises(RouterClientError) as exc_info:
                classify(client)
        payload = exc_info.value.payload
        assert payload["attempts"] == 3
        assert payload["retryable"] is True
        assert payload["source"] == "router"
        assert payload["type"] == "timeout"
        assert payload["request_timeout_seconds"] == client.request_timeout_seconds
        assert payload["sdk_max_retries"] == 0
        assert payload["max_retry_attempts"] == 3
        assert payload["retry_max_delay_seconds"] == client.retry_max_delay_seconds
        assert payload["total_elapsed_ms"] >= 0


# ---------------------------------------------------------------------------
# Response parsing (the router-specific surface)
# ---------------------------------------------------------------------------


class TestInvalidResponses:
    def test_non_json_body_is_terminal(self):
        """A non-JSON completion fails terminally and is NOT retried."""
        client = build_client(max_retry_attempts=3)
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=make_response(content="not json at all"),
        ) as mock_create:
            with pytest.raises(RouterClientError) as exc_info:
                classify(client)
        assert exc_info.value.payload["type"] == "invalid_response"
        assert exc_info.value.payload["retryable"] is False
        assert exc_info.value.payload["content_length"] == len("not json at all")
        assert mock_create.call_count == 1  # no retry on a bad body

    def test_schema_violation_is_terminal(self):
        """JSON that violates the decision schema (unknown field) is terminal."""
        client = build_client(max_retry_attempts=3)
        bad = '{"domains": ["todoist"], "confidence": 0.9}'
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=make_response(content=bad),
        ) as mock_create:
            with pytest.raises(RouterClientError) as exc_info:
                classify(client)
        assert exc_info.value.payload["type"] == "invalid_response"
        assert exc_info.value.payload["validation_error_count"] > 0
        assert mock_create.call_count == 1

    def test_missing_complexity_is_terminal(self):
        client = build_client(max_retry_attempts=3)
        missing_complexity = (
            '{"outcome": "routed", "domains": ["todoist"], "uncertain": false, '
            '"candidate_domains": []}'
        )
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=make_response(content=missing_complexity),
        ) as mock_create:
            with pytest.raises(RouterClientError) as exc_info:
                classify(client)
        assert exc_info.value.payload["type"] == "invalid_response"
        assert exc_info.value.payload["validation_error_count"] > 0
        assert mock_create.call_count == 1

    def test_empty_content_is_terminal(self):
        client = build_client(max_retry_attempts=3)
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=make_response(content=""),
        ):
            with pytest.raises(RouterClientError) as exc_info:
                classify(client)
        assert exc_info.value.payload["type"] == "invalid_response"
        assert exc_info.value.payload["content_length"] == 0


class TestErrorPayloadStructure:
    def test_transport_error_payload_keys(self):
        client = build_client(max_retry_attempts=1)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=make_timeout_error(),
        ):
            with pytest.raises(RouterClientError) as exc_info:
                classify(client)
        payload = exc_info.value.payload
        for key in (
            "source",
            "type",
            "retryable",
            "attempts",
            "message",
            "error_message",
            "exception_type",
            "exception_module",
            "provider_request_id",
            "timeout_kind",
            "base_url",
            "request_timeout_seconds",
            "sdk_max_retries",
            "max_retry_attempts",
            "retry_max_delay_seconds",
            "total_elapsed_ms",
        ):
            assert key in payload
        assert payload["source"] == "router"
        assert payload["type"] == "timeout"
        assert payload["retryable"] is True
        assert payload["attempts"] == 1
        assert payload["error_message"] == payload["message"]
        assert payload["exception_type"] == "APITimeoutError"
        assert payload["exception_module"] == "openai"
        assert payload["base_url"] == client.base_url
        assert payload["request_timeout_seconds"] == client.request_timeout_seconds
        assert payload["sdk_max_retries"] == 0

    def test_timeout_kind_identifies_connect_timeout_cause(self):
        client = build_client(max_retry_attempts=1)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=make_timeout_error(ConnectTimeout("connect timed out")),
        ):
            with pytest.raises(RouterClientError) as exc_info:
                classify(client)
        assert exc_info.value.payload["timeout_kind"] == "connect"

    def test_status_error_payload_includes_provider_request_id(self):
        client = build_client(max_retry_attempts=1)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=make_status_error(
                500,
                "Internal Server Error",
                headers={"x-request-id": "req_router_123"},
            ),
        ):
            with pytest.raises(RouterClientError) as exc_info:
                classify(client)
        payload = exc_info.value.payload
        assert payload["status_code"] == 500
        assert payload["provider_request_id"] == "req_router_123"


# ---------------------------------------------------------------------------
# Successful decisions + usage + request shape
# ---------------------------------------------------------------------------


class TestSuccessfulDecision:
    def test_returns_router_decision(self):
        client = build_client()
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=make_response(
                content='{"outcome": "routed", "domains": ["todoist", "google_calendar"], '
                '"uncertain": false, "candidate_domains": [], "complexity": "low"}'
            ),
        ):
            decision = classify(client)
        assert isinstance(decision, RouterDecision)
        assert decision.domains == ["todoist", "google_calendar"]
        assert decision.outcome == "routed"

    def test_legacy_reasoning_is_discarded(self):
        client = build_client()
        legacy = (
            '{"outcome": "routed", "domains": ["todoist"], '
            '"uncertain": false, "candidate_domains": [], "complexity": "low", '
            '"reasoning": "legacy explanation"}'
        )
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=make_response(content=legacy),
        ):
            decision = classify(client)

        assert "reasoning" not in decision.model_dump()

    def test_empty_domains_is_valid(self):
        """A greeting routes to no domain -> empty list, not an error."""
        client = build_client()
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=make_response(content='{"outcome": "conversation", "domains": [], "uncertain": false, "candidate_domains": [], "complexity": "low"}'),
        ):
            decision = classify(client)
        assert decision.domains == []

    def test_reasoning_off_disables_thinking_and_forces_json(self):
        """DeepSeek defaults thinking on, so the classifier disables it explicitly."""
        client = build_client()
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=make_response(),
        ) as mock_create:
            classify(client)
        kwargs = mock_create.call_args.kwargs
        assert kwargs["response_format"] == {"type": "json_object"}
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        assert "reasoning_effort" not in kwargs
        assert "temperature" not in kwargs

    def test_reasoning_enabled_enables_thinking(self):
        client = RouterClient(
            api_key="test-key",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com/v1",
            reasoning_effort="high",
            retry_sleep=NO_SLEEP,
        )
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=make_response(),
        ) as mock_create:
            classify(client)
        kwargs = mock_create.call_args.kwargs
        assert kwargs["reasoning_effort"] == "high"
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}

    def test_openai_uses_provider_safe_request_and_usage_ledger(self, monkeypatch):
        profile = OpenAIChatProfile(
            api_key="openai-test-key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6-luna",
            max_output_tokens=400,
            request_timeout_seconds=5.0,
            max_retry_attempts=2,
            retry_max_delay_seconds=2.0,
            sdk_max_retries=0,
        )
        sdk_client = MagicMock()
        response = make_response()
        response.model = "gpt-5.6-luna"
        response.usage.prompt_tokens_details = SimpleNamespace(cached_tokens=3)
        sdk_client.chat.completions.create.return_value = response
        monkeypatch.setattr(
            router_client_module,
            "settings",
            SimpleNamespace(llm_safety_identifier_secret="safety-secret"),
        )
        client = RouterClient(profile=profile, client=sdk_client)
        ledger = UsageLedger()

        decision = client.classify("add milk", SNAPSHOT, usage_accumulator=ledger)

        assert decision.domains == ["todoist"]
        kwargs = sdk_client.chat.completions.create.call_args.kwargs
        assert kwargs["model"] == "gpt-5.6-luna"
        assert kwargs["max_completion_tokens"] == 400
        assert kwargs["reasoning_effort"] == "none"
        assert len(kwargs["safety_identifier"]) == 64
        assert "max_tokens" not in kwargs
        assert "extra_body" not in kwargs
        assert "temperature" not in kwargs
        assert len(ledger.calls) == 1
        assert ledger.calls[0].provider.value == "openai"
        assert ledger.calls[0].cached_read_tokens == 3

    def test_request_trace_includes_router_budget_fields(self):
        tracer = RecordingTracer()
        client = RouterClient(
            api_key="test-key",
            tracer=tracer,
            retry_sleep=NO_SLEEP,
        )
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=make_response(),
        ):
            classify(client)
        request_event = next(event for event in tracer.events if event["stage"] == "router.request")
        fields = request_event["fields"]
        assert fields["request_timeout_seconds"] == client.request_timeout_seconds
        assert fields["sdk_max_retries"] == 0
        assert fields["max_retry_attempts"] == client.max_retry_attempts
        assert fields["retry_max_delay_seconds"] == client.retry_max_delay_seconds
        assert fields["base_url"] == client.base_url
        assert fields["response_format"] == "json_object"
        assert fields["thinking_enabled"] is False

        response_event = next(
            event for event in tracer.events if event["stage"] == "router.response"
        )
        assert response_event["fields"]["complexity"] == "low"

    def test_logs_full_router_prompts(self):
        tracer = RecordingTracer()
        client = RouterClient(
            api_key="test-key",
            tracer=tracer,
            retry_sleep=NO_SLEEP,
        )
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=make_response(),
        ):
            classify(client)
        system_payload = next(
            payload
            for payload in tracer.payloads
            if payload["stage"] == "router.prompt" and payload["label"] == "system_prompt"
        )
        user_payload = next(
            payload
            for payload in tracer.payloads
            if payload["stage"] == "router.prompt" and payload["label"] == "user_prompt"
        )
        assert "You are a fast query router" in system_payload["value"]
        assert "## Domains" in system_payload["value"]
        assert system_payload["limit"] > len(system_payload["value"])
        assert user_payload["value"] == "User request:\nadd buy milk"

    def test_openai_client_receives_configured_timeout_and_disables_sdk_retries(self):
        with patch("agents.agent_api.app.router.client.wrap_openai", side_effect=lambda c, **_: c):
            with patch("agents.agent_api.app.router.client.OpenAI") as openai_cls:
                RouterClient(api_key="test-key", request_timeout_seconds=5.0)
        assert openai_cls.call_args.kwargs["timeout"] == 5.0
        assert openai_cls.call_args.kwargs["max_retries"] == 0


class TestUsageAccumulation:
    def test_usage_accumulates_only_in_explicit_per_run_context(self):
        client = build_client()
        first_run = UsageSummary()
        second_run = UsageSummary()
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[
                make_response(prompt_tokens=12, completion_tokens=6),
                make_response(prompt_tokens=20, completion_tokens=8),
            ],
        ):
            client.classify(
                "add buy milk", SNAPSHOT, usage_accumulator=first_run
            )
            client.classify(
                "add buy milk", SNAPSHOT, usage_accumulator=second_run
            )
        assert first_run.prompt_tokens == 12
        assert first_run.completion_tokens == 6
        assert second_run.prompt_tokens == 20
        assert second_run.completion_tokens == 8
        assert client.usage.total_tokens == 0

    def test_direct_calls_preserve_per_instance_usage_compatibility(self):
        client = build_client()
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[
                make_response(prompt_tokens=12, completion_tokens=6),
                make_response(prompt_tokens=20, completion_tokens=8),
            ],
        ):
            classify(client)
            classify(client)

        assert client.usage.prompt_tokens == 32
        assert client.usage.completion_tokens == 14
        assert client.usage.total_tokens == 46


class TestSharedRouterTransports:
    def test_default_wrappers_reuse_one_sync_sdk_client(self, monkeypatch):
        monkeypatch.setattr(router_client_module, "ROUTER_API_KEY", "test-key")
        sdk_client = MagicMock()
        with patch.object(router_client_module, "OpenAI", return_value=sdk_client) as openai_cls, patch.object(
            router_client_module, "wrap_openai", side_effect=lambda value, **_: value
        ):
            first = get_shared_router_client()
            second = get_shared_router_client()
            direct = RouterClient()

        assert first is second
        assert first.client is sdk_client
        assert direct.client is sdk_client
        assert openai_cls.call_count == 1

    def test_async_sdk_client_is_reused(self, monkeypatch):
        monkeypatch.setattr(router_client_module, "ROUTER_API_KEY", "test-key")
        sdk_client = MagicMock()
        sdk_client.close = AsyncMock()
        with patch.object(
            router_client_module, "AsyncOpenAI", return_value=sdk_client
        ) as openai_cls, patch.object(
            router_client_module, "wrap_openai", side_effect=lambda value, **_: value
        ):
            first = get_shared_async_router_openai_client()
            second = get_shared_async_router_openai_client()

        assert first is second
        assert openai_cls.call_count == 1

    def test_async_classify_preserves_sync_contract_and_usage(self):
        client = build_client()
        async_sdk = MagicMock()
        async_sdk.chat.completions.create = AsyncMock(
            return_value=make_response(prompt_tokens=7, completion_tokens=3)
        )
        tracer = RecordingTracer()
        usage = UsageSummary()

        decision = asyncio.run(
            client.async_classify(
                "add buy milk",
                SNAPSHOT,
                tracer=tracer,
                usage_accumulator=usage,
                async_client=async_sdk,
            )
        )

        assert isinstance(decision, RouterDecision)
        assert decision.outcome.value == "routed"
        assert usage.prompt_tokens == 7
        assert usage.completion_tokens == 3
        assert any(
            event["fields"].get("async_request") is True
            for event in tracer.events
            if event["stage"] == "router.response"
        )


class TestConcurrentRequestIsolation:
    def test_tracer_model_reasoning_and_usage_do_not_leak_between_calls(self):
        client = build_client()
        barrier = threading.Barrier(2)
        seen_kwargs = []
        seen_lock = threading.Lock()

        def create(**kwargs):
            with seen_lock:
                seen_kwargs.append(kwargs)
            barrier.wait(timeout=5)
            prompt_tokens = 11 if kwargs["model"] == "deepseek-router-fast" else 29
            return make_response(
                content=VALID_DECISION_JSON,
                prompt_tokens=prompt_tokens,
                completion_tokens=1,
            )

        first_tracer = RecordingTracer()
        second_tracer = RecordingTracer()
        first_usage = UsageSummary()
        second_usage = UsageSummary()

        def run(query, tracer, model, reasoning, usage):
            return client.classify(
                query,
                SNAPSHOT,
                tracer=tracer,
                model=model,
                reasoning_effort=reasoning,
                usage_accumulator=usage,
            )

        with patch.object(client.client.chat.completions, "create", side_effect=create):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(
                    run,
                    "first request",
                    first_tracer,
                    "deepseek-router-fast",
                    "off",
                    first_usage,
                )
                second_future = pool.submit(
                    run,
                    "second request",
                    second_tracer,
                    "deepseek-router-careful",
                    "high",
                    second_usage,
                )
                assert first_future.result(timeout=5).outcome.value == "routed"
                assert second_future.result(timeout=5).outcome.value == "routed"

        first_request = next(
            event for event in first_tracer.events if event["stage"] == "router.request"
        )
        second_request = next(
            event for event in second_tracer.events if event["stage"] == "router.request"
        )
        assert first_request["fields"]["model"] == "deepseek-router-fast"
        assert first_request["fields"]["reasoning"] == "off"
        assert second_request["fields"]["model"] == "deepseek-router-careful"
        assert second_request["fields"]["reasoning"] == "high"
        assert first_usage.prompt_tokens == 11
        assert second_usage.prompt_tokens == 29
        assert {kwargs["model"] for kwargs in seen_kwargs} == {
            "deepseek-router-fast",
            "deepseek-router-careful",
        }
        assert client.model == "deepseek-v4-flash"
        assert client.reasoning_effort == "off"
        assert any(
            payload["value"] == "User request:\nfirst request"
            for payload in first_tracer.payloads
            if payload["label"] == "user_prompt"
        )
        assert any(
            payload["value"] == "User request:\nsecond request"
            for payload in second_tracer.payloads
            if payload["label"] == "user_prompt"
        )


def test_wrap_openai_receives_domain_router_span_names():
    """wrap_openai is called with domain_router.classify.<provider> span names."""
    spy = MagicMock(side_effect=lambda c, **_: c)
    with (
        patch("agents.agent_api.app.router.client.wrap_openai", spy),
        patch("agents.agent_api.app.router.client.OpenAI", return_value=MagicMock()),
    ):
        RouterClient(api_key="test-key")
    assert spy.called
    call_kwargs = spy.call_args[1]
    assert call_kwargs["chat_name"].startswith("domain_router.classify.")
    assert call_kwargs["completions_name"].startswith("domain_router.classify.")
    assert call_kwargs["chat_name"] == call_kwargs["completions_name"]
