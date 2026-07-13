"""Unit tests for RouterClient retry/backoff, tracing, and response parsing.

Mirrors ``test_deepseek_client.py``: the router reuses the same OpenAI-compatible
retry scaffolding, so the transport-error tests are structurally identical. The
extra surface is response parsing — a malformed or schema-invalid body must fail
terminally (non-retryable) so the caller falls back to the static selector.
"""

import os
from unittest.mock import MagicMock, patch

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
with patch("langsmith.wrappers.wrap_openai", side_effect=lambda c: c):
    from agents.agent_api.app.router.client import RouterClient, RouterClientError
    from agents.agent_api.app.router.prompt import RouterDecision

from tests.agents.runtime_helpers import make_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NO_SLEEP = lambda _: None  # noqa: E731 — skip real delays in retry loops

VALID_DECISION_JSON = '{"outcome": "routed", "domains": ["todoist"], "uncertain": false, "candidate_domains": [], "reasoning": "task"}'

SNAPSHOT = make_snapshot()


def make_response(content=VALID_DECISION_JSON, prompt_tokens=12, completion_tokens=6, cached_tokens=0):
    """Build a mock OpenAI-compatible chat completion whose message.content is JSON."""
    message = MagicMock()
    message.content = content

    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = prompt_tokens + completion_tokens
    usage.prompt_cache_hit_tokens = cached_tokens
    usage.completion_tokens_details = None

    response = MagicMock()
    response.choices = [MagicMock(message=message)]
    response.usage = usage
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
    """Build a RouterClient with test defaults, patching wrap_openai."""
    with patch("langsmith.wrappers.wrap_openai", side_effect=lambda c: c):
        client = RouterClient(
            api_key="test-key",
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
        with patch("langsmith.wrappers.wrap_openai", side_effect=lambda c: c):
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
                '"uncertain": false, "candidate_domains": [], "reasoning": "spans both"}'
            ),
        ):
            decision = classify(client)
        assert isinstance(decision, RouterDecision)
        assert decision.domains == ["todoist", "google_calendar"]
        assert decision.outcome == "routed"

    def test_empty_domains_is_valid(self):
        """A greeting routes to no domain -> empty list, not an error."""
        client = build_client()
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=make_response(content='{"outcome": "conversation", "domains": [], "uncertain": false, "candidate_domains": [], "reasoning": "greeting"}'),
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
        with patch("agents.agent_api.app.router.client.wrap_openai", side_effect=lambda c: c):
            with patch("agents.agent_api.app.router.client.OpenAI") as openai_cls:
                RouterClient(api_key="test-key", request_timeout_seconds=5.0)
        assert openai_cls.call_args.kwargs["timeout"] == 5.0
        assert openai_cls.call_args.kwargs["max_retries"] == 0


class TestUsageAccumulation:
    def test_usage_accumulates_across_calls(self):
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
