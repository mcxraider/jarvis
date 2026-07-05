"""Unit tests for DeepSeekAgentClient retry/backoff logic."""

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

from httpx import Request, Response as HttpxResponse
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

# Patch wrap_openai before importing the client so it does not decorate the
# OpenAI client with actual LangSmith instrumentation during tests.
with patch("langsmith.wrappers.wrap_openai", side_effect=lambda c: c):
    from agents.agent_api.app.graph.nodes.orchestrator import (
        DeepSeekAgentClient,
        DeepSeekAgentClientError,
        LLM_FAILURE_MESSAGE,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NO_SLEEP = lambda _: None  # noqa: E731 — skip real delays in retry loops


def make_response(
    content="Hello",
    tool_calls=None,
    prompt_tokens=10,
    completion_tokens=5,
    cached_tokens=0,
):
    """Build a mock OpenAI-compatible chat completion response."""
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls
    message.role = "assistant"
    message.model_dump.return_value = {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls,
    }

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


def make_timeout_error():
    """Create an APITimeoutError with a valid request arg."""
    return APITimeoutError(
        request=Request("POST", "https://api.deepseek.com/v1/chat/completions")
    )


def make_connection_error():
    """Create an APIConnectionError."""
    return APIConnectionError(
        request=Request("POST", "https://api.deepseek.com/v1/chat/completions"),
        message="Connection refused",
    )


def make_rate_limit_error():
    """Create a RateLimitError (status 429)."""
    request = Request("POST", "https://api.deepseek.com/v1/chat/completions")
    response = HttpxResponse(429, json={"error": {"message": "rate limited"}}, request=request)
    return RateLimitError(
        message="Rate limit exceeded",
        response=response,
        body={"error": {"message": "rate limited"}},
    )


def make_status_error(status_code: int, message: str = "Error"):
    """Create an APIStatusError with the given status code."""
    request = Request("POST", "https://api.deepseek.com/v1/chat/completions")
    response = HttpxResponse(status_code, json={"error": {"message": message}}, request=request)
    return APIStatusError(
        message=message,
        response=response,
        body={"error": {"message": message}},
    )


def build_client(max_retry_attempts=3):
    """Build a DeepSeekAgentClient with test defaults, patching wrap_openai."""
    with patch("langsmith.wrappers.wrap_openai", side_effect=lambda c: c):
        client = DeepSeekAgentClient(
            api_key="test-key",
            max_retry_attempts=max_retry_attempts,
            retry_sleep=NO_SLEEP,
        )
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMissingApiKey:
    def test_missing_api_key_raises(self, monkeypatch):
        """No API key provided and env var unset -> RuntimeError."""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with patch("langsmith.wrappers.wrap_openai", side_effect=lambda c: c):
            with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY is required"):
                DeepSeekAgentClient(api_key=None)


class TestRetryableErrors:
    """Retryable errors trigger retry and succeed on second attempt."""

    def test_timeout_error_triggers_retry(self):
        client = build_client(max_retry_attempts=3)
        success_response = make_response(content="ok")
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[make_timeout_error(), success_response],
        ) as mock_create:
            result = client.create_message(messages=[{"role": "user", "content": "hi"}], tools=[])
        assert result["content"] == "ok"
        assert mock_create.call_count == 2

    def test_connection_error_triggers_retry(self):
        client = build_client(max_retry_attempts=3)
        success_response = make_response(content="recovered")
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[make_connection_error(), success_response],
        ) as mock_create:
            result = client.create_message(messages=[{"role": "user", "content": "hi"}], tools=[])
        assert result["content"] == "recovered"
        assert mock_create.call_count == 2

    def test_rate_limit_429_triggers_retry(self):
        client = build_client(max_retry_attempts=3)
        success_response = make_response(content="back online")
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[make_rate_limit_error(), success_response],
        ) as mock_create:
            result = client.create_message(messages=[{"role": "user", "content": "hi"}], tools=[])
        assert result["content"] == "back online"
        assert mock_create.call_count == 2

    def test_status_500_triggers_retry(self):
        client = build_client(max_retry_attempts=3)
        success_response = make_response(content="server recovered")
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[make_status_error(500, "Internal Server Error"), success_response],
        ) as mock_create:
            result = client.create_message(messages=[{"role": "user", "content": "hi"}], tools=[])
        assert result["content"] == "server recovered"
        assert mock_create.call_count == 2


