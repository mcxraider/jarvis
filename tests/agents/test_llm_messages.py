"""Canonical checkpoint conversion and provider serialization tests."""

import copy

import pytest

from agents.agent_api.app.llm.messages import (
    CanonicalAssistantMessage,
    CanonicalMessageBatch,
    CanonicalToolCall,
    DeepSeekContinuation,
    canonicalize_messages,
    serialize_messages,
)
from agents.agent_api.app.llm.provider import LLMProvider, LLMProviderError


TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "lookup", "arguments": '{"id":"one"}'},
}


def test_deepseek_continuation_round_trips_and_openai_strips_it():
    batch = CanonicalMessageBatch(
        messages=(
            CanonicalAssistantMessage(
                content="I will look that up.",
                tool_calls=(
                    CanonicalToolCall(
                        id="call_1", name="lookup", arguments='{"id":"one"}'
                    ),
                ),
                continuation=DeepSeekContinuation(reasoning_content="private reasoning"),
            ),
        )
    )

    restored = canonicalize_messages(batch.to_checkpoint())

    assert restored == batch
    assert serialize_messages(LLMProvider.DEEPSEEK, restored)[0][
        "reasoning_content"
    ] == "private reasoning"
    assert "reasoning_content" not in serialize_messages(LLMProvider.OPENAI, restored)[0]


def test_legacy_deepseek_and_openai_output_fields_are_allowlisted():
    legacy = [
        {
            "role": "assistant",
            "content": "Calling a tool",
            "reasoning_content": "continuation",
            "annotations": [{"secret": "drop"}],
            "refusal": "drop",
            "tool_calls": [TOOL_CALL],
        }
    ]

    deepseek = serialize_messages(LLMProvider.DEEPSEEK, legacy)[0]
    openai = serialize_messages(LLMProvider.OPENAI, legacy)[0]

    assert set(deepseek) == {"role", "content", "tool_calls", "reasoning_content"}
    assert set(openai) == {"role", "content", "tool_calls"}


def test_text_only_reasoning_is_output_metadata_not_a_continuation():
    legacy = [
        {
            "role": "assistant",
            "content": "Final answer",
            "reasoning_content": "do not checkpoint",
        }
    ]

    serialized = serialize_messages(LLMProvider.DEEPSEEK, legacy)

    assert serialized == [{"role": "assistant", "content": "Final answer"}]


def test_parallel_tool_calls_keep_stable_order_and_matching_results():
    second = {
        "id": "call_2",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"id":"two"}'},
    }
    legacy = [
        {"role": "user", "content": "both"},
        {"role": "assistant", "content": "Checking both", "tool_calls": [TOOL_CALL, second]},
        {"role": "tool", "content": "one result", "tool_call_id": "call_1"},
        {"role": "tool", "content": "two result", "tool_call_id": "call_2"},
    ]

    serialized = serialize_messages(LLMProvider.OPENAI, legacy)

    assert [call["id"] for call in serialized[1]["tool_calls"]] == ["call_1", "call_2"]
    assert [message["tool_call_id"] for message in serialized[2:]] == ["call_1", "call_2"]


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "assistant", "content": "x", "tool_calls": [{**TOOL_CALL, "id": ""}]}],
        [
            {"role": "assistant", "content": "x", "tool_calls": [TOOL_CALL, TOOL_CALL]},
        ],
        [{"role": "tool", "content": "orphan", "tool_call_id": "unknown"}],
        [
            {"role": "assistant", "content": "x", "tool_calls": [TOOL_CALL]},
            {"role": "tool", "content": "a", "tool_call_id": "call_1"},
            {"role": "tool", "content": "b", "tool_call_id": "call_1"},
        ],
        [{"role": "user", "content": [{"type": "image", "url": "x"}]}],
    ],
)
def test_invalid_tool_correlation_or_content_is_rejected(messages):
    with pytest.raises(LLMProviderError) as error:
        serialize_messages(LLMProvider.OPENAI, messages)
    assert error.value.category == "incompatible_checkpoint"


def test_serialization_does_not_mutate_legacy_checkpoint():
    legacy = [
        {"role": "assistant", "content": "Calling", "tool_calls": [TOOL_CALL]},
        {"role": "tool", "content": "done", "tool_call_id": "call_1"},
    ]
    original = copy.deepcopy(legacy)

    serialize_messages(LLMProvider.OPENAI, legacy)

    assert legacy == original
