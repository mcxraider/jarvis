"""Jarvis prompts package: one module per agent role, plus shared context.

Re-exports the historical ``graph.prompts`` import surface so existing callers
(``graph.builder``, ``service``, tests) keep importing from
``agents.agent_api.app.graph.prompts`` unchanged.
"""

from agents.agent_api.app.graph.prompts.context import (
    USER_PROMPT,
    USER_PROMPTS,
    available_tools_line,
    build_initial_messages,
    build_user_prompt_with_request_datetime,
)
from agents.agent_api.app.graph.prompts.orchestrator import (
    CURRENT_GRAPH_COMPATIBILITY_NOTE,
    ORCHESTRATOR_PROMPT,
    get_orchestrator_prompt,
    get_system_prompt,
)
from agents.agent_api.app.graph.prompts.worker import WORKER_PROMPT, get_worker_prompt

__all__ = [
    "CURRENT_GRAPH_COMPATIBILITY_NOTE",
    "ORCHESTRATOR_PROMPT",
    "USER_PROMPT",
    "USER_PROMPTS",
    "WORKER_PROMPT",
    "available_tools_line",
    "build_initial_messages",
    "build_user_prompt_with_request_datetime",
    "get_orchestrator_prompt",
    "get_system_prompt",
    "get_worker_prompt",
]
