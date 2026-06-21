"""Tool execution graph node."""

import copy
from typing import Optional

from langgraph.prebuilt import ToolNode

from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.dispatcher import (
    ToolDispatcher,
    execute_tool_calls_with_toolnode,
    tool_result_to_message,
)
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


def create_tools_node(
    tool_dispatcher: ToolDispatcher,
    tracer: Optional[TracePrinter] = None,
):
    """Create the graph node that executes requested tools and records results."""

    tracer = tracer or NULL_TRACE
    tool_node = ToolNode(
        tool_dispatcher.build_langchain_tools(),
        handle_tool_errors=True,
    )

    def tools_node(state: JarvisState) -> JarvisState:
        messages = copy.deepcopy(state.get("messages", []))
        latest_message = messages[-1] if messages else {}
        tool_calls = latest_message.get("tool_calls") or []
        tracer.event(
            "graph.tools",
            "Entering tools node.",
            tool_calls=len(tool_calls),
            accumulated_results=len(state.get("tool_results", [])),
        )

        results = execute_tool_calls_with_toolnode(tool_calls, tool_node, tool_dispatcher)
        # Tool result messages are appended so the next agent turn can synthesize
        # an answer or request another tool call with full context.
        messages.extend(tool_result_to_message(result) for result in results)
        tracer.event(
            "graph.route",
            "Tools node completed.",
            next="agent",
            successes=sum(1 for result in results if result.get("success")),
            failures=sum(1 for result in results if not result.get("success")),
        )

        return {
            **state,
            "messages": messages,
            "tool_results": state.get("tool_results", []) + results,
            "next": "agent",
        }

    return tools_node


__all__ = ["create_tools_node", "execute_tool_calls_with_toolnode"]
