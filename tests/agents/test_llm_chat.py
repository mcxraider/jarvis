"""Provider request, response normalization, safety, and usage tests."""

from types import SimpleNamespace

import pytest
from openai.types.chat.completion_create_params import CompletionCreateParamsNonStreaming

from agents.agent_api.app.llm.chat import (
    UsageLedger,
    build_chat_completion_call,
    derive_safety_identifier,
    normalize_chat_completion,
)
from agents.agent_api.app.llm.provider import (
    DeepSeekProfile,
    LLMProvider,
    LLMProviderError,
    OpenAIChatProfile,
)


def _deepseek(*, role: str):
    return DeepSeekProfile(
        api_key="deepseek-key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        max_output_tokens={"orchestrator": 13000, "router": 400, "summarizer": 15000}[role],
        request_timeout_seconds=30,
        max_retry_attempts=3,
        retry_max_delay_seconds=8,
        sdk_max_retries=0,
        reasoning_effort="max" if role == "orchestrator" else "off",
        thinking_enabled=role == "orchestrator",
    )


def _openai(*, role: str):
    return OpenAIChatProfile(
        api_key="openai-key",
        base_url="https://api.openai.com/v1",
        model="gpt-5.6-luna",
        max_output_tokens={"orchestrator": 16000, "router": 400, "summarizer": 15000}[role],
        request_timeout_seconds=60,
        max_retry_attempts=3,
        retry_max_delay_seconds=8,
        sdk_max_retries=0,
    )


MESSAGES = [{"role": "user", "content": "hello"}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Lookup",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


def test_installed_openai_sdk_exposes_required_baseline_fields():
    required = {
        "model",
        "messages",
        "max_completion_tokens",
        "reasoning_effort",
        "safety_identifier",
        "tools",
        "response_format",
    }
    assert required <= CompletionCreateParamsNonStreaming.__annotations__.keys()


@pytest.mark.parametrize("provider", [LLMProvider.DEEPSEEK, LLMProvider.OPENAI])
@pytest.mark.parametrize("role", ["orchestrator", "router", "summarizer"])
@pytest.mark.parametrize("async_mode", [False, True])
def test_twelve_cell_request_matrix(provider, role, async_mode):
    profile = _deepseek(role=role) if provider is LLMProvider.DEEPSEEK else _openai(role=role)
    kwargs = {
        "messages": MESSAGES,
        "tools": TOOLS if role == "orchestrator" else (),
        "response_format": {"type": "json_object"} if role == "router" else None,
        "tool_choice": "auto" if role == "orchestrator" else None,
        "temperature": 0 if role == "summarizer" else None,
        "include_thinking": role != "summarizer",
    }
    if provider is LLMProvider.OPENAI:
        kwargs["safety_identifier"] = "a" * 64

    call = build_chat_completion_call(profile, **kwargs)
    request = call.as_kwargs()

    assert request["model"] == profile.model
    assert request["timeout"] == profile.request_timeout_seconds
    if provider is LLMProvider.DEEPSEEK:
        assert request["max_tokens"] == profile.max_output_tokens
        assert "max_completion_tokens" not in request
        assert "safety_identifier" not in request
        if role == "orchestrator":
            assert request["reasoning_effort"] == "max"
            assert request["extra_body"] == {"thinking": {"type": "enabled"}}
        elif role == "router":
            assert "reasoning_effort" not in request
            assert request["extra_body"] == {"thinking": {"type": "disabled"}}
        else:
            assert "reasoning_effort" not in request
            assert "extra_body" not in request
            assert request["temperature"] == 0
    else:
        assert request["max_completion_tokens"] == profile.max_output_tokens
        assert request["reasoning_effort"] == "none"
        assert request["safety_identifier"] == "a" * 64
        for forbidden in ("max_tokens", "extra_body", "temperature", "reasoning_content"):
            assert forbidden not in request
    if role == "router":
        assert request["response_format"] == {"type": "json_object"}


def test_safety_identifier_is_stable_namespaced_hmac():
    first = derive_safety_identifier("secret", "telegram-123")
    second = derive_safety_identifier("secret", "telegram-123")
    different = derive_safety_identifier("secret", "telegram-124")

    assert first == second
    assert first != different
    assert len(first) == 64
    assert first != "telegram-123"


def _response(*, content="done", tool_calls=None, finish_reason="stop", usage=True):
    result = {
        "id": "chatcmpl_1",
        "model": "gpt-5.6-luna-2026-08-01",
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": content, "tool_calls": tool_calls},
            }
        ],
    }
    if usage:
        result["usage"] = {
            "prompt_tokens": 20,
            "completion_tokens": 8,
            "prompt_tokens_details": {"cached_tokens": 5},
            "completion_tokens_details": {"reasoning_tokens": 3},
        }
    return result


