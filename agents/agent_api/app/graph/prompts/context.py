"""Prompt context and message builders shared across roles.

The "Available tools" line is rendered by the orchestrator from the runtime
snapshot's registered tool names (or an explicit ``registered_tools`` list for
offline/DI runs), so the prompt's capability claims always match the live
:class:`ToolRegistry`.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from agents.agent_api.app.graph.prompts.orchestrator import (
    _current_user_datetime,
    _user_timezone,
    get_system_prompt,
)
from agents.agent_api.app.user_context.runtime import RuntimeContextSnapshot

USER_PROMPTS: List[str] = []

USER_PROMPT = USER_PROMPTS[0] if USER_PROMPTS else ""


def build_user_prompt_with_request_datetime(
    user_prompt: str,
    timezone: Optional[str] = None,
    request_datetime: Optional[datetime] = None,
) -> str:
    """Add one timezone-resolved request timestamp to the user message content."""

    current = request_datetime or _current_user_datetime(_user_timezone(timezone))

    return "\n".join(
        [
            "Request context:",
            "Current request date and time: "
            f"{current.isoformat(timespec='seconds')} ({current:%A})",
            "",
            "User request:",
            user_prompt,
        ]
    )


def build_initial_messages(
    user_prompt: str,
    timezone: Optional[str] = None,
    user_name: Optional[str] = None,
    runtime_context: Optional[RuntimeContextSnapshot] = None,
    registered_tools: Optional[List[str]] = None,
    relevant_domains: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Create the raw message list used by the DeepSeek API.

    ``relevant_domains`` (from the query router) is forwarded to the system
    prompt to slim the per-domain fragments; ``None`` keeps every active domain
    (today's behavior).
    """

    return [
        {
            "role": "system",
            "content": get_system_prompt(
                timezone,
                user_name=user_name,
                runtime_context=runtime_context,
                registered_tools=registered_tools,
                relevant_domains=relevant_domains,
            ),
        },
        {
            "role": "user",
            "content": build_user_prompt_with_request_datetime(
                user_prompt,
                timezone=(runtime_context.timezone if runtime_context is not None else timezone),
            ),
        },
    ]


__all__ = [
    "USER_PROMPT",
    "USER_PROMPTS",
    "build_initial_messages",
    "build_user_prompt_with_request_datetime",
]
