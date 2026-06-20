"""Runtime constants derived from the Jarvis agent settings.

These mirror the historical module-level constants from the original
``service.py`` so the rest of the package (and the legacy import surface) keep a
single source of truth for run defaults.
"""

from typing import List

from agents.agent_api.app.config import settings

USER_ID = "local-user"
ALLOW_MUTATIONS = settings.allow_mutations
MAX_AGENT_TURNS = settings.max_agent_turns
DEBUG_TRACE = settings.debug_trace
DEBUG_PAYLOADS = settings.debug_payloads

DEEPSEEK_MODEL = settings.deepseek_model
DEEPSEEK_BASE_URL = settings.deepseek_base_url

LANGSMITH_TAGS: List[str] = ["jarvis", "langgraph", "todoist", "local"]

__all__ = [
    "USER_ID",
    "ALLOW_MUTATIONS",
    "MAX_AGENT_TURNS",
    "DEBUG_TRACE",
    "DEBUG_PAYLOADS",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_BASE_URL",
    "LANGSMITH_TAGS",
]
