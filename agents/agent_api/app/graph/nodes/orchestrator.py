"""Orchestrator (agent) graph node and the DeepSeek LLM client."""

import copy
import json
import os
from typing import Any, Dict, List, Optional

from langsmith import traceable
from langsmith.wrappers import wrap_openai
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_random_exponential
from tenacity.nap import sleep as tenacity_sleep

from agents.agent_api.app.constants import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MAX_RETRY_ATTEMPTS,
    DEEPSEEK_MODEL,
    DEEPSEEK_REQUEST_TIMEOUT_SECONDS,
    DEEPSEEK_RETRY_MAX_DELAY_SECONDS,
)
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.base import ToolRegistry
from agents.agent_api.app.tools.control import is_ask_user_tool_call
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


LLM_FAILURE_MESSAGE = "Jarvis could not reach DeepSeek reliably. Please try again in a moment."


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


class DeepSeekAgentClientError(RuntimeError):
    """Terminal DeepSeek client failure with graph-safe structured metadata."""

    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        super().__init__(json.dumps(payload, sort_keys=True))


class DeepSeekAgentClient:
    """Small wrapper around DeepSeek's OpenAI-compatible chat API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEEPSEEK_MODEL,
        base_url: str = DEEPSEEK_BASE_URL,
        tracer: Optional[TracePrinter] = None,
        request_timeout_seconds: float = DEEPSEEK_REQUEST_TIMEOUT_SECONDS,
        max_retry_attempts: int = DEEPSEEK_MAX_RETRY_ATTEMPTS,
        retry_max_delay_seconds: float = DEEPSEEK_RETRY_MAX_DELAY_SECONDS,
        retry_sleep: Optional[Any] = None,
    ):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.model = model
        self.tracer = tracer or NULL_TRACE
        self.max_retry_attempts = max(1, max_retry_attempts)
        self.retry_max_delay_seconds = retry_max_delay_seconds
        self.retry_sleep = retry_sleep
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required to run Jarvis.")
        self.client = wrap_openai(
            OpenAI(
                api_key=self.api_key,
                base_url=base_url,
                timeout=request_timeout_seconds,
            )
        )

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
        attempts = 0

        def create_completion() -> Any:
            nonlocal attempts
            attempts += 1
            return self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0,
                max_tokens=10000,
            )

        try:
            response = self._retrying()(create_completion)
            message = raw_message_from_openai(response.choices[0].message)
        except Exception as error:
            payload = self._failure_payload(error, attempts)
            self.tracer.event(
                "agent.error",
                "DeepSeek chat completion failed.",
                error_type=payload["type"],
                retryable=payload["retryable"],
                attempts=payload["attempts"],
                status_code=payload.get("status_code"),
            )
            raise DeepSeekAgentClientError(payload) from error

        self.tracer.event(
            "agent.response",
            "Received assistant message.",
            has_tool_calls=bool(message.get("tool_calls")),
            tool_calls=len(message.get("tool_calls") or []),
            has_content=bool(message.get("content")),
            has_reasoning=bool(message.get("reasoning_content")),
        )
        return message

    def _retrying(self) -> Retrying:
        sleep = self.retry_sleep if self.retry_sleep is not None else tenacity_sleep
        return Retrying(
            retry=retry_if_exception(self._is_retryable_error),
            wait=wait_random_exponential(multiplier=1, max=self.retry_max_delay_seconds),
            stop=stop_after_attempt(self.max_retry_attempts),
            reraise=True,
            sleep=sleep,
            before_sleep=self._trace_retry,
        )

    def _trace_retry(self, retry_state: Any) -> None:
        error = retry_state.outcome.exception() if retry_state.outcome else None
        self.tracer.event(
            "agent.retry",
            "Retrying DeepSeek chat completion.",
            attempt=retry_state.attempt_number,
            error_type=self._error_type(error),
            status_code=self._status_code(error),
        )

    def _is_retryable_error(self, error: BaseException) -> bool:
        if isinstance(error, (APITimeoutError, APIConnectionError)):
            return True

        status_code = self._status_code(error)
        if status_code == 429:
            return True
        if isinstance(error, APIStatusError) and status_code is not None:
            return status_code >= 500

        return isinstance(error, RateLimitError)

    def _failure_payload(self, error: BaseException, attempts: int) -> Dict[str, Any]:
        status_code = self._status_code(error)
        payload: Dict[str, Any] = {
            "source": "deepseek",
            "type": self._error_type(error),
            "retryable": self._is_retryable_error(error),
            "attempts": attempts,
            "message": str(error),
        }
        if status_code is not None:
            payload["status_code"] = status_code
        return payload

    def _error_type(self, error: Optional[BaseException]) -> str:
        if error is None:
            return "unexpected"
        if isinstance(error, APITimeoutError):
            return "timeout"
        if isinstance(error, APIConnectionError):
            return "connection_error"

        status_code = self._status_code(error)
        if status_code == 429 or isinstance(error, RateLimitError):
            return "rate_limit"
        if status_code is not None and status_code >= 500:
            return "server_error"
        if status_code is not None and 400 <= status_code < 500:
            return "client_error"

        return "unexpected"

    @staticmethod
    def _status_code(error: Optional[BaseException]) -> Optional[int]:
        status_code = getattr(error, "status_code", None)
        return status_code if isinstance(status_code, int) else None


def create_agent_node(
    agent_client: Any,
    registry: ToolRegistry,
    max_agent_turns: int,
    tracer: Optional[TracePrinter] = None,
):
    """Create the graph node that asks the model what to do next.

    The available tool catalogue comes from ``registry`` so the agent node is
    domain-agnostic — adding a tool domain never edits this node.
    """

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
        try:
            assistant_message = agent_client.create_message(messages, registry.openai_schemas())
        except DeepSeekAgentClientError as error:
            tracer.event(
                "graph.agent",
                "Stopping graph because DeepSeek failed.",
                error_type=error.payload.get("type"),
                attempts=error.payload.get("attempts"),
            )
            return {
                **state,
                "error": json.dumps(error.payload, sort_keys=True),
                "final_response": LLM_FAILURE_MESSAGE,
                "next": "end",
            }
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


__all__ = [
    "DeepSeekAgentClient",
    "DeepSeekAgentClientError",
    "LLM_FAILURE_MESSAGE",
    "create_agent_node",
    "raw_message_from_openai",
]