class TestNonRetryableErrors:
    """Non-retryable errors raise DeepSeekAgentClientError immediately."""

    def test_status_400_does_not_retry(self):
        client = build_client(max_retry_attempts=3)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=make_status_error(400, "Bad Request"),
        ):
            with pytest.raises(DeepSeekAgentClientError) as exc_info:
                client.create_message(messages=[{"role": "user", "content": "hi"}], tools=[])
        # Only 1 attempt — no retry for 400
        assert exc_info.value.payload["attempts"] == 1
        assert exc_info.value.payload["retryable"] is False

    def test_status_401_does_not_retry(self):
        client = build_client(max_retry_attempts=3)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=make_status_error(401, "Unauthorized"),
        ):
            with pytest.raises(DeepSeekAgentClientError) as exc_info:
                client.create_message(messages=[{"role": "user", "content": "hi"}], tools=[])
        assert exc_info.value.payload["attempts"] == 1
        assert exc_info.value.payload["retryable"] is False


class TestExhaustedRetries:
    def test_exhausted_retries_raises_error(self):
        """3 consecutive timeouts exhaust retries -> DeepSeekAgentClientError."""
        client = build_client(max_retry_attempts=3)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[make_timeout_error(), make_timeout_error(), make_timeout_error()],
        ):
            with pytest.raises(DeepSeekAgentClientError) as exc_info:
                client.create_message(messages=[{"role": "user", "content": "hi"}], tools=[])
        payload = exc_info.value.payload
        assert payload["attempts"] == 3
        assert payload["retryable"] is True
        assert payload["source"] == "deepseek"


class TestErrorPayloadStructure:
    def test_error_payload_structure(self):
        """Verify the payload contains all expected keys."""
        client = build_client(max_retry_attempts=1)
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=make_timeout_error(),
        ):
            with pytest.raises(DeepSeekAgentClientError) as exc_info:
                client.create_message(messages=[{"role": "user", "content": "hi"}], tools=[])
        payload = exc_info.value.payload
        assert "source" in payload
        assert "type" in payload
        assert "retryable" in payload
        assert "attempts" in payload
        assert "message" in payload
        # source is always deepseek
        assert payload["source"] == "deepseek"
        # type should reflect the error kind
        assert payload["type"] == "timeout"
        assert payload["retryable"] is True
        assert payload["attempts"] == 1


class TestUsageAccumulation:
    def test_usage_accumulates_across_calls(self):
        """Two successful calls -> usage.prompt_tokens is the sum of both."""
        client = build_client(max_retry_attempts=3)
        response_1 = make_response(
            content="first",
            prompt_tokens=10,
            completion_tokens=5,
            cached_tokens=4,
        )
        response_2 = make_response(
            content="second",
            prompt_tokens=20,
            completion_tokens=8,
            cached_tokens=7,
        )
        with patch.object(
            client.client.chat.completions,
            "create",
            side_effect=[response_1, response_2],
        ):
            client.create_message(messages=[{"role": "user", "content": "hi"}], tools=[])
            client.create_message(messages=[{"role": "user", "content": "hi again"}], tools=[])
        assert client.usage.prompt_tokens == 30
        assert client.usage.completion_tokens == 13
        assert client.usage.total_tokens == 43
        assert client.usage.cached_tokens == 11


class TestSuccessfulResponse:
    def test_request_enables_deepseek_thinking_high_effort(self):
        """DeepSeek V4 Flash requests opt into thinking mode at high effort."""
        client = build_client(max_retry_attempts=3)
        response = make_response(content="All done", tool_calls=None)
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=response,
        ) as mock_create:
            client.create_message(messages=[{"role": "user", "content": "hi"}], tools=[])

        assert mock_create.call_args.kwargs["reasoning_effort"] == "high"
        assert mock_create.call_args.kwargs["extra_body"] == {"thinking": {"type": "enabled"}}

    def test_successful_response_returns_message_dict(self):
        """Normal completion returns the message as a dict."""
        client = build_client(max_retry_attempts=3)
        response = make_response(content="All done", tool_calls=None)
        with patch.object(
            client.client.chat.completions,
            "create",
            return_value=response,
        ):
            result = client.create_message(messages=[{"role": "user", "content": "hi"}], tools=[])
        assert isinstance(result, dict)
        assert result["role"] == "assistant"
        assert result["content"] == "All done"
        assert result.get("tool_calls") is None
