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
DEEPSEEK_REQUEST_TIMEOUT_SECONDS = settings.deepseek_request_timeout_seconds
DEEPSEEK_MAX_RETRY_ATTEMPTS = settings.deepseek_max_retry_attempts
DEEPSEEK_RETRY_MAX_DELAY_SECONDS = settings.deepseek_retry_max_delay_seconds

LANGSMITH_TAGS: List[str] = ["jarvis", "langgraph", "todoist", "local"]

__all__ = [
    "USER_ID",
    "ALLOW_MUTATIONS",
    "MAX_AGENT_TURNS",
    "DEBUG_TRACE",
    "DEBUG_PAYLOADS",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_REQUEST_TIMEOUT_SECONDS",
    "DEEPSEEK_MAX_RETRY_ATTEMPTS",
    "DEEPSEEK_RETRY_MAX_DELAY_SECONDS",
    "LANGSMITH_TAGS",
]