def test_normalizes_text_dict_and_usage_details():
    result = normalize_chat_completion(_response(), _openai(role="orchestrator"))

    assert result.message.content == "done"
    assert result.finish_reason == "stop"
    assert result.returned_model == "gpt-5.6-luna-2026-08-01"
    assert result.provider_request_id == "chatcmpl_1"
    assert result.usage is not None
    assert result.usage.cached_read_tokens == 5
    assert result.usage.reasoning_tokens == 3
    assert result.usage.request_input_tokens == 20


def test_normalizes_sdk_object_tool_only_parallel_response():
    tool_calls = [
        SimpleNamespace(
            id=f"call_{index}",
            function=SimpleNamespace(name="lookup", arguments=f'{{"id":{index}}}'),
        )
        for index in (1, 2)
    ]
    response = SimpleNamespace(
        _request_id="req_123",
        model="deepseek-v4-flash",
        usage=None,
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    tool_calls=tool_calls,
                    reasoning_content="continuation",
                    refusal=None,
                ),
            )
        ],
    )

    result = normalize_chat_completion(response, _deepseek(role="orchestrator"))

    assert [call.id for call in result.message.tool_calls] == ["call_1", "call_2"]
    assert result.message.continuation.reasoning_content == "continuation"
    assert result.usage is None
    assert result.provider_request_id == "req_123"


def test_refusal_is_explicit_and_not_checkpoint_metadata():
    response = _response(content=None, usage=False)
    response["choices"][0]["message"]["refusal"] = "I cannot help with that."

    result = normalize_chat_completion(response, _openai(role="orchestrator"))

    assert result.refusal == "I cannot help with that."
    assert result.message.content == ""


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_truncation_and_filtering_are_preserved(finish_reason):
    result = normalize_chat_completion(
        _response(content="partial", finish_reason=finish_reason),
        _openai(role="orchestrator"),
    )
    assert result.finish_reason == finish_reason


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_truncation_and_filtering_with_null_content_are_explicit(finish_reason):
    result = normalize_chat_completion(
        _response(content=None, finish_reason=finish_reason),
        _openai(role="orchestrator"),
    )
    assert result.finish_reason == finish_reason
    assert result.message.content == ""


@pytest.mark.parametrize(
    "response",
    [
        {"choices": []},
        {"choices": [{"finish_reason": "stop", "message": {"content": None}}]},
        _response(
            content=None,
            tool_calls=[
                {
                    "id": "call_1",
                    "function": {"name": "lookup", "arguments": "not-json"},
                }
            ],
            finish_reason="tool_calls",
        ),
        _response(
            content=None,
            tool_calls=[
                {"id": "call_1", "function": {"name": "a", "arguments": "{}"}},
                {"id": "call_1", "function": {"name": "b", "arguments": "{}"}},
            ],
            finish_reason="tool_calls",
        ),
    ],
)
def test_malformed_responses_raise_typed_invalid_response(response):
    with pytest.raises(LLMProviderError) as error:
        normalize_chat_completion(response, _openai(role="orchestrator"))
    assert error.value.category == "invalid_response"


def test_deepseek_cache_write_and_ledger_keep_per_call_identity():
    response = _response()
    response["model"] = "deepseek-v4-flash"
    response["usage"] = {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 10,
        "completion_tokens_details": None,
    }
    result = normalize_chat_completion(response, _deepseek(role="orchestrator"))
    ledger = UsageLedger()
    ledger.add(result.usage)
    ledger.add(result.usage)

    assert len(ledger.calls) == 2
    assert ledger.calls[0].cache_write_tokens == 10
    assert ledger.calls[0].cached_read_tokens == 0
    assert ledger.totals()["prompt_tokens"] == 20
