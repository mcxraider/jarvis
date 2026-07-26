"""Tool execution graph node."""

from typing import Dict, Optional

from langchain_core.runnables import RunnableConfig

from agents.agent_api.app.graph.run_deps import RunDeps, deps_from_config
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.base import tool_call_name
from agents.agent_api.app.tools.dispatcher import (
    ToolDispatcher,
    async_execute_tool_calls,
    build_tool_result,
    tool_idempotency_context,
    tool_result_to_message,
)
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
    tool_dispatcher: Optional[ToolDispatcher] = None,
    tracer: Optional[TracePrinter] = None,
):
    """Create the graph node that executes requested tools and records results."""

    _captured = RunDeps(dispatcher=tool_dispatcher, tracer=tracer or NULL_TRACE)

    async def tools_node(
        state: JarvisState,
        config: RunnableConfig | None = None,
    ) -> JarvisState:
        deps = deps_from_config(config)
        dispatcher_deps = (
            deps
            if deps is not None and deps.dispatcher is not None
            else _captured
        )
        tool_dispatcher = dispatcher_deps.dispatcher
        tracer = (
            deps.tracer
            if deps is not None and deps.tracer is not None
            else _captured.tracer
        )
        if tool_dispatcher is None:
            raise RuntimeError(
                "Tools node requires a dispatcher from RunDeps or captured fallbacks."
            )
        messages = list(state.get("messages", []))
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
        rejected_results: Dict[int, dict] = {}
        executable_calls = tool_calls
        if selected:
            executable_calls = []
            for call_index, tool_call in enumerate(tool_calls):
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
                rejected_results[call_index] = result
            if rejected_results:
                tracer.event(
                    "graph.tools.rejected",
                    "Rejected tool calls outside the selected route.",
                    requested=sorted({tool_call_name(call) for call in tool_calls}),
                    allowed=selected_tool_names,
                    rejected=sorted(
                        {result["tool_name"] for result in rejected_results.values()}
                    ),
                )

        # Idempotency keys use the call's original assistant-message position, not
        # its position after out-of-route calls have been filtered out.
        executable_ids = {id(tool_call) for tool_call in executable_calls}
        call_index_map = {
            tool_call.get("id", ""): index
            for index, tool_call in enumerate(tool_calls)
            if id(tool_call) in executable_ids
        }
        with tool_idempotency_context(
            str(state.get("thread_id") or ""),
            int(state.get("turn_count") or 0),
            call_index_map,
        ):
            executable_results = await async_execute_tool_calls(
                executable_calls,
                tool_dispatcher,
            )
        executable_iter = iter(executable_results)
        results = [
            rejected_results[index]
            if index in rejected_results
            else next(executable_iter)
            for index in range(len(tool_calls))
        ]

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


__all__ = ["create_tools_node"]
