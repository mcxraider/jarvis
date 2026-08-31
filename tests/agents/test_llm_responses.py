"""OpenAI Responses adapter request, replay, normalization, and live smoke tests."""

import os
import asyncio
import inspect
import pathlib
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import OpenAI
from openai.types.responses import ResponseReasoningSummaryTextDeltaEvent
from openai.types.responses.response_create_params import (
    ResponseCreateParamsNonStreaming,
)

from agents.agent_api.app.api.schemas import MAX_IMAGE_BYTES, MAX_IMAGE_COUNT
from agents.agent_api.app.llm.streaming import (
    consume_async_response_stream,
    consume_response_stream,
)

from agents.agent_api.app.llm.messages import canonicalize_messages, serialize_messages
from agents.agent_api.app.llm.provider import (
    LLMProvider,
    LLMProviderError,
    OpenAIResponsesProfile,
)
from agents.agent_api.app.llm.responses import (
    ImageContext,
    build_responses_call,
    normalize_response,
    serialize_responses_input,
)
from agents.agent_api.app.graph.nodes.orchestrator import (
    LLMAgentClient,
    LLMAgentClientError,
    _model_trace_inputs,
)
from agents.agent_api.app.tracing import UserProgressTracePrinter
from agents.agent_api.app.graph import builder


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
IMAGES = (
    {
        "image_url": "data:image/jpeg;base64,/9j/2Q==",
        "detail": "auto",
    },
)


class _MockStream:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def __iter__(self):
        return iter([])

    def get_final_response(self):
        return self._response


class _MockAsyncStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    # Async, matching openai's AsyncResponseStream. A sync double here hides a
    # missing `await` in consume_async_response_stream.
    async def get_final_response(self):
        return self._response


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
    assert request["reasoning"] == {
        "effort": "medium",
        "context": "current_turn",
        "summary": "concise",
    }
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


def test_builds_pinned_multimodal_request_on_latest_user_turn():
    call = build_responses_call(
        replace(_profile(), model="gpt-5.6"),
        model="gpt-5.6-mini",
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "Which one?"},
            {"role": "user", "content": "caption"},
        ],
        image_context=ImageContext(images=IMAGES, prior_batches=None),
        vision_model="gpt-5.6-luna",
    )
    request = call.as_kwargs()

    # vision_model wins over both profile.model and the model arg when images are present.
    assert request["model"] == "gpt-5.6-luna"
    assert request["store"] is False
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["input"][0]["content"] == "first"
    assert request["input"][-1]["content"] == [
        {"type": "input_text", "text": "Image 1:"},
        {"type": "input_image", "image_url": IMAGES[0]["image_url"], "detail": "auto"},
        {"type": "input_text", "text": "caption"},
    ]


def test_model_trace_inputs_replace_pixels_with_safe_count():
    processed = _model_trace_inputs(
        {
            "self": object(),
            "messages": [{"role": "user", "content": "caption"}],
            "image_context": ImageContext(images=IMAGES, prior_batches=None),
        }
    )

    assert processed["image_count"] == 1
    assert "data:image" not in repr(processed)


def test_multimodal_batches_are_labeled_globally_with_text_last():
    image_2 = {**IMAGES[0], "image_url": "data:image/jpeg;base64,/9j/2g=="}
    call = build_responses_call(
        _profile(),
        messages=[
            {"role": "user", "content": "original"},
            {"role": "assistant", "content": "Which one?"},
            {"role": "user", "content": "second turn"},
            {"role": "assistant", "content": "More detail?"},
            {"role": "user", "content": "current"},
        ],
        image_context=ImageContext(images=(image_2,), prior_batches=[IMAGES, ()]),
    ).as_kwargs()

    user_items = [item for item in call["input"] if item.get("role") == "user"]
    assert user_items[0]["content"] == [
        {"type": "input_text", "text": "Image 1:"},
        {"type": "input_image", "image_url": IMAGES[0]["image_url"], "detail": "auto"},
        {"type": "input_text", "text": "original"},
    ]
    assert user_items[1]["content"] == "second turn"
    assert user_items[2]["content"] == [
        {"type": "input_text", "text": "Image 2:"},
        {"type": "input_image", "image_url": image_2["image_url"], "detail": "auto"},
        {"type": "input_text", "text": "current"},
    ]
    assert call["model"] == "gpt-5.6-luna"


