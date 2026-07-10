"""Tool execution graph node."""

import copy
from typing import Optional

from langgraph.prebuilt import ToolNode

from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.dispatcher import (
    build_tool_result,
    ToolDispatcher,
    execute_tool_calls_with_toolnode,
    tool_idempotency_context,
    tool_result_to_message,
)
from agents.agent_api.app.tools.base import tool_call_name
from agents.agent_api.app.tools.metadata import get_service
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


_TODOIST_TOOLS_WITHOUT_PREFIX = frozenset({
    "complete_task", "uncomplete_task", "create_project",
    "get_tasks", "get_tasks_by_filter", "get_comments",
    "add_comment", "get_labels", "get_projects",
})


def _progress_domain(tool_name: str) -> Optional[str]:
    if "calendar" in tool_name:
        return "calendar"
    if "todoist" in tool_name or tool_name in _TODOIST_TOOLS_WITHOUT_PREFIX:
        return "todoist"
    if "gmail" in tool_name:
        return "gmail"
    if "notion" in tool_name:
        return "notion"
    return None


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
        tool_names = [tool_call_name(call) for call in tool_calls]
        domains = sorted({domain for name in tool_names if (domain := _progress_domain(name))})
        intent = "mutation" if any(
            spec.mutating
            for name in tool_names
            if (spec := tool_dispatcher.registry.get(name)) is not None
        ) else "read"
        tracer.progress({
            "phase": "preparing_change" if intent == "mutation" else "lookup",
            "action": "started",
            **({"domains": domains} if domains else {}),
            "intent": intent,
        })
        tracer.event(
            "graph.tools",
            "Entering tools node.",
            tool_calls=len(tool_calls),
            accumulated_results=len(state.get("tool_results", [])),
        )

        selected_tool_names = state.get("selected_tool_names") or []
        selected = set(selected_tool_names)
        rejected_results = []
        executable_calls = tool_calls
        if selected:
            executable_calls = []
            for tool_call in tool_calls:
                name = tool_call_name(tool_call)
                if name in selected:
                    executable_calls.append(tool_call)
                    continue
                result = build_tool_result(
                    tool_call.get("id", "missing_tool_call_id"),
                    name,
                    success=False,
                    error=(
                        f"Tool '{name}' was not selected for this turn. "
                        f"Allowed tools: {', '.join(selected_tool_names) if selected_tool_names else 'none'}."
                    ),
                )
                result["out_of_route_tool"] = True
                rejected_results.append(result)
            if rejected_results:
                tracer.event(
                    "graph.tools.rejected",
                    "Rejected tool calls outside the selected route.",
                    requested=sorted({tool_call_name(call) for call in tool_calls}),
                    allowed=selected_tool_names,
                    rejected=sorted({result["tool_name"] for result in rejected_results}),
                )

        call_index_map = {tc.get("id", ""): i for i, tc in enumerate(executable_calls)}
        with tool_idempotency_context(
            str(state.get("thread_id") or ""),
            int(state.get("turn_count") or 0),
            call_index_map,
        ):
            results = execute_tool_calls_with_toolnode(
                executable_calls,
                tool_node,
                tool_dispatcher,
            )
        results = rejected_results + results

        existing_results = state.get("tool_results", [])
        existing_batches = {r.get("batch_index") for r in existing_results if r.get("batch_index") is not None}
        current_batch = max(existing_batches, default=-1) + 1
        for result in results:
            result["batch_index"] = current_batch
            result["service"] = result.get("service") or get_service(result.get("tool_name", "")) or _progress_domain(result.get("tool_name", "")) or ""

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
        tracer.progress({
            "phase": "review",
            "action": "completed",
            **({"domains": domains} if domains else {}),
            "intent": intent,
        })

        return {
            "messages": messages,
            "tool_results": state.get("tool_results", []) + results,
            "next": "agent",
        }

    return tools_node


__all__ = ["create_tools_node", "execute_tool_calls_with_toolnode"]
