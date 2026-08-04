"""OpenAI Responses adapter request, replay, normalization, and live smoke tests."""

import os
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import OpenAI
from openai.types.responses.response_create_params import (
    ResponseCreateParamsNonStreaming,
)

from agents.agent_api.app.llm.messages import canonicalize_messages, serialize_messages
from agents.agent_api.app.llm.provider import (
    LLMProvider,
    LLMProviderError,
    OpenAIResponsesProfile,
)
from agents.agent_api.app.llm.responses import (
    build_responses_call,
    normalize_response,
    serialize_responses_input,
)
from agents.agent_api.app.graph.nodes.orchestrator import (
    LLMAgentClient,
    LLMAgentClientError,
)
from agents.agent_api.app.tracing import UserProgressTracePrinter


def _profile() -> OpenAIResponsesProfile:
    return OpenAIResponsesProfile(
        api_key="openai-key",
        base_url="https://api.openai.com/v1",
        model="gpt-5.6-luna",
        max_output_tokens=16000,
        request_timeout_seconds=60,
        max_retry_attempts=3,
        retry_max_delay_seconds=8,
        sdk_max_retries=0,
        reasoning_effort="medium",
    )


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look up a value",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    }
]


def _tool_response(*, encrypted_content: str | None = "encrypted-reasoning"):
    reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "status": "completed",
    }
    if encrypted_content is not None:
        reasoning["encrypted_content"] = encrypted_content
    return {
        "id": "resp_1",
        "status": "completed",
        "model": "gpt-5.6-luna",
        "output": [
            reasoning,
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "phase": "commentary",
                "content": [
                    {
                        "type": "output_text",
                        "text": "I will check that.",
                        "annotations": [],
                    }
                ],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": '{"id":"one"}',
                "status": "completed",
            },
            {
                "type": "function_call",
                "id": "fc_2",
                "call_id": "call_2",
                "name": "lookup",
                "arguments": '{"id":"two"}',
                "status": "completed",
            },
        ],
        "usage": {
            "input_tokens": 40,
            "input_tokens_details": {
                "cached_tokens": 10,
                "cache_write_tokens": 5,
            },
            "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 12},
            "total_tokens": 60,
        },
    }


def test_installed_sdk_exposes_required_responses_fields():
    required = {
        "input",
        "include",
        "max_output_tokens",
        "model",
        "parallel_tool_calls",
        "reasoning",
        "safety_identifier",
        "store",
        "tool_choice",
        "tools",
    }
    assert required <= ResponseCreateParamsNonStreaming.__annotations__.keys()


def test_builds_medium_stateless_request_with_flat_function_tools():
    call = build_responses_call(
        _profile(),
        messages=[{"role": "system", "content": "help"}, {"role": "user", "content": "x"}],
        tools=TOOLS,
        safety_identifier="a" * 64,
    )
    request = call.as_kwargs()

    assert request["model"] == "gpt-5.6-luna"
    assert request["reasoning"] == {"effort": "medium", "context": "current_turn"}
    assert request["store"] is False
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["parallel_tool_calls"] is True
    assert request["max_output_tokens"] == 16000
    assert request["tool_choice"] == "auto"
    assert request["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Look up a value",
            "parameters": TOOLS[0]["function"]["parameters"],
        }
    ]
    for forbidden in (
        "messages",
        "max_completion_tokens",
        "reasoning_effort",
        "extra_body",
        "previous_response_id",
    ):
        assert forbidden not in request


