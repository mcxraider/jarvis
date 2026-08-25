"""Canonical checkpoint conversion and provider serialization tests."""

import copy

import pytest

from agents.agent_api.app.llm.messages import (
    CanonicalAssistantMessage,
    CanonicalMessageBatch,
    CanonicalToolCall,
    DeepSeekContinuation,
    OpenAIResponsesContinuation,
    canonicalize_messages,
    serialize_messages,
)
from agents.agent_api.app.llm.provider import LLMProvider, LLMProviderError


TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "lookup", "arguments": '{"id":"one"}'},
}


def test_deepseek_continuation_discarded_on_checkpoint_load():
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

    # DeepSeek continuation is discarded on load (legacy bridge)
    assert restored.messages[0].continuation is None
    assert restored.messages[0].content == "I will look that up."
    assert restored.messages[0].tool_calls == batch.messages[0].tool_calls


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

    # DeepSeek reasoning_content is discarded during canonicalization (legacy bridge)
    deepseek = serialize_messages(LLMProvider.DEEPSEEK, legacy)[0]
    openai = serialize_messages(LLMProvider.OPENAI, legacy)[0]

    assert set(deepseek) == {"role", "content", "tool_calls"}
    assert set(openai) == {"role", "content", "tool_calls"}


def test_replayed_reasoning_item_keeps_summary_key_required_by_the_api():
    """`summary` is Required[] on ResponseReasoningItemParam.

    Dropping the key makes OpenAI reject every post-tool-call turn with a 400,
    so it must survive replay — emptied, not deleted, since the summary text
    itself is display-only and must not be sent back.
    """
    from openai.types.responses.response_reasoning_item_param import (
        ResponseReasoningItemParam,
    )

    continuation = OpenAIResponsesContinuation.from_items(
        [
            {
                "type": "reasoning",
                "id": "rs_1",
                "encrypted_content": "encrypted-reasoning",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": "Checking your tasks."}],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": '{"id":"one"}',
                "status": "completed",
            },
        ]
    )
    reasoning = continuation.output_items()[0]

    assert "summary" in ResponseReasoningItemParam.__annotations__
    assert reasoning["summary"] == []
    assert reasoning["encrypted_content"] == "encrypted-reasoning"
    assert "Checking your tasks." not in repr(reasoning)


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
