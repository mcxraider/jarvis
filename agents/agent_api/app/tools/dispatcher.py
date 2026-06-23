"""Domain-neutral tool dispatcher and the ToolNode bridge.

The dispatcher bridges model tool calls to registered tool handlers, applying the
shared mutation guard, result envelope, tracing, and classified-error handling.
It is driven entirely by a :class:`ToolRegistry`, so it works for any domain
(Todoist today, Gmail/Calendar/Notion later) without changes here.
"""

import json
from typing import Any, Callable, Dict, List, Optional

from langchain_core.messages import ToolMessage
from langgraph.prebuilt import ToolNode
from langsmith import traceable

from agents.agent_api.app.constants import ALLOW_MUTATIONS
from agents.agent_api.app.tools.base import (
    ToolRegistry,
    parse_arguments,
    parse_tool_call_arguments,
    tool_call_name,
)
from agents.agent_api.app.tools.todoist.client import TodoistApiError
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


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
    ):
        self.registry = registry
        self.allow_mutations = allow_mutations
        self.tracer = tracer or NULL_TRACE
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
    ) -> Dict[str, Any]:
        """Execute one parsed tool call and return the Jarvis result envelope."""

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

            content = self.supported_tools[tool_name](arguments)
            self.tracer.event("tool.done", "Tool call completed.", name=tool_name)
            self.tracer.payload("tool.result", tool_name, content)
            return build_tool_result(tool_call_id, tool_name, success=True, content=content)
        except TodoistApiError as error:
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
            self.tracer.event("tool.error", "Tool call failed.", name=tool_name, error=str(error))
            return build_tool_result(tool_call_id, tool_name, success=False, error=str(error))

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
        output = tool_node.invoke(toolnode_calls)
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
    "toolnode_output_messages",
]
