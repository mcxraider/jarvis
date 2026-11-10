"""Table-driven tests for ``build_routing_query`` (domain-router input shapes)."""

from typing import Any, Dict, List, Optional

import pytest

from agents.agent_api.app.graph.prompts.context import (
    ROUTING_CURRENT_REQUEST_HEADER,
    ROUTING_HISTORY_HEADER,
    RoutingQuery,
    build_routing_query,
    build_user_request_context,
)

PROMPT = "make it due tomorrow"
REPLY = {"role": "assistant", "message": "Created task: Buy milk"}
CLARIFICATION = [
    {"question": "Which list?", "reply": "Inbox"},
    {"question": "Which task?", "reply": "Buy milk"},
]
HISTORY = (
    "[Previous thread 1 — most recent]\n"
    '{"role": "user", "content": "cancel my dentist appointment"}'
)
CLARIFIED = f"{PROMPT} [You asked: Which task?] [User replied: Buy milk]"
REPLY_BLOCK = (
    "Reply context:\n"
    "- Replied-to role: assistant\n"
    "- Replied-to message: Created task: Buy milk\n"
    "\n"
)


def _old_routing_query(
    user_prompt: str,
    reply_context: Optional[dict],
    clarification_history: List[Dict[str, Any]],
) -> str:
    """The pre-refactor inline orchestrator assembly (no history)."""

    if clarification_history:
        last = clarification_history[-1]
        return (
            f"{user_prompt} "
            f"[You asked: {last.get('question') or ''}] "
            f"[User replied: {last.get('reply') or ''}]"
        )
    if reply_context:
        return build_user_request_context(user_prompt, reply_context=reply_context)
    return user_prompt


CASES = [
    pytest.param(dict(), PROMPT, PROMPT, id="fresh"),
    pytest.param(
        dict(reply_context=REPLY),
        build_user_request_context(PROMPT, reply_context=REPLY),
        PROMPT,
        id="fresh+reply",
    ),
    pytest.param(
        dict(clarification_history=CLARIFICATION), CLARIFIED, "Buy milk",
        id="clarification",
    ),
    pytest.param(
        dict(clarification_history=CLARIFICATION, reply_context=REPLY),
        REPLY_BLOCK + "Current user message:\n" + CLARIFIED,
        "Buy milk",
        id="clarification+reply",
    ),
    pytest.param(
        dict(history_text=HISTORY),
        ROUTING_HISTORY_HEADER + HISTORY + ROUTING_CURRENT_REQUEST_HEADER
        + build_user_request_context(PROMPT),
        PROMPT,
        id="history+fresh",
    ),
    pytest.param(
        dict(history_text=HISTORY, reply_context=REPLY),
        ROUTING_HISTORY_HEADER + HISTORY + ROUTING_CURRENT_REQUEST_HEADER
        + build_user_request_context(PROMPT, reply_context=REPLY),
        PROMPT,
        id="history+fresh+reply",
    ),
    pytest.param(
        dict(history_text=HISTORY, clarification_history=CLARIFICATION),
        ROUTING_HISTORY_HEADER + HISTORY + ROUTING_CURRENT_REQUEST_HEADER
        + "Current user message:\n" + CLARIFIED,
        "Buy milk",
        id="history+clarification",
    ),
    pytest.param(
        dict(
            history_text=HISTORY,
            clarification_history=CLARIFICATION,
            reply_context=REPLY,
        ),
        ROUTING_HISTORY_HEADER + HISTORY + ROUTING_CURRENT_REQUEST_HEADER
        + REPLY_BLOCK + "Current user message:\n" + CLARIFIED,
        "Buy milk",
        id="history+clarification+reply",
    ),
]


@pytest.mark.parametrize("kwargs, expected_text, expected_latest", CASES)
def test_build_routing_query_shapes(kwargs, expected_text, expected_latest):
    result = build_routing_query(PROMPT, **kwargs)
    assert isinstance(result, RoutingQuery)
    assert result.text == expected_text
    assert result.latest_user_text == expected_latest


@pytest.mark.parametrize(
    "reply_context, clarification_history",
    [
        pytest.param(None, [], id="fresh"),
        pytest.param(REPLY, [], id="fresh+reply"),
        pytest.param(None, CLARIFICATION, id="clarification"),
    ],
)
def test_byte_identical_with_previous_inline_assembly(
    reply_context, clarification_history
):
    result = build_routing_query(
        PROMPT,
        reply_context=reply_context,
        clarification_history=clarification_history,
    )
    assert result.text == _old_routing_query(
        PROMPT, reply_context, clarification_history
    )


def test_history_clarification_reply_ordering():
    text = build_routing_query(
        PROMPT,
        reply_context=REPLY,
        clarification_history=CLARIFICATION,
        history_text=HISTORY,
    ).text
    positions = [
        text.index(ROUTING_HISTORY_HEADER),
        text.index(HISTORY),
        text.index(ROUTING_CURRENT_REQUEST_HEADER),
        text.index("Reply context:"),
        text.index("Current user message:"),
        text.index("[User replied: Buy milk]"),
    ]
    assert positions == sorted(positions)
    assert text.count("Current user message:") == 1


def test_missing_question_or_reply_renders_empty():
    result = build_routing_query(PROMPT, clarification_history=[{}])
    assert result.text == f"{PROMPT} [You asked: ] [User replied: ]"
    assert result.latest_user_text == ""
