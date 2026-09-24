"""Tool execution graph node."""

from typing import Any, Dict, Optional, Sequence

from langchain_core.runnables import RunnableConfig

from agents.agent_api.app.api.schemas import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_COUNT,
)
from agents.agent_api.app.graph.run_deps import RunDeps, deps_from_config
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.thread_memory import (
    decoded_jpeg_bytes,
    fetch_previous_image_by_reference,
)
from agents.agent_api.app.tools.base import parse_tool_call_arguments, tool_call_name
from agents.agent_api.app.tools.control import RECALL_IMAGE_TOOL_NAME
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


def _accumulated_image_bytes(images: Sequence[Dict[str, Any]]) -> int:
    """Total decoded bytes of the JPEG data-URL images already in the batch."""

    return sum(len(data) for image in images if (data := decoded_jpeg_bytes(image)))


async def _resolve_recall_call(
    tool_call: Dict[str, Any],
    deps: Optional[RunDeps],
    tracer: TracePrinter,
) -> Dict[str, Any]:
    """Fetch a previous-thread image on demand and attach it to the run's image
    batch, so the next orchestrator turn sees it. Fail-open: any miss/error leaves
    ``deps.images`` untouched and reports the image as unavailable to the model."""

    call_id = tool_call.get("id", "missing_tool_call_id")
    try:
        recall_id = parse_tool_call_arguments(tool_call).get("recall_id")
    except ValueError:
        recall_id = None
    recallable = (deps.recallable_images if deps is not None else None) or {}
    ref = recallable.get(recall_id) if isinstance(recall_id, str) else None
    if ref is None or deps is None:
        return build_tool_result(
            call_id, RECALL_IMAGE_TOOL_NAME, success=False,
            error=f"No recallable image for id '{recall_id}'.",
        )
    if len(deps.images) >= MAX_IMAGE_COUNT:
        return build_tool_result(
            call_id, RECALL_IMAGE_TOOL_NAME, success=False,
            error="Image recall limit reached for this turn.",
        )
    try:
        fetched = await fetch_previous_image_by_reference(ref)
    except Exception:
        fetched = None
    if fetched is None:
        return build_tool_result(
            call_id, RECALL_IMAGE_TOOL_NAME, success=False,
            error="Requested image could not be retrieved.",
        )
    if _accumulated_image_bytes(deps.images) + _accumulated_image_bytes((fetched,)) > MAX_IMAGE_BYTES:
        return build_tool_result(
            call_id, RECALL_IMAGE_TOOL_NAME, success=False,
            error="Image recall byte budget exceeded for this turn.",
        )
    deps.images = tuple(deps.images) + (fetched,)
    tracer.event(
        "thread_memory.recall",
        "Recalled a previous-thread image on demand.",
        recall_id=recall_id,
    )
    return build_tool_result(
        call_id, RECALL_IMAGE_TOOL_NAME, success=True,
        content={"recall_id": recall_id, "status": "attached"},
    )


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
        # Results computed in-node, never dispatched: out-of-route rejections and
        # recall_previous_image (a pseudo-tool the node handles because only it, not
        # a stateless registry handler, can reach RunDeps to attach the image).
        # ponytail: if recall is emitted in the same turn as a risky mutation the
        # whole batch routes to prepare_confirm (validate_entities), so recall is
        # deferred, not fetched here — the model just re-calls it alone next turn.
        precomputed: Dict[int, dict] = {}
        executable_calls = []
        for call_index, tool_call in enumerate(tool_calls):
            name = tool_call_name(tool_call)
            if selected and name not in selected:
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
                precomputed[call_index] = result
                continue
            if name == RECALL_IMAGE_TOOL_NAME:
                precomputed[call_index] = await _resolve_recall_call(
                    tool_call, deps, tracer
                )
                continue
            executable_calls.append(tool_call)

        rejected_names = sorted(
            {r["tool_name"] for r in precomputed.values() if r.get("out_of_route_tool")}
        )
        if rejected_names:
            tracer.event(
                "graph.tools.rejected",
                "Rejected tool calls outside the selected route.",
                requested=sorted({tool_call_name(call) for call in tool_calls}),
                allowed=selected_tool_names,
                rejected=rejected_names,
            )

        # Idempotency keys use the call's original assistant-message position, not
        # its position after in-node calls have been filtered out.
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
            precomputed[index]
            if index in precomputed
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
