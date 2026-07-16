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
SUMMARIZER_MAX_CONCURRENCY = settings.summarizer_max_concurrency

EXECUTOR_MAX_WORKERS = settings.executor_max_workers
EXECUTOR_BATCH_TIMEOUT_SECONDS = settings.executor_batch_timeout_seconds
EXECUTOR_CIRCUIT_BREAKER_THRESHOLD = settings.executor_circuit_breaker_threshold
EXECUTOR_THROTTLE_ENABLED = settings.executor_throttle_enabled

DEEPSEEK_MODEL = settings.deepseek_model
DEEPSEEK_BASE_URL = settings.deepseek_base_url
DEEPSEEK_REASONING_EFFORT = settings.deepseek_reasoning_effort
DEEPSEEK_REQUEST_TIMEOUT_SECONDS = settings.deepseek_request_timeout_seconds
DEEPSEEK_MAX_RETRY_ATTEMPTS = settings.deepseek_max_retry_attempts
DEEPSEEK_RETRY_MAX_DELAY_SECONDS = settings.deepseek_retry_max_delay_seconds
DEEPSEEK_SDK_MAX_RETRIES = settings.deepseek_sdk_max_retries
DEEPSEEK_MAX_TOKENS = settings.deepseek_max_tokens
DEEPSEEK_THINKING_ENABLED = settings.deepseek_thinking_enabled

# Query router (pre-orchestrator domain classifier). Opt-in and non-critical:
# see app/router/ and the RouterToolSelector. TOOL_SELECTOR chooses the strategy.
ROUTER_ENABLED = settings.router_enabled
ROUTER_MODEL = settings.router_model
ROUTER_BASE_URL = settings.router_base_url
ROUTER_API_KEY = settings.router_api_key
ROUTER_REASONING_EFFORT = settings.router_reasoning_effort
ROUTER_REQUEST_TIMEOUT_SECONDS = settings.router_request_timeout_seconds
ROUTER_MAX_RETRY_ATTEMPTS = settings.router_max_retry_attempts
ROUTER_RETRY_MAX_DELAY_SECONDS = settings.router_retry_max_delay_seconds
TOOL_SELECTOR = settings.tool_selector

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
    "SUMMARIZER_MAX_CONCURRENCY",
    "EXECUTOR_MAX_WORKERS",
    "EXECUTOR_BATCH_TIMEOUT_SECONDS",
    "EXECUTOR_CIRCUIT_BREAKER_THRESHOLD",
    "EXECUTOR_THROTTLE_ENABLED",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_REASONING_EFFORT",
    "DEEPSEEK_REQUEST_TIMEOUT_SECONDS",
    "DEEPSEEK_MAX_RETRY_ATTEMPTS",
    "DEEPSEEK_RETRY_MAX_DELAY_SECONDS",
    "DEEPSEEK_SDK_MAX_RETRIES",
    "DEEPSEEK_MAX_TOKENS",
    "DEEPSEEK_THINKING_ENABLED",
    "ROUTER_ENABLED",
    "ROUTER_MODEL",
    "ROUTER_BASE_URL",
    "ROUTER_API_KEY",
    "ROUTER_REASONING_EFFORT",
    "ROUTER_REQUEST_TIMEOUT_SECONDS",
    "ROUTER_MAX_RETRY_ATTEMPTS",
    "ROUTER_RETRY_MAX_DELAY_SECONDS",
    "TOOL_SELECTOR",
    "LANGSMITH_DOMAIN_TAGS",
    "LANGSMITH_TAGS",
]
