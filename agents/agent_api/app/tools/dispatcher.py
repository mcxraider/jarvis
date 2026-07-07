"""Domain-neutral tool dispatcher and the ToolNode bridge.

The dispatcher bridges model tool calls to registered tool handlers, applying the
shared mutation guard, result envelope, tracing, and classified-error handling.
It is driven entirely by a :class:`ToolRegistry`, so it works for any domain
(Todoist today, Gmail/Calendar/Notion later) without changes here.
"""

import contextvars
import copy
import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

from langchain_core.messages import ToolMessage
from langgraph.prebuilt import ToolNode
from langsmith import traceable

from agents.agent_api.app.config import settings
from agents.agent_api.app.constants import ALLOW_MUTATIONS
from agents.agent_api.app.graph.canonicalize import build_operation_idempotency_key
from agents.agent_api.app.idempotency.store import ClaimResult, ClaimState, IdempotencyStore
from agents.agent_api.app.tools.base import (
    ToolRegistry,
    parse_arguments,
    parse_tool_call_arguments,
    tool_call_name,
)
from agents.agent_api.app.tools.errors import ClassifiedApiError
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter

@dataclass
class IdempotencyBatchContext:
    """Per-batch context for the idempotency layer.

    ``call_index_map`` maps each tool_call_id to its 0-based position in the
    original assistant message. This allows the idempotency key to distinguish
    intentional parallel duplicates (same args, different positions) from retries.
    """

    thread_id: str
    turn_count: int
    call_index_map: Dict[str, int] = field(default_factory=dict)


_IDEMPOTENCY_CONTEXT: contextvars.ContextVar[Optional[IdempotencyBatchContext]] = (
    contextvars.ContextVar("jarvis_tool_idempotency_context", default=None)
)


@contextmanager
def tool_idempotency_context(
    thread_id: str,
    turn_count: int,
    call_index_map: Optional[Dict[str, int]] = None,
) -> Iterator[None]:
    """Scope direct ToolNode calls to one graph thread and turn."""

    token = _IDEMPOTENCY_CONTEXT.set(
        IdempotencyBatchContext(thread_id, turn_count, call_index_map or {})
    )
    try:
        yield
    finally:
        _IDEMPOTENCY_CONTEXT.reset(token)


