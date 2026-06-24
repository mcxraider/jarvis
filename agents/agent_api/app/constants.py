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

CONFIRM_BULK_THRESHOLD = settings.confirm_bulk_threshold

SUMMARIZER_MODEL = settings.summarizer_model
SUMMARIZE_THRESHOLD = settings.summarize_threshold
SUMMARIZER_REQUEST_TIMEOUT_SECONDS = settings.summarizer_request_timeout_seconds
SUMMARIZER_MAX_RETRY_ATTEMPTS = settings.summarizer_max_retry_attempts
SUMMARIZER_RETRY_MAX_DELAY_SECONDS = settings.summarizer_retry_max_delay_seconds
SUMMARIZER_MIN_ID_COVERAGE = settings.summarizer_min_id_coverage
SUMMARIZER_MAX_TOKENS_CEILING = settings.summarizer_max_tokens_ceiling

EXECUTOR_MAX_WORKERS = settings.executor_max_workers
EXECUTOR_BATCH_TIMEOUT_SECONDS = settings.executor_batch_timeout_seconds
EXECUTOR_CIRCUIT_BREAKER_THRESHOLD = settings.executor_circuit_breaker_threshold
EXECUTOR_THROTTLE_ENABLED = settings.executor_throttle_enabled

DEEPSEEK_MODEL = settings.deepseek_model
DEEPSEEK_BASE_URL = settings.deepseek_base_url
DEEPSEEK_REQUEST_TIMEOUT_SECONDS = settings.deepseek_request_timeout_seconds
DEEPSEEK_MAX_RETRY_ATTEMPTS = settings.deepseek_max_retry_attempts
DEEPSEEK_RETRY_MAX_DELAY_SECONDS = settings.deepseek_retry_max_delay_seconds

# Active tool-domain tags, surfaced as LangSmith run tags. Add a tag here when a
# new domain (gmail, calendar, ...) goes live so observability stays accurate;
# the domain name is no longer baked into the middle of LANGSMITH_TAGS.
LANGSMITH_DOMAIN_TAGS: List[str] = ["todoist"]
LANGSMITH_TAGS: List[str] = ["jarvis", "langgraph", *LANGSMITH_DOMAIN_TAGS, "local"]

__all__ = [
    "USER_ID",
    "ALLOW_MUTATIONS",
    "CONFIRM_BULK_THRESHOLD",
    "MAX_AGENT_TURNS",
    "DEBUG_TRACE",
    "DEBUG_PAYLOADS",
    "SUMMARIZER_MODEL",
    "SUMMARIZE_THRESHOLD",
    "SUMMARIZER_REQUEST_TIMEOUT_SECONDS",
    "SUMMARIZER_MAX_RETRY_ATTEMPTS",
    "SUMMARIZER_RETRY_MAX_DELAY_SECONDS",
    "SUMMARIZER_MIN_ID_COVERAGE",
    "SUMMARIZER_MAX_TOKENS_CEILING",
    "EXECUTOR_MAX_WORKERS",
    "EXECUTOR_BATCH_TIMEOUT_SECONDS",
    "EXECUTOR_CIRCUIT_BREAKER_THRESHOLD",
    "EXECUTOR_THROTTLE_ENABLED",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_REQUEST_TIMEOUT_SECONDS",
    "DEEPSEEK_MAX_RETRY_ATTEMPTS",
    "DEEPSEEK_RETRY_MAX_DELAY_SECONDS",
    "LANGSMITH_DOMAIN_TAGS",
    "LANGSMITH_TAGS",
]
