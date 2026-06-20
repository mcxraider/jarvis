"""Orchestrator (agent) graph node and the DeepSeek LLM client."""

import copy
import os
from typing import Any, Dict, List, Optional

from langsmith import traceable
from langsmith.wrappers import wrap_openai
from openai import OpenAI

from agents.agent_api.app.constants import DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.todoist.schemas import get_todoist_tools
from agents.agent_api.app.tools.todoist.tools import is_ask_user_tool_call
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


def raw_message_from_openai(message: Any) -> Dict[str, Any]:
    """Convert an OpenAI SDK message object into a raw dict without extras loss."""

    # DeepSeek can include provider-specific fields such as reasoning_content.
    # Keeping the raw shape prevents later tool turns from losing that metadata.
    if isinstance(message, dict):
        return copy.deepcopy(message)

    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)

    if hasattr(message, "to_dict"):
        return message.to_dict()

    raise TypeError(f"Unsupported message type: {type(message)!r}")


class DeepSeekAgentClient:
    """Small wrapper around DeepSeek's OpenAI-compatible chat API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEEPSEEK_MODEL,
        base_url: str = DEEPSEEK_BASE_URL,
        tracer: Optional[TracePrinter] = None,
    ):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.model = model
        self.tracer = tracer or NULL_TRACE
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required to run Jarvis.")
        self.client = wrap_openai(OpenAI(api_key=self.api_key, base_url=base_url))

    @traceable(
        name="deepseek_create_message",
        run_type="llm",
        process_inputs=lambda inputs: {
            "message_count": len(inputs.get("messages", [])),
            "tool_count": len(inputs.get("tools", [])),
        },
    )
    def create_message(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        self.tracer.event(
            "agent.request",
            "Calling DeepSeek chat completions.",
            model=self.model,
            messages=len(messages),
            tools=len(tools),
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0,
            max_tokens=10000,
        )
        message = raw_message_from_openai(response.choices[0].message)
        self.tracer.event(
            "agent.response",
            "Received assistant message.",
            has_tool_calls=bool(message.get("tool_calls")),
            tool_calls=len(message.get("tool_calls") or []),
            has_content=bool(message.get("content")),
            has_reasoning=bool(message.get("reasoning_content")),
        )
        return message


def create_agent_node(
    agent_client: Any,
    max_agent_turns: int,
    tracer: Optional[TracePrinter] = None,
):
    """Create the graph node that asks the model what to do next."""

    tracer = tracer or NULL_TRACE

    def agent_node(state: JarvisState) -> JarvisState:
        turn_count = state.get("turn_count", 0)
        tracer.event(
            "graph.agent",
            "Entering agent node.",
            turn=turn_count + 1,
            max_turns=max_agent_turns,
            messages=len(state.get("messages", [])),
        )
        if turn_count >= max_agent_turns:
            error = f"Max agent turns exceeded ({max_agent_turns})."
            tracer.event("graph.guard", "Stopping graph because max turns was reached.", error=error)
            return {
                **state,
                "error": error,
                "final_response": error,
                "next": "end",
            }

        messages = copy.deepcopy(state.get("messages", []))
        assistant_message = agent_client.create_message(messages, get_todoist_tools())
        messages.append(assistant_message)

        # No tool calls means the model has chosen ANSWER and the graph can end.
        final_response = ""
        if not assistant_message.get("tool_calls"):
            final_response = assistant_message.get("content") or ""
            tracer.payload("agent.final", "content", final_response)

        tool_calls = assistant_message.get("tool_calls") or []
        next_node = "end"
        if any(is_ask_user_tool_call(tool_call) for tool_call in tool_calls):
            next_node = "hitl"
        elif tool_calls:
            next_node = "tools"

        tracer.event(
            "graph.route",
            "Agent node completed.",
            next=next_node,
            turn=turn_count + 1,
        )

        return {
            **state,
            "messages": messages,
            "turn_count": turn_count + 1,
            "final_response": final_response,
            "next": next_node,
        }

    return agent_node


__all__ = ["DeepSeekAgentClient", "create_agent_node", "raw_message_from_openai"]
