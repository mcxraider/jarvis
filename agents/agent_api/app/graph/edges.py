"""LangGraph routing functions."""

from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.todoist.tools import is_ask_user_tool_call


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
        return "tools"

    return "end"


__all__ = ["route_after_agent"]
