"""Human-in-the-loop (clarification) graph node and its message helpers."""

import json
from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from agents.agent_api.app.constants import USER_ID
from agents.agent_api.app.graph.run_deps import RunDeps, deps_from_config
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.base import parse_tool_call_arguments, tool_call_name
from agents.agent_api.app.tools.control import ASK_USER_TOOL_NAME, is_ask_user_tool_call
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


def build_ask_user_payload(
    state: JarvisState,
    ask_user_call: Dict[str, Any],
    deferred_tool_calls: List[Dict[str, Any]],
    extra_ask_user_calls: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the interrupt payload shown to the human."""

    arguments = parse_tool_call_arguments(ask_user_call)
    question = arguments.get("question") or "What detail should I use before continuing?"
    return {
        "type": "clarify",
        "question": question,
        "reason": arguments.get("reason", ""),
        "missing_fields": arguments.get("missing_fields", []),
        "risk": arguments.get("risk", ""),
        "tool_call_id": ask_user_call.get("id", "missing_tool_call_id"),
        "deferred_tool_calls": [
            {
                "id": item.get("id", "missing_tool_call_id"),
                "name": tool_call_name(item),
            }
            for item in deferred_tool_calls
        ],
        "extra_ask_user_calls": [
            {
                "id": item.get("id", "missing_tool_call_id"),
                "name": tool_call_name(item),
            }
            for item in extra_ask_user_calls
        ],
        "user_id": state.get("user_id", USER_ID),
        "request_source": state.get("request_source", "api"),
        "thread_id": state.get("thread_id", ""),
    }


def ask_user_tool_message(
    tool_call_id: str,
    content: Dict[str, Any],
) -> Dict[str, Any]:
    """Create a tool message that closes an ask_user pseudo-tool call."""

    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": ASK_USER_TOOL_NAME,
        "content": json.dumps(content, default=str),
    }


def deferred_tool_message(tool_call: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Create a synthetic tool message for a call intentionally not executed."""

    tool_name = tool_call_name(tool_call)
    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", "missing_tool_call_id"),
        "name": tool_name,
        "content": json.dumps(
            {
                "tool_call_id": tool_call.get("id", "missing_tool_call_id"),
                "tool_name": tool_name,
                "success": False,
                "content": None,
                "error": reason,
                "deferred_for_clarification": True,
            },
            default=str,
        ),
    }


def create_hitl_node(tracer: Optional[TracePrinter] = None):
    """Create the graph node that pauses for user clarification."""

    _captured = RunDeps(tracer=tracer or NULL_TRACE)

    async def hitl_node(
        state: JarvisState,
        config: RunnableConfig | None = None,
    ) -> JarvisState:
        deps = deps_from_config(config)
        tracer = (
            deps.tracer
            if deps is not None and deps.tracer is not None
            else _captured.tracer
        )
        latest_message = (state.get("messages") or [{}])[-1]
        tool_calls = latest_message.get("tool_calls") or []
        ask_user_calls = [tool_call for tool_call in tool_calls if is_ask_user_tool_call(tool_call)]
        if not ask_user_calls:
            error = "HITL node reached without an ask_user tool call."
            tracer.event("graph.hitl", "Missing ask_user tool call.", error=error)
            return {
                "error": error,
                "final_response": error,
                "next": "end",
            }

        primary_ask_user_call = ask_user_calls[0]
        extra_ask_user_calls = ask_user_calls[1:]
        deferred_tool_calls = [
            tool_call for tool_call in tool_calls if not is_ask_user_tool_call(tool_call)
        ]
        payload = build_ask_user_payload(
            state,
            primary_ask_user_call,
            deferred_tool_calls,
            extra_ask_user_calls,
        )
        tracer.event(
            "graph.hitl",
            "Interrupting for user clarification.",
            question=payload.get("question"),
            deferred_tools=len(deferred_tool_calls),
            extra_questions=len(extra_ask_user_calls),
        )
        tracer.progress({
            "phase": "awaiting_confirmation",
            "action": "waiting",
            "intent": "clarify",
        })

        human_reply = interrupt(payload)
        reply_text = str(human_reply)
        tracer.event("graph.hitl", "Resumed from user clarification.")

        messages = list(state.get("messages", []))
        messages.append(
            ask_user_tool_message(
                primary_ask_user_call.get("id", "missing_tool_call_id"),
                {
                    "success": True,
                    "question": payload.get("question"),
                    "user_reply": reply_text,
                },
            )
        )
        for ask_user_call in extra_ask_user_calls:
            messages.append(
                ask_user_tool_message(
                    ask_user_call.get("id", "missing_tool_call_id"),
                    {
                        "success": False,
                        "error": "Only one clarification question is supported per HITL turn.",
                        "user_reply": reply_text,
                    },
                )
            )
        for tool_call in deferred_tool_calls:
            messages.append(
                deferred_tool_message(
                    tool_call,
                    "Tool call was not executed because Jarvis requested user clarification first.",
                )
            )
        original_prompt = state.get("user_prompt", "")
        messages.append({
            "role": "user",
            "content": (
                f"[Clarification result]\n"
                f"Original request: \"{original_prompt}\"\n"
                f"Question asked by you: \"{payload.get('question')}\"\n"
                f"User response: \"{reply_text}\""
            ),
        })

        clarification_record = {
            "question": payload.get("question"),
            "reply": reply_text,
            "tool_call_id": primary_ask_user_call.get("id", "missing_tool_call_id"),
            "deferred_tool_calls": payload.get("deferred_tool_calls", []),
            "extra_ask_user_calls": payload.get("extra_ask_user_calls", []),
        }

        return {
            "messages": messages,
            "pending_clarification": {},
            "clarification_history": state.get("clarification_history", []) + [clarification_record],
            "interrupted": False,
            "interrupt_payload": {},
            "next": "agent",
        }

    return hitl_node


__all__ = [
    "ask_user_tool_message",
    "build_ask_user_payload",
    "create_hitl_node",
    "deferred_tool_message",
]