def test_historical_images_pin_vision_model_for_confirmation_resume():
    call = build_responses_call(
        replace(_profile(), model="gpt-5.6"),
        model="gpt-5.6-mini",
        messages=[{"role": "user", "content": "original"}],
        image_context=ImageContext(images=(), prior_batches=[IMAGES]),
        vision_model="gpt-5.6-luna",
    ).as_kwargs()

    assert call["model"] == "gpt-5.6-luna"
    assert call["input"][0]["content"][-1] == {
        "type": "input_text",
        "text": "original",
    }


def test_image_batch_history_mismatch_falls_back_to_last_message():
    call = build_responses_call(
        _profile(),
        messages=[{"role": "user", "content": "only"}],
        image_context=ImageContext(images=(), prior_batches=[IMAGES, ()]),
    ).as_kwargs()

    user_items = [item for item in call["input"] if item.get("role") == "user"]
    assert len(user_items) == 1
    assert user_items[0]["content"] == [
        {"type": "input_text", "text": "Image 1:"},
        {"type": "input_image", "image_url": IMAGES[0]["image_url"], "detail": "auto"},
        {"type": "input_text", "text": "only"},
    ]


def test_hitl_resume_reconstructs_exact_original_multimodal_prefix():
    original_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "original"},
    ]
    initial = build_responses_call(
        _profile(),
        messages=original_messages,
        image_context=ImageContext(images=IMAGES, prior_batches=None),
    ).as_kwargs()["input"]
    resumed_messages = [
        *original_messages,
        {"role": "assistant", "content": "Which one?"},
        {"role": "user", "content": "the second"},
    ]
    resumed = build_responses_call(
        _profile(),
        messages=resumed_messages,
        image_context=ImageContext(images=(), prior_batches=[IMAGES]),
    ).as_kwargs()["input"]

    assert resumed[:2] == initial
    assert resumed[-1] == {"role": "user", "content": "the second"}
    assert original_messages[1]["content"] == "original"


def test_model_trace_inputs_count_current_and_historical_images():
    processed = _model_trace_inputs(
        {
            "image_context": ImageContext(images=IMAGES, prior_batches=[IMAGES, ()]),
        }
    )

    assert processed == {"image_count": 2}
    assert "data:image" not in repr(processed)


def test_image_run_rejects_non_responses_provider_before_graph(monkeypatch):
    compile_graph = MagicMock()
    monkeypatch.setattr(
        builder, "settings", replace(builder.settings, orchestrator_llm=object())
    )
    monkeypatch.setattr(builder, "get_or_compile_graph", compile_graph)

    with pytest.raises(LLMProviderError, match="OpenAI Responses provider"):
        asyncio.run(
            builder.run_jarvis_async(
                user_prompt="caption",
                images=list(IMAGES),
                checkpointer=object(),
            )
        )

    compile_graph.assert_not_called()


def test_historical_image_run_rejects_non_responses_provider_before_graph(monkeypatch):
    compile_graph = MagicMock()
    monkeypatch.setattr(
        builder, "settings", replace(builder.settings, orchestrator_llm=object())
    )
    monkeypatch.setattr(builder, "get_or_compile_graph", compile_graph)

    with pytest.raises(LLMProviderError, match="OpenAI Responses provider"):
        asyncio.run(
            builder.run_jarvis_async(
                user_prompt="confirmation",
                prior_image_batches=[list(IMAGES)],
                checkpointer=object(),
            )
        )

    compile_graph.assert_not_called()


def test_normalizes_parallel_calls_usage_and_checkpoint_replay():
    result = normalize_response(_tool_response(), _profile())

    assert result.message.content == ""
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
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    assert replay[1]["encrypted_content"] == "encrypted-reasoning"
    # Key required by the API, emptied so no summary text is replayed.
    assert replay[1]["summary"] == []
    assert [item.get("call_id") for item in replay[-2:]] == ["call_1", "call_2"]
    deepseek_history = serialize_messages(LLMProvider.DEEPSEEK, restored)
    assert "continuation" not in deepseek_history[1]
    assert "reasoning_content" not in deepseek_history[1]