def test_normalizes_parallel_calls_usage_and_checkpoint_replay():
    result = normalize_response(_tool_response(), _profile())

    assert result.message.content == ""
    assert result.commentary == ("I will check that.",)
    assert [call.id for call in result.message.tool_calls] == ["call_1", "call_2"]
    assert result.finish_reason == "tool_calls"
    assert result.provider_request_id == "resp_1"
    assert result.usage.prompt_tokens == 40
    assert result.usage.cached_read_tokens == 10
    assert result.usage.cache_write_tokens == 5
    assert result.usage.reasoning_tokens == 12

    checkpoint = canonicalize_messages(
        [
            {"role": "user", "content": "both"},
            result.message,
            {"role": "tool", "content": "one", "tool_call_id": "call_1"},
            {"role": "tool", "content": "two", "tool_call_id": "call_2"},
        ]
    ).to_checkpoint()
    restored = canonicalize_messages(checkpoint)
    replay = serialize_responses_input(restored)

    assert [item.get("type") for item in replay[1:]] == [
        "reasoning",
        "message",
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    assert replay[1]["encrypted_content"] == "encrypted-reasoning"
    assert replay[2]["phase"] == "commentary"
    assert [item.get("call_id") for item in replay[-2:]] == ["call_1", "call_2"]
    deepseek_history = serialize_messages(LLMProvider.DEEPSEEK, restored)
    assert "continuation" not in deepseek_history[1]
    assert "reasoning_content" not in deepseek_history[1]


def test_partitions_multiple_commentary_messages_from_final_answer():
    result = normalize_response(
        {
            "id": "resp_mixed",
            "status": "completed",
            "model": "gpt-5.6-luna",
            "output": [
                {
                    "type": "message",
                    "id": "msg_commentary_1",
                    "status": "completed",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Checking the logs.",
                            "annotations": [],
                        }
                    ],
                },
                {
                    "type": "message",
                    "id": "msg_commentary_2",
                    "status": "completed",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "I found the race.",
                            "annotations": [],
                        }
                    ],
                },
                {
                    "type": "message",
                    "id": "msg_final",
                    "status": "completed",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Use a generation key.",
                            "annotations": [],
                        }
                    ],
                },
            ],
        },
        _profile(),
    )

    assert result.commentary == ("Checking the logs.", "I found the race.")
    assert result.message.content == "Use a generation key."
    assert result.message.continuation is None


def test_unphased_tool_message_remains_backward_compatible_commentary():
    response = _tool_response()
    response["output"][1].pop("phase")

    result = normalize_response(response, _profile())

    assert result.commentary == ("I will check that.",)
    assert result.message.content == ""


@pytest.mark.parametrize(
    "phase, message",
    [
        ("analysis", "invalid phase"),
        ("final_answer", "cannot accompany unresolved function calls"),
    ],
)
def test_rejects_invalid_or_contradictory_message_phases(phase, message):
    response = _tool_response()
    response["output"][1]["phase"] = phase

    with pytest.raises(LLMProviderError, match=message):
        normalize_response(response, _profile())


def test_rejects_commentary_only_terminal_response():
    response = _text_response("Still working.")
    response["output"][0]["phase"] = "commentary"

    with pytest.raises(LLMProviderError, match="no content or tools"):
        normalize_response(response, _profile())


def test_old_responses_sidecars_are_reconstructed_outside_current_user_turn():
    first = normalize_response(_tool_response(), _profile()).message
    replay = serialize_responses_input(
        [
            {"role": "user", "content": "old"},
            first,
            {"role": "tool", "content": "one", "tool_call_id": "call_1"},
            {"role": "tool", "content": "two", "tool_call_id": "call_2"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new"},
        ]
    )

    assert not any(item.get("type") == "reasoning" for item in replay)
    assert [item.get("call_id") for item in replay if item.get("type") == "function_call"] == [
        "call_1",
        "call_2",
    ]


def test_hitl_checkpoint_resume_starts_a_new_reasoning_turn():
    assistant = normalize_response(_tool_response(), _profile()).message
    checkpoint = canonicalize_messages(
        [
            {"role": "user", "content": "Ask me which record"},
            assistant,
            {
                "role": "tool",
                "content": '{"success":true,"user_reply":"one"}',
                "tool_call_id": "call_1",
            },
            {
                "role": "tool",
                "content": '{"success":false,"error":"deferred"}',
                "tool_call_id": "call_2",
            },
            {"role": "user", "content": "[Clarification result] one"},
        ]
    ).to_checkpoint()

    replay = serialize_responses_input(canonicalize_messages(checkpoint))

    assert not any(item.get("type") == "reasoning" for item in replay)
    assert replay[-1] == {"role": "user", "content": "[Clarification result] one"}
    assert [item.get("call_id") for item in replay if item.get("type") == "function_call"] == [
        "call_1",
        "call_2",
    ]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda item: item.pop("call_id"), "Malformed OpenAI Responses function call"),
        (lambda item: item.update(arguments="not-json"), "valid JSON"),
        (lambda item: item.update(call_id="call_1"), "Duplicate Responses call_id"),
    ],
)
def test_rejects_missing_malformed_or_duplicate_function_calls(mutate, message):
    response = _tool_response()
    target = response["output"][3]
    mutate(target)

    with pytest.raises(LLMProviderError, match=message):
        normalize_response(response, _profile())


