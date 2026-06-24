"""LangGraph routing functions."""

from agents.agent_api.app.constants import SUMMARIZE_THRESHOLD
from agents.agent_api.app.graph.extractors import extract_list_from_content
from agents.agent_api.app.graph.risk import partition_tool_calls
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.control import is_ask_user_tool_call


def route_after_agent(state: JarvisState) -> str:
    """Route from the agent node based on the latest assistant output."""

    if state.get("error"):
        return "end"

    messages = state.get("messages", [])
    latest_message = messages[-1] if messages else {}
    tool_calls = latest_message.get("tool_calls") or []

    if any(is_ask_user_tool_call(tool_call) for tool_call in tool_calls):
        return "hitl"

    if tool_calls:
        risky, _safe = partition_tool_calls(tool_calls, state)
        if risky:
            return "confirm"
        return "tools"

    return "end"


def route_after_confirm(state: JarvisState) -> str:
    """Route from the confirm node based on the user's decision."""

    return state.get("confirm_decision") or "decline"


def route_after_tools(state: JarvisState) -> str:
    """Route from tools: summarize large results, go direct to agent for small ones."""

    tool_results = state.get("tool_results", [])
    if not tool_results:
        return "agent"

    # Check the most recent tool results (appended by the tools node this turn).
    # Walk backward to find results from this batch.
    messages = state.get("messages", [])
    latest_tool_count = 0
    for msg in reversed(messages):
        if msg.get("role") == "tool":
            latest_tool_count += 1
        else:
            break

    # Check the last N results matching the tool messages just appended
    results_to_check = tool_results[-latest_tool_count:] if latest_tool_count else tool_results[-1:]

    for result in results_to_check:
        content = result.get("content")
        if content is None:
            continue
        items = extract_list_from_content(content)
        if items is not None and len(items) > SUMMARIZE_THRESHOLD:
            return "summarize"

    return "agent"


def route_by_next(state: JarvisState) -> str:
    """Generic router that reads the node-supplied ``state["next"]``.

    Reusable conditional edge for future decision nodes (planner, confirmation,
    rewriter, worker-dispatch): a node sets ``state["next"]`` and its NodeSpec maps
    that key to a target. Defaults to ``"end"`` when unset.
    """

    return state.get("next") or "end"


__all__ = ["route_after_agent", "route_after_confirm", "route_after_tools", "route_by_next"]