def test_discards_commentary_messages_keeps_final_answer():
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

    assert result.message.content == "Use a generation key."
    assert result.message.continuation is None


def test_unphased_tool_preamble_discarded_with_tool_calls():
    response = _tool_response()
    response["output"][1].pop("phase")

    result = normalize_response(response, _profile())

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
    sdk.responses.stream.return_value = _MockStream(_text_response())
    client = LLMAgentClient(
        profile=_profile(), client=sdk, safety_identifier="b" * 64
    )

    result = client.create_message(
        messages=[{"role": "user", "content": "Say hello"}], tools=[]
    )

    assert result == {"role": "assistant", "content": "OpenAI works."}
    sdk.responses.stream.assert_called_once()
    sdk.chat.completions.create.assert_not_called()
    assert sdk.responses.stream.call_args.kwargs["reasoning"] == {
        "effort": "medium",
        "context": "current_turn",
        "summary": "concise",
    }
    assert client.usage.records[0].provider.value == "openai"




def test_orchestrator_tool_loop_replays_checkpointed_output_items():
    sdk = MagicMock()
    sdk.responses.stream.side_effect = [
        _MockStream(_tool_response()),
        _MockStream(_text_response("Both done.")),
    ]
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
    replay = sdk.responses.stream.call_args_list[1].kwargs["input"]
    assert [item.get("type") for item in replay[1:]] == [
        "reasoning",
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    assert len(client.usage.records) == 2


def test_orchestrator_tool_loop_reattaches_images_without_mutating_history():
    sdk = MagicMock()
    sdk.responses.stream.side_effect = [
        _MockStream(_tool_response()),
        _MockStream(_text_response("Seen.")),
    ]
    client = LLMAgentClient(
        profile=replace(_profile(), model="gpt-5.6"),
        client=sdk,
        safety_identifier="d" * 64,
    )
    messages = [{"role": "user", "content": "Read this"}]
    ctx = ImageContext(images=IMAGES, prior_batches=None)

    assistant = client.create_message(messages=messages, tools=TOOLS, image_context=ctx)
    messages.extend(
        [
            assistant,
            {"role": "tool", "content": "one", "tool_call_id": "call_1"},
            {"role": "tool", "content": "two", "tool_call_id": "call_2"},
        ]
    )
    client.create_message(messages=messages, tools=TOOLS, image_context=ctx)

    assert messages[0] == {"role": "user", "content": "Read this"}
    for call in sdk.responses.stream.call_args_list:
        assert call.kwargs["model"] == "gpt-5.6-luna"
        assert call.kwargs["input"][0]["content"][1] == {
            "type": "input_image",
            "image_url": IMAGES[0]["image_url"],
            "detail": "auto",
        }


def test_orchestrator_async_dispatches_responses_not_chat():
    sync_sdk = MagicMock()
    async_sdk = MagicMock()
    async_sdk.responses.stream.return_value = _MockAsyncStream(_text_response("Async works."))
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
    async_sdk.responses.stream.assert_called_once()
    async_sdk.chat.completions.create.assert_not_awaited()


def test_summary_fallback_on_rejection():
    from openai import BadRequestError

    sdk = MagicMock()
    rejection = BadRequestError(
        message="Parameter reasoning.summary is not supported for this model.",
        response=MagicMock(status_code=400),
        body={"message": "Parameter reasoning.summary is not supported for this model."},
    )

    def _stream_side_effect(**kwargs):
        if kwargs.get("reasoning", {}).get("summary"):
            raise rejection
        return _MockStream(_text_response("Fallback works."))

    sdk.responses.stream.side_effect = _stream_side_effect
    client = LLMAgentClient(
        profile=_profile(), client=sdk, safety_identifier="b" * 64
    )

    result = client.create_message(
        messages=[{"role": "user", "content": "Hello"}], tools=[]
    )

    assert result == {"role": "assistant", "content": "Fallback works."}
    assert sdk.responses.stream.call_count == 2
    fallback_kwargs = sdk.responses.stream.call_args_list[1]
    assert "summary" not in fallback_kwargs.kwargs.get("reasoning", {})


# --- async Responses transport ------------------------------------------------
# Every image/stream test above drives the *sync* _MockStream. That blind spot
# hid a missing `await` on AsyncResponseStream.get_final_response, which turned
# every OpenAI Responses turn into "did not complete: unknown". These tests
# mirror the sync coverage onto the async transport.


def _summary_delta(summary_index: int, delta: str, sequence: int):
    return ResponseReasoningSummaryTextDeltaEvent(
        delta=delta,
        item_id="rs_1",
        output_index=0,
        sequence_number=sequence,
        summary_index=summary_index,
        type="response.reasoning_summary_text.delta",
    )


class _EventStream:
    """Sync stream double that yields real SDK events before completing."""

    def __init__(self, response, events):
        self._response = response
        self._events = list(events)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def __iter__(self):
        return iter(self._events)

    def get_final_response(self):
        return self._response


class _AsyncEventStream(_EventStream):
    """Async counterpart. get_final_response is async, matching the real SDK."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def __aiter__(self):
        self._cursor = iter(self._events)
        return self

    async def __anext__(self):
        try:
            return next(self._cursor)
        except StopIteration:
            raise StopAsyncIteration

    async def get_final_response(self):
        return self._response


def test_stream_doubles_match_sdk_sync_async_contract():
    """A sync double for an async SDK method silently swallows missing awaits."""
    from openai.lib.streaming.responses import AsyncResponseStream, ResponseStream

    assert not inspect.iscoroutinefunction(ResponseStream.get_final_response)
    assert inspect.iscoroutinefunction(AsyncResponseStream.get_final_response)
    assert not inspect.iscoroutinefunction(_MockStream.get_final_response)
    assert inspect.iscoroutinefunction(_MockAsyncStream.get_final_response)
    assert not inspect.iscoroutinefunction(_EventStream.get_final_response)
    assert inspect.iscoroutinefunction(_AsyncEventStream.get_final_response)


def test_async_stream_consumption_matches_sync_summaries_and_response():
    deltas = [_summary_delta(0, "Checking ", 1), _summary_delta(0, "your tasks.", 2)]
    sync_emitted: list[str] = []
    async_emitted: list[str] = []

    sync_response = consume_response_stream(
        _EventStream(_text_response("Done."), deltas), on_summary=sync_emitted.append
    )
    async_response = asyncio.run(
        consume_async_response_stream(
            _AsyncEventStream(_text_response("Done."), deltas),
            on_summary=async_emitted.append,
        )
    )

    assert async_response == sync_response == _text_response("Done.")
    assert async_emitted == sync_emitted
    assert async_emitted[-1] == "Checking your tasks."


def test_async_image_run_pins_vision_model_and_attaches_image():
    async_sdk = MagicMock()
    async_sdk.responses.stream.return_value = _MockAsyncStream(_text_response("A cat."))
    async_sdk.chat.completions.create = AsyncMock()
    client = LLMAgentClient(
        profile=replace(_profile(), model="gpt-5.6"),
        client=MagicMock(),
        async_client=async_sdk,
        safety_identifier="e" * 64,
    )

    result = asyncio.run(
        client.async_create_message(
            messages=[{"role": "user", "content": "what is this?"}],
            tools=[],
            async_client=async_sdk,
            image_context=ImageContext(images=IMAGES, prior_batches=None),
        )
    )

    assert result == {"role": "assistant", "content": "A cat."}
    kwargs = async_sdk.responses.stream.call_args.kwargs
    assert kwargs["model"] == "gpt-5.6-luna"
    assert kwargs["input"][-1]["content"] == [
        {"type": "input_text", "text": "Image 1:"},
        {"type": "input_image", "image_url": IMAGES[0]["image_url"], "detail": "auto"},
        {"type": "input_text", "text": "what is this?"},
    ]
    async_sdk.chat.completions.create.assert_not_awaited()


def test_async_tool_loop_reattaches_images_without_mutating_history():
    async_sdk = MagicMock()
    async_sdk.responses.stream.side_effect = [
        _MockAsyncStream(_tool_response()),
        _MockAsyncStream(_text_response("Seen.")),
    ]
    client = LLMAgentClient(
        profile=replace(_profile(), model="gpt-5.6"),
        client=MagicMock(),
        async_client=async_sdk,
        safety_identifier="e" * 64,
    )
    messages = [{"role": "user", "content": "Read this"}]
    ctx = ImageContext(images=IMAGES, prior_batches=None)

    async def _tool_loop():
        assistant = await client.async_create_message(
            messages=messages, tools=TOOLS, async_client=async_sdk, image_context=ctx
        )
        messages.extend(
            [
                assistant,
                {"role": "tool", "content": "one", "tool_call_id": "call_1"},
                {"role": "tool", "content": "two", "tool_call_id": "call_2"},
            ]
        )
        return await client.async_create_message(
            messages=messages, tools=TOOLS, async_client=async_sdk, image_context=ctx
        )

    final = asyncio.run(_tool_loop())

    assert final == {"role": "assistant", "content": "Seen."}
    assert messages[0] == {"role": "user", "content": "Read this"}
    for call in async_sdk.responses.stream.call_args_list:
        assert call.kwargs["model"] == "gpt-5.6-luna"
        assert [part["type"] for part in call.kwargs["input"][0]["content"]] == [
            "input_text",
            "input_image",
            "input_text",
        ]


def test_async_summary_fallback_on_rejection_still_returns_a_response():
    from openai import BadRequestError

    async_sdk = MagicMock()
    rejection = BadRequestError(
        message="Parameter reasoning.summary is not supported for this model.",
        response=MagicMock(status_code=400),
        body={"message": "Parameter reasoning.summary is not supported for this model."},
    )

    def _stream_side_effect(**kwargs):
        if kwargs.get("reasoning", {}).get("summary"):
            raise rejection
        return _MockAsyncStream(_text_response("Fallback works."))

    async_sdk.responses.stream.side_effect = _stream_side_effect
    client = LLMAgentClient(
        profile=_profile(),
        client=MagicMock(),
        async_client=async_sdk,
        safety_identifier="e" * 64,
    )

    result = asyncio.run(
        client.async_create_message(
            messages=[{"role": "user", "content": "Hello"}],
            tools=[],
            async_client=async_sdk,
        )
    )

    assert result == {"role": "assistant", "content": "Fallback works."}
    assert async_sdk.responses.stream.call_count == 2
    assert "summary" not in async_sdk.responses.stream.call_args_list[1].kwargs.get(
        "reasoning", {}
    )


def test_async_image_failure_reports_provider_error_not_unknown():
    """A genuine truncation must not be reported as the generic 'unknown'."""
    truncated = {
        "id": "resp_1",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "model": "gpt-5.6-luna",
        "output": [],
    }
    async_sdk = MagicMock()
    async_sdk.responses.stream.return_value = _MockAsyncStream(truncated)
    client = LLMAgentClient(
        profile=_profile(),
        client=MagicMock(),
        async_client=async_sdk,
        safety_identifier="e" * 64,
    )

    with pytest.raises(LLMAgentClientError) as excinfo:
        asyncio.run(
            client.async_create_message(
                messages=[{"role": "user", "content": "describe"}],
                tools=[],
                async_client=async_sdk,
                image_context=ImageContext(images=IMAGES, prior_batches=None),
            )
        )

    assert "max_output_tokens" in excinfo.value.payload["message"]
    assert "unknown" not in excinfo.value.payload["message"]


def test_api_and_telegram_agree_on_image_count_and_size_limits():
    """A TS-side accept that the Python schema rejects is a 422 the user sees."""
    ts_types = (
        pathlib.Path(__file__).resolve().parents[2] / "src" / "types" / "agent.types.ts"
    ).read_text()

    assert f"MAX_AGENT_IMAGE_COUNT = {MAX_IMAGE_COUNT};" in ts_types
    assert "MAX_AGENT_IMAGE_BYTES = 10 * 1024 * 1024;" in ts_types
    assert MAX_IMAGE_BYTES == 10 * 1024 * 1024


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
