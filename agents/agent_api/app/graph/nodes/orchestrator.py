"""Orchestrator (agent) graph node and the DeepSeek LLM client."""

import copy
import json
import os
import re
import uuid
from dataclasses import dataclass
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
    DEEPSEEK_REASONING_EFFORT,
    DEEPSEEK_REQUEST_TIMEOUT_SECONDS,
    DEEPSEEK_RETRY_MAX_DELAY_SECONDS,
)
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.base import ToolRegistry
from agents.agent_api.app.tools.control import is_ask_user_tool_call
from agents.agent_api.app.tools.selection import DEFAULT_TOOL_SELECTOR, ToolSelector
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


LLM_FAILURE_MESSAGE = "Jarvis could not reach DeepSeek reliably. Please try again in a moment."


@dataclass
class UsageSummary:
    """Token usage aggregated across DeepSeek calls within one Jarvis run.

    ``cached_tokens`` and ``reasoning_tokens`` are optional provider extras
    (DeepSeek reports prompt cache hits and reasoning tokens); they stay 0 when
    the provider omits them. Monetary cost is intentionally not computed here —
    there is no maintained pricing source yet.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: "UsageSummary") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cached_tokens += other.cached_tokens
        self.reasoning_tokens += other.reasoning_tokens

    def as_dict(self) -> Dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


def _int_attr(source: Any, name: str) -> int:
    """Read an optional integer usage field from a dict or SDK object."""

    if source is None:
        return 0
    value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
    return int(value) if isinstance(value, (int, float)) else 0


def usage_from_response(response: Any) -> UsageSummary:
    """Extract a UsageSummary from an OpenAI-compatible completion response.

    Tolerates missing optional fields so a provider that omits cache/reasoning
    token counts still yields valid prompt/completion/total numbers.
    """

    usage = getattr(response, "usage", None)
    if usage is None:
        return UsageSummary()

    details = (
        usage.get("completion_tokens_details")
        if isinstance(usage, dict)
        else getattr(usage, "completion_tokens_details", None)
    )
    return UsageSummary(
        prompt_tokens=_int_attr(usage, "prompt_tokens"),
        completion_tokens=_int_attr(usage, "completion_tokens"),
        total_tokens=_int_attr(usage, "total_tokens"),
        cached_tokens=_int_attr(usage, "prompt_cache_hit_tokens"),
        reasoning_tokens=_int_attr(details, "reasoning_tokens"),
    )


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
        reasoning_effort: str = DEEPSEEK_REASONING_EFFORT,
        tracer: Optional[TracePrinter] = None,
        request_timeout_seconds: float = DEEPSEEK_REQUEST_TIMEOUT_SECONDS,
        max_retry_attempts: int = DEEPSEEK_MAX_RETRY_ATTEMPTS,
        retry_max_delay_seconds: float = DEEPSEEK_RETRY_MAX_DELAY_SECONDS,
        retry_sleep: Optional[Any] = None,
    ):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.tracer = tracer or NULL_TRACE
        # Token usage accumulated across every turn/retry of one Jarvis run, read
        # by run_jarvis for the per-run log footer. LangSmith gets per-call usage
        # automatically via wrap_openai; this is the on-disk fallback.
        self.usage = UsageSummary()
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
            reasoning_effort=self.reasoning_effort,
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
                max_tokens=10000,
                reasoning_effort=self.reasoning_effort,
                extra_body={"thinking": {"type": "enabled"}},
            )

        try:
            response = self._retrying()(create_completion)
            # Read usage before the completion object is discarded.
            turn_usage = usage_from_response(response)
            self.usage.add(turn_usage)
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

        cache_hit_rate = (
            round(turn_usage.cached_tokens / turn_usage.prompt_tokens * 100, 1)
            if turn_usage.prompt_tokens > 0 and turn_usage.cached_tokens > 0
            else None
        )
        self.tracer.event(
            "agent.response",
            "Received assistant message.",
            has_tool_calls=bool(message.get("tool_calls")),
            tool_calls=len(message.get("tool_calls") or []),
            has_content=bool(message.get("content")),
            has_reasoning=bool(message.get("reasoning_content")),
            prompt_tokens=turn_usage.prompt_tokens or None,
            completion_tokens=turn_usage.completion_tokens or None,
            total_tokens=turn_usage.total_tokens or None,
            cached_tokens=turn_usage.cached_tokens or None,
            cache_hit_rate=cache_hit_rate,
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


_CLARIFICATION_PATTERNS = [
    "could you clarify",
    "could you specify",
    "could you tell me",
    "can you tell me",
    "what would you like",
    "which one do you",
    "do you want me to",
    "would you like me to",
    "please provide",
    "i need more information",
    "can you provide",
]


def _looks_like_question(content: str) -> bool:
    """Detect text-only responses that are actually clarification questions.

    The system prompt forbids asking questions in ANSWER (text-only responses).
    A bare "?" in a completion summary (e.g. a task named "meet jon maybe?") is
    not a question — we only treat "?" as meaningful when it ends a sentence:
    at end-of-text, before a newline, or before whitespace+uppercase (new sentence).
    Markdown bold/italic markers are stripped first to avoid false positives from
    task names like **"meeting tomorrow?"** embedded in a success summary.
    """
    if not content:
        return False
    stripped = content.strip()
    if not stripped:
        return False
    plain = re.sub(r'\*+', '', stripped)
    if re.search(r'\?(?:\s*$|\s*\n|\s+[A-Z])', plain):
        return True
    lower = stripped.lower()
    return any(p in lower for p in _CLARIFICATION_PATTERNS)


def create_agent_node(
    agent_client: Any,
    registry: ToolRegistry,
    max_agent_turns: int,
    tracer: Optional[TracePrinter] = None,
    tool_selector: Optional[ToolSelector] = None,
):
    """Create the graph node that asks the model what to do next.

    The available tool catalogue comes from ``registry`` so the agent node is
    domain-agnostic — adding a tool domain never edits this node. ``tool_selector``
    decides which of those tools the model actually sees this turn; the default
    pass-through selector exposes the whole catalogue (see ``tools/selection.py``).
    """

    tracer = tracer or NULL_TRACE
    tool_selector = tool_selector or DEFAULT_TOOL_SELECTOR

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
            user_message = (
                "You have reached the max number of turns for this request. "
                "I'm unable to handle this complex request — please try breaking it into smaller steps."
            )
            tracer.event("graph.guard", "Stopping graph because max turns was reached.", error=error)
            return {
                "error": error,
                "final_response": user_message,
                "next": "end",
            }

        messages = copy.deepcopy(state.get("messages", []))
        # Narrow the catalogue to the tools this turn should expose. The default
        # selector returns everything; a future query-aware selector returns a
        # relevant subset (see tools/selection.py). Execution still runs against
        # the full registry, so this only shapes what the model sees.
        tool_schemas = tool_selector.select_schemas(state.get("user_prompt", ""), registry)
        tracer.event(
            "graph.tools.selected",
            "Selected tools for this turn.",
            available=len(registry.specs),
            selected=len(tool_schemas),
        )
        try:
            assistant_message = agent_client.create_message(messages, tool_schemas)
        except DeepSeekAgentClientError as error:
            tracer.event(
                "graph.agent",
                "Stopping graph because DeepSeek failed.",
                error_type=error.payload.get("type"),
                attempts=error.payload.get("attempts"),
            )
            return {
                "error": json.dumps(error.payload, sort_keys=True),
                "final_response": LLM_FAILURE_MESSAGE,
                "next": "end",
            }
        messages.append(assistant_message)

        final_response = ""
        next_node = "end"

        if not assistant_message.get("tool_calls"):
            content = assistant_message.get("content") or ""
            if _looks_like_question(content):
                synthetic_id = f"synthetic_ask_user_{uuid.uuid4().hex[:8]}"
                synthetic_tool_call = {
                    "id": synthetic_id,
                    "type": "function",
                    "function": {
                        "name": "ask_user",
                        "arguments": json.dumps({
                            "question": content,
                            "reason": "Auto-converted from text response containing a question.",
                        }),
                    },
                }
                assistant_message["tool_calls"] = [synthetic_tool_call]
                assistant_message["content"] = None
                messages[-1] = assistant_message
                next_node = "hitl"
                tracer.event(
                    "graph.hitl_upgrade",
                    "Converted text question to ask_user tool call.",
                    question_preview=content[:100],
                )
            else:
                final_response = content
                tracer.payload("agent.final", "content", final_response)
                run_log = getattr(tracer, "run_log", None)
                if run_log is not None:
                    run_log.write_messages_dump(
                        "final_turn_input (context sent to LLM on ANSWER turn)",
                        messages[:-1],
                    )
        else:
            tool_calls = assistant_message.get("tool_calls") or []
            if any(is_ask_user_tool_call(tc) for tc in tool_calls):
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
    "UsageSummary",
    "create_agent_node",
    "raw_message_from_openai",
    "usage_from_response",
]