@pytest.mark.parametrize(
    "response, message",
    [
        (
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
            "did not complete",
        ),
        (
            {"status": "completed", "output": [{"type": "web_search_call"}]},
            "Unsupported Responses output item",
        ),
        (_tool_response(encrypted_content=None), "encrypted_content"),
    ],
)
def test_rejects_incomplete_unknown_or_non_replayable_outputs(response, message):
    with pytest.raises(LLMProviderError, match=message):
        normalize_response(response, _profile())


def test_refusal_is_explicit_and_not_a_successful_answer():
    result = normalize_response(
        {
            "id": "resp_refusal",
            "status": "completed",
            "model": "gpt-5.6-luna",
            "output": [
                {
                    "type": "message",
                    "id": "msg_refusal",
                    "status": "completed",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "refusal", "refusal": "Cannot help."}],
                }
            ],
        },
        _profile(),
    )

    assert result.refusal == "Cannot help."
    assert result.message.content == ""
    assert result.commentary == ()


def _text_response(text: str = "OpenAI works."):
    return {
        "id": "resp_text",
        "status": "completed",
        "model": "gpt-5.6-luna",
        "output": [
            {
                "type": "message",
                "id": "msg_text",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7},
    }


def test_orchestrator_sync_dispatches_responses_not_chat():
    sdk = MagicMock()
    sdk.responses.create.return_value = _text_response()
    client = LLMAgentClient(
        profile=_profile(), client=sdk, safety_identifier="b" * 64
    )

    result = client.create_message(
        messages=[{"role": "user", "content": "Say hello"}], tools=[]
    )

    assert result == {"role": "assistant", "content": "OpenAI works."}
    sdk.responses.create.assert_called_once()
    sdk.chat.completions.create.assert_not_called()
    assert sdk.responses.create.call_args.kwargs["reasoning"] == {
        "effort": "medium",
        "context": "current_turn",
    }
    assert client.usage.records[0].provider.value == "openai"


def test_orchestrator_sync_emits_openai_commentary_without_checkpointing_it():
    sdk = MagicMock()
    sdk.responses.create.return_value = {
        "id": "resp_mixed",
        "status": "completed",
        "model": "gpt-5.6-luna",
        "output": [
            {
                "type": "message",
                "id": "msg_commentary",
                "status": "completed",
                "role": "assistant",
                "phase": "commentary",
                "content": [
                    {"type": "output_text", "text": "Checking now.", "annotations": []}
                ],
            },
            {
                "type": "message",
                "id": "msg_final",
                "status": "completed",
                "role": "assistant",
                "phase": "final_answer",
                "content": [
                    {"type": "output_text", "text": "All clear.", "annotations": []}
                ],
            },
        ],
    }
    events = []
    tracer = UserProgressTracePrinter(events.append, enabled=False)
    client = LLMAgentClient(
        profile=_profile(), client=sdk, safety_identifier="e" * 64
    )

    result = client.create_message(
        messages=[{"role": "user", "content": "Check"}],
        tools=[],
        tracer=tracer,
    )

    assert result == {"role": "assistant", "content": "All clear."}
    assert events == [{"narration": "Checking now."}]


def test_invalid_openai_output_emits_no_commentary():
    sdk = MagicMock()
    response = _text_response("Do not show this.")
    response["output"][0]["phase"] = "invalid"
    sdk.responses.create.return_value = response
    events = []
    tracer = UserProgressTracePrinter(events.append, enabled=False)
    client = LLMAgentClient(
        profile=_profile(), client=sdk, safety_identifier="f" * 64
    )

    with pytest.raises(LLMAgentClientError):
        client.create_message(
            messages=[{"role": "user", "content": "Check"}],
            tools=[],
            tracer=tracer,
        )

    assert events == []


def test_orchestrator_tool_loop_replays_checkpointed_output_items():
    sdk = MagicMock()
    sdk.responses.create.side_effect = [_tool_response(), _text_response("Both done.")]
    client = LLMAgentClient(
        profile=_profile(), client=sdk, safety_identifier="d" * 64
    )
    messages = [{"role": "user", "content": "Look up both"}]

    assistant = client.create_message(messages=messages, tools=TOOLS)
    messages.extend(
        [
            assistant,
            {"role": "tool", "content": "one", "tool_call_id": "call_1"},
            {"role": "tool", "content": "two", "tool_call_id": "call_2"},
        ]
    )
    final = client.create_message(messages=messages, tools=TOOLS)

    assert final == {"role": "assistant", "content": "Both done."}
    replay = sdk.responses.create.call_args_list[1].kwargs["input"]
    assert [item.get("type") for item in replay[1:]] == [
        "reasoning",
        "message",
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    assert len(client.usage.records) == 2


def test_orchestrator_async_dispatches_responses_not_chat():
    sync_sdk = MagicMock()
    async_sdk = MagicMock()
    async_sdk.responses.create = AsyncMock(return_value=_text_response("Async works."))
    async_sdk.chat.completions.create = AsyncMock()
    client = LLMAgentClient(
        profile=_profile(),
        client=sync_sdk,
        async_client=async_sdk,
        safety_identifier="c" * 64,
    )

    result = asyncio.run(
        client.async_create_message(
            messages=[{"role": "user", "content": "Say hello"}],
            tools=[],
            async_client=async_sdk,
        )
    )

    assert result == {"role": "assistant", "content": "Async works."}
    async_sdk.responses.create.assert_awaited_once()
    async_sdk.chat.completions.create.assert_not_awaited()


def test_orchestrator_async_emits_openai_commentary_once():
    sync_sdk = MagicMock()
    async_sdk = MagicMock()
    async_sdk.responses.create = AsyncMock(return_value=_tool_response())
    events = []
    tracer = UserProgressTracePrinter(events.append, enabled=False)
    client = LLMAgentClient(
        profile=_profile(),
        client=sync_sdk,
        async_client=async_sdk,
        safety_identifier="1" * 64,
    )

    result = asyncio.run(
        client.async_create_message(
            messages=[{"role": "user", "content": "Look it up"}],
            tools=TOOLS,
            tracer=tracer,
            async_client=async_sdk,
        )
    )

    assert result["content"] == ""
    assert len(result["tool_calls"]) == 2
    assert events == [{"narration": "I will check that."}]


@pytest.mark.skipif(
    os.getenv("JARVIS_LIVE_OPENAI_RESPONSES") != "1",
    reason="set JARVIS_LIVE_OPENAI_RESPONSES=1 to run the paid live smoke",
)
def test_live_luna_medium_function_round_trip():
    api_key = os.environ["OPENAI_API_KEY"]
    profile = replace(_profile(), api_key=api_key)
    client = OpenAI(api_key=api_key, max_retries=0)
    first_call = build_responses_call(
        profile,
        messages=[
            {
                "role": "user",
                "content": "Call lookup with id smoke, then report its returned value.",
            }
        ],
        tools=TOOLS,
        safety_identifier="a" * 64,
    )
    first = normalize_response(client.responses.create(**first_call.as_kwargs()), profile)
    assert first.message.tool_calls
    messages = [
        {"role": "user", "content": "Call lookup with id smoke, then report its returned value."},
        first.message,
        {
            "role": "tool",
            "content": '{"value":"ok"}',
            "tool_call_id": first.message.tool_calls[0].id,
        },
    ]
    second_call = build_responses_call(
        profile,
        messages=messages,
        tools=TOOLS,
        safety_identifier="a" * 64,
    )
    second = normalize_response(client.responses.create(**second_call.as_kwargs()), profile)
    assert "ok" in (second.message.content or "").lower()
    assert (first.usage.reasoning_tokens if first.usage else 0) > 0