def build_tool_result(
    tool_call_id: str,
    tool_name: str,
    success: bool,
    content: Any = None,
    error: Optional[str] = None,
    mutation_blocked: bool = False,
    classified_error: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The canonical Jarvis tool-result envelope shared across the runtime."""

    return {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "success": success,
        "content": content,
        "error": error,
        "mutation_blocked": mutation_blocked,
        "classified_error": classified_error,
    }


class ToolDispatcher:
    """Execute model tool calls against a registry of tool handlers."""

    def __init__(
        self,
        registry: ToolRegistry,
        allow_mutations: bool = ALLOW_MUTATIONS,
        tracer: Optional[TracePrinter] = None,
        idempotency_store: Optional[IdempotencyStore] = None,
        idempotency_operation_ttl_seconds: int = settings.idempotency_operation_ttl_seconds,
        idempotency_lease_seconds: int = settings.idempotency_lease_seconds,
        idempotency_wait_seconds: float = settings.idempotency_wait_seconds,
        idempotency_poll_interval_seconds: float = settings.idempotency_poll_interval_seconds,
    ):
        self.registry = registry
        self.allow_mutations = allow_mutations
        self.tracer = tracer or NULL_TRACE
        self.idempotency_store = idempotency_store
        self.idempotency_operation_ttl_seconds = idempotency_operation_ttl_seconds
        self.idempotency_lease_seconds = idempotency_lease_seconds
        self.idempotency_wait_seconds = idempotency_wait_seconds
        self.idempotency_poll_interval_seconds = idempotency_poll_interval_seconds
        # Snapshot the registry's executable handlers and mutation policy so the
        # hot path is a dict lookup, not a registry walk per tool call.
        self.supported_tools: Dict[str, Callable[[Dict[str, Any]], Any]] = registry.handler_map()
        self._mutating_names = registry.mutating_names()

    def build_langchain_tools(self) -> List[Any]:
        """Build the ToolNode tools for every registered domain."""

        return self.registry.build_langchain_tools(self.execute_tool)

    def execute_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self.tracer.event("tools.batch", "Executing tool call batch.", count=len(tool_calls))
        return [self.execute_tool_call(tool_call) for tool_call in tool_calls]

    def execute_tool_call(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        tool_call_id = tool_call.get("id", "missing_tool_call_id")
        function_data = tool_call.get("function", {})
        tool_name = function_data.get("name", "unknown")

        try:
            arguments = parse_arguments(function_data.get("arguments", "{}"))
        except Exception as error:
            self.tracer.event("tool.error", "Tool call failed.", name=tool_name, error=str(error))
            return build_tool_result(tool_call_id, tool_name, success=False, error=str(error))

        return self.execute_tool(tool_call_id, tool_name, arguments)

    @traceable(
        name="tool_execute",
        run_type="tool",
    )
    def execute_tool(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute one parsed tool call and return the Jarvis result envelope."""

        owner_token: Optional[str] = None
        resolved_key: Optional[str] = None
        ctx_thread_id: Optional[str] = None
        ctx_turn_count: Optional[int] = None
        try:
            self.tracer.event(
                "tool.start",
                "Preparing tool call.",
                id=tool_call_id,
                name=tool_name,
                mutating=tool_name in self._mutating_names,
            )
            self.tracer.payload("tool.args", tool_name, arguments)

            if tool_name not in self.supported_tools:
                self.tracer.event("tool.error", "Tool is not supported.", name=tool_name)
                return build_tool_result(
                    tool_call_id,
                    tool_name,
                    success=False,
                    error=f"Unsupported tool: {tool_name}",
                )

            # Local runs default to read-only mode so a prompt experiment cannot
            # accidentally create, complete, update, or delete real data.
            if tool_name in self._mutating_names and not self.allow_mutations:
                self.tracer.event(
                    "tool.blocked",
                    "Mutation blocked by ALLOW_MUTATIONS = False.",
                    name=tool_name,
                )
                return build_tool_result(
                    tool_call_id,
                    tool_name,
                    success=False,
                    error=(
                        f"Mutation blocked for {tool_name}. Set ALLOW_MUTATIONS = True "
                        "in agents/jarvis.py to allow real Todoist changes."
                    ),
                    mutation_blocked=True,
                )

            if tool_name in self._mutating_names:
                context = _IDEMPOTENCY_CONTEXT.get()
                if context is not None:
                    ctx_thread_id, ctx_turn_count = context.thread_id, context.turn_count
                resolved_key = idempotency_key or self._context_idempotency_key(
                    tool_call_id,
                    tool_name,
                    arguments,
                )
                if resolved_key and self.idempotency_store is not None:
                    claim = self._claim_operation(resolved_key, tool_name)
                    if claim.state is ClaimState.COMPLETED:
                        self._trace_idempotency("cache_hit", tool_name, resolved_key, ctx_thread_id, ctx_turn_count)
                        return self._rebind_cached_result(
                            claim.result,
                            tool_call_id,
                            tool_name,
                        )
                    if claim.state is ClaimState.IN_PROGRESS:
                        claim = self._wait_for_operation(resolved_key, tool_name, ctx_thread_id, ctx_turn_count)
                        if claim.state is ClaimState.COMPLETED:
                            return self._rebind_cached_result(
                                claim.result,
                                tool_call_id,
                                tool_name,
                            )
                        if claim.state is ClaimState.IN_PROGRESS:
                            self._trace_idempotency("timeout", tool_name, resolved_key, ctx_thread_id, ctx_turn_count)
                            return build_tool_result(
                                tool_call_id,
                                tool_name,
                                success=False,
                                error=(
                                    "An identical mutation is still in progress. "
                                    "Retry after the current operation finishes."
                                ),
                            )
                    if claim.state is ClaimState.ACQUIRED:
                        owner_token = claim.owner_token
                        self._trace_idempotency("claim_acquired", tool_name, resolved_key, ctx_thread_id, ctx_turn_count)
                    elif claim.state is ClaimState.UNAVAILABLE:
                        self._trace_idempotency("fail_closed", tool_name, resolved_key, ctx_thread_id, ctx_turn_count)
                        logger.warning(
                            "Mutation blocked because idempotency is unavailable.",
                            extra={
                                "idempotency_operation": "claim",
                                "tool_name": tool_name,
                                "thread_id": ctx_thread_id,
                                "turn_count": ctx_turn_count,
                            },
                        )
                        return build_tool_result(
                            tool_call_id,
                            tool_name,
                            success=False,
                            error=(
                                "This change was not made because mutation safety "
                                "is temporarily unavailable. Please try again shortly."
                            ),
                            mutation_blocked=True,
                        )

            content = self.supported_tools[tool_name](arguments)
            self.tracer.event("tool.done", "Tool call completed.", name=tool_name)
            self.tracer.payload("tool.result", tool_name, content)
            result = build_tool_result(tool_call_id, tool_name, success=True, content=content)
            if owner_token and resolved_key and self.idempotency_store:
                completed = self._complete_operation(
                    resolved_key,
                    owner_token,
                    result,
                )
                self._trace_idempotency(
                    "completed" if completed else "completion_failed",
                    tool_name,
                    resolved_key,
                    ctx_thread_id,
                    ctx_turn_count,
                )
                if not completed:
                    logger.warning(
                        "Tool executed but claim was lost — possible duplicate.",
                        extra={
                            "tool_name": tool_name,
                            "idempotency_key": resolved_key,
                            "thread_id": ctx_thread_id,
                            "turn_count": ctx_turn_count,
                        },
                    )
                    self._abandon_operation(resolved_key, owner_token, tool_name, ctx_thread_id, ctx_turn_count)
            return result
        except ClassifiedApiError as error:
            self._abandon_operation(resolved_key, owner_token, tool_name, ctx_thread_id, ctx_turn_count)
            classified_error = error.to_classifier_payload()
            self.tracer.event(
                "tool.error",
                "Tool call failed with a classified API error.",
                name=tool_name,
                kind=error.kind,
                retryable=error.retryable,
                status_code=error.status_code,
                attempts=error.attempts,
            )
            return build_tool_result(
                tool_call_id,
                tool_name,
                success=False,
                error=error.message,
                classified_error=classified_error,
            )
        except Exception as error:
            self._abandon_operation(resolved_key, owner_token, tool_name, ctx_thread_id, ctx_turn_count)
            self.tracer.event("tool.error", "Tool call failed.", name=tool_name, error=str(error))
            return build_tool_result(tool_call_id, tool_name, success=False, error=str(error))

    def _context_idempotency_key(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Optional[str]:
        context = _IDEMPOTENCY_CONTEXT.get()
        if context is None:
            return None
        if not context.thread_id:
            return None
        call_index = context.call_index_map.get(tool_call_id, 0)
        return build_operation_idempotency_key(
            tool_name,
            arguments,
            context.thread_id,
            context.turn_count,
            call_index,
        )

    def _claim_operation(
        self,
        key: Optional[str],
        tool_name: str,
    ) -> ClaimResult:
        if not key or self.idempotency_store is None:
            return ClaimResult(ClaimState.UNAVAILABLE)
        try:
            return self.idempotency_store.claim(
                key,
                "operation",
                self.idempotency_operation_ttl_seconds,
                self.idempotency_lease_seconds,
                tool_name=tool_name,
            )
        except Exception:
            return ClaimResult(ClaimState.UNAVAILABLE)

    def _wait_for_operation(
        self,
        key: Optional[str],
        tool_name: str,
        thread_id: Optional[str] = None,
        turn_count: Optional[int] = None,
    ) -> ClaimResult:
        if not key or self.idempotency_store is None:
            return ClaimResult(ClaimState.UNAVAILABLE)

        self._trace_idempotency("wait", tool_name, key, thread_id, turn_count)
        deadline = time.monotonic() + self.idempotency_wait_seconds
        while time.monotonic() < deadline:
            time.sleep(
                min(
                    self.idempotency_poll_interval_seconds,
                    max(0.0, deadline - time.monotonic()),
                )
            )
            claim = self._claim_operation(key, tool_name)
            if claim.state is not ClaimState.IN_PROGRESS:
                event = (
                    "cache_hit"
                    if claim.state is ClaimState.COMPLETED
                    else "claim_takeover"
                    if claim.state is ClaimState.ACQUIRED
                    else "fail_closed"
                )
                self._trace_idempotency(event, tool_name, key, thread_id, turn_count)
                return claim
        return ClaimResult(ClaimState.IN_PROGRESS)

    def _abandon_operation(
        self,
        key: Optional[str],
        owner_token: Optional[str],
        tool_name: str,
        thread_id: Optional[str] = None,
        turn_count: Optional[int] = None,
    ) -> None:
        if not key or not owner_token or self.idempotency_store is None:
            return
        try:
            abandoned = self.idempotency_store.abandon(key, owner_token)
        except Exception:
            abandoned = False
        self._trace_idempotency(
            "abandoned" if abandoned else "abandon_failed",
            tool_name,
            key,
            thread_id,
            turn_count,
        )

    def _complete_operation(
        self,
        key: str,
        owner_token: str,
        result: Dict[str, Any],
    ) -> bool:
        if self.idempotency_store is None:
            return False
        try:
            return self.idempotency_store.complete(
                key,
                owner_token,
                result,
                self.idempotency_operation_ttl_seconds,
            )
        except Exception:
            return False

    def _trace_idempotency(
        self,
        action: str,
        tool_name: str,
        key: Optional[str],
        thread_id: Optional[str] = None,
        turn_count: Optional[int] = None,
    ) -> None:
        self.tracer.event(
            f"idempotency.operation.{action}",
            "Operation idempotency state changed.",
            tool_name=tool_name,
            key=key,
            thread_id=thread_id,
            turn_count=turn_count,
        )

    @staticmethod
    def _rebind_cached_result(
        cached: Optional[Dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
    ) -> Dict[str, Any]:
        if not isinstance(cached, dict):
            return build_tool_result(
                tool_call_id,
                tool_name,
                success=False,
                error="Cached operation result is invalid.",
            )
        result = copy.deepcopy(cached)
        result["tool_call_id"] = tool_call_id
        result["tool_name"] = tool_name
        result["idempotency_deduplicated"] = True
        return result

    @staticmethod
    def _parse_arguments(arguments_json: Any) -> Dict[str, Any]:
        # Retained for backwards compatibility with callers that referenced the
        # dispatcher's parser directly.
        return parse_arguments(arguments_json)

    @staticmethod
    def _result(
        tool_call_id: str,
        tool_name: str,
        success: bool,
        content: Any = None,
        error: Optional[str] = None,
        mutation_blocked: bool = False,
        classified_error: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return build_tool_result(
            tool_call_id,
            tool_name,
            success,
            content=content,
            error=error,
            mutation_blocked=mutation_blocked,
            classified_error=classified_error,
        )


def tool_result_to_message(result: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a dispatcher result into a raw chat tool message."""

    # The next agent turn sees this as the observation for its prior tool call.
    return {
        "role": "tool",
        "tool_call_id": result["tool_call_id"],
        "name": result["tool_name"],
        "content": json.dumps(result, default=str),
    }


def openai_tool_call_to_toolnode_call(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw OpenAI-compatible tool call into ToolNode's direct-call shape."""

    return {
        "name": tool_call_name(tool_call),
        "args": parse_tool_call_arguments(tool_call),
        "id": tool_call.get("id", "missing_tool_call_id"),
        "type": "tool_call",
    }


def toolnode_output_messages(output: Any) -> List[ToolMessage]:
    """Extract ToolMessages from ToolNode output across supported input modes."""

    if isinstance(output, dict):
        return output.get("messages", [])
    return output or []


def tool_message_to_result(tool_message: ToolMessage) -> Dict[str, Any]:
    """Convert a ToolNode ToolMessage into the existing Jarvis tool result envelope."""

    content = tool_message.content
    parsed_content: Any = content
    if isinstance(content, str):
        try:
            parsed_content = json.loads(content)
        except json.JSONDecodeError:
            parsed_content = content

    if isinstance(parsed_content, dict) and {
        "tool_call_id",
        "tool_name",
        "success",
        "content",
        "error",
        "mutation_blocked",
    }.issubset(parsed_content.keys()):
        return parsed_content

    failed = getattr(tool_message, "status", "success") == "error"
    return build_tool_result(
        tool_message.tool_call_id,
        tool_message.name or "unknown",
        success=not failed,
        content=None if failed else parsed_content,
        error=str(content) if failed else None,
    )


def execute_tool_calls_with_toolnode(
    tool_calls: List[Dict[str, Any]],
    tool_node: ToolNode,
    tool_dispatcher: ToolDispatcher,
) -> List[Dict[str, Any]]:
    """Execute supported calls through ToolNode and return ordered Jarvis results."""

    toolnode_calls: List[Dict[str, Any]] = []
    results_by_id: Dict[str, Dict[str, Any]] = {}

    for tool_call in tool_calls:
        tool_call_id = tool_call.get("id", "missing_tool_call_id")
        tool_name = tool_call_name(tool_call)

        try:
            toolnode_call = openai_tool_call_to_toolnode_call(tool_call)
        except Exception:
            results_by_id[tool_call_id] = tool_dispatcher.execute_tool_call(tool_call)
            continue

        if tool_name not in tool_dispatcher.supported_tools:
            results_by_id[tool_call_id] = tool_dispatcher.execute_tool(
                tool_call_id,
                tool_name,
                toolnode_call["args"],
            )
            continue

        toolnode_calls.append(toolnode_call)

    if toolnode_calls:
        try:
            output = tool_node.invoke(toolnode_calls)
        except Exception:
            # Some ToolNode versions require graph runtime config even for
            # direct calls. Keep the graph path moving through the same
            # dispatcher policy rather than failing before any tool runs.
            for tool_call in tool_calls:
                tool_call_id = tool_call.get("id", "missing_tool_call_id")
                if tool_call_id not in results_by_id:
                    results_by_id[tool_call_id] = tool_dispatcher.execute_tool_call(tool_call)
        else:
            for tool_message in toolnode_output_messages(output):
                result = tool_message_to_result(tool_message)
                results_by_id[result["tool_call_id"]] = result

    ordered_results = []
    for tool_call in tool_calls:
        tool_call_id = tool_call.get("id", "missing_tool_call_id")
        if tool_call_id in results_by_id:
            ordered_results.append(results_by_id[tool_call_id])
    return ordered_results


__all__ = [
    "ToolDispatcher",
    "build_tool_result",
    "execute_tool_calls_with_toolnode",
    "openai_tool_call_to_toolnode_call",
    "tool_message_to_result",
    "tool_result_to_message",
    "tool_idempotency_context",
    "toolnode_output_messages",
]
