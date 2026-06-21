"""Domain-neutral control-flow pseudo-tools.

These are not domain actions — they steer the graph itself. ``ask_user`` pauses the
run with a human-in-the-loop interrupt and is routed to the HITL node rather than
executed by the tool dispatcher (it has no handler and no LangChain wrapper). It
lives here, not under any domain package, so the graph core never imports a
concrete domain to reach generic agent machinery.
"""

from typing import Any, Dict, List

from agents.agent_api.app.tools.base import ToolSpec, tool_call_name

ASK_USER_TOOL_NAME = "ask_user"


def _ask_user_parameters() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "One concise question to ask the user before continuing.",
            },
            "reason": {
                "type": "string",
                "description": "Optional short explanation of why clarification is needed.",
            },
            "missing_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional missing inputs needed to continue safely.",
            },
            "risk": {
                "type": "string",
                "description": "Optional risk if Jarvis guessed instead of asking.",
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }


def get_ask_user_schema() -> Dict[str, Any]:
    """Return the OpenAI/DeepSeek function schema for the ask_user pseudo-tool."""

    return {
        "type": "function",
        "function": {
            "name": ASK_USER_TOOL_NAME,
            "description": (
                "Ask the user for one missing or risky detail. This pauses the "
                "LangGraph run with a human-in-the-loop interrupt."
            ),
            "parameters": _ask_user_parameters(),
        },
    }


def get_control_tool_specs() -> List[ToolSpec]:
    """Control pseudo-tools as registry specs (no handler, no LangChain builder)."""

    return [ToolSpec(name=ASK_USER_TOOL_NAME, openai_schema=get_ask_user_schema())]


def get_control_tools() -> List[Dict[str, Any]]:
    """Control tool schemas as the LLM sees them."""

    return [spec.openai_schema for spec in get_control_tool_specs()]


def is_ask_user_tool_call(tool_call: Dict[str, Any]) -> bool:
    """Return whether a tool call is the HITL clarification pseudo-tool."""

    return tool_call_name(tool_call) == ASK_USER_TOOL_NAME


__all__ = [
    "ASK_USER_TOOL_NAME",
    "get_ask_user_schema",
    "get_control_tool_specs",
    "get_control_tools",
    "is_ask_user_tool_call",
]
