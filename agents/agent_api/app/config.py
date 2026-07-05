"""Environment-backed settings for the Jarvis agent API."""

import os
from dataclasses import dataclass
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {"0", "false", "no", "off", ""}


def _int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return int(raw_value)


def _float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return float(raw_value)


def _positive_int_env(name: str, default: int) -> int:
    value = _int_env(name, default)
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def _positive_float_env(name: str, default: float) -> float:
    value = _float_env(name, default)
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


@dataclass(frozen=True)
class Settings:
    api_title: str
    api_key: Optional[str]
    deepseek_model: str
    deepseek_base_url: str
    deepseek_reasoning_effort: str
    deepseek_request_timeout_seconds: float
    deepseek_max_retry_attempts: int
    deepseek_retry_max_delay_seconds: float
    todoist_rest_base_url: str
    allow_mutations: bool
    max_agent_turns: int
    todoist_max_retry_attempts: int
    todoist_retry_total_timeout_seconds: float
    todoist_retry_base_delay_seconds: float
    todoist_retry_max_delay_seconds: float
    confirm_bulk_threshold: int
    summarizer_model: str
    summarize_threshold: int
    summarizer_request_timeout_seconds: float
    summarizer_max_retry_attempts: int
    summarizer_retry_max_delay_seconds: float
    summarizer_min_id_coverage: float
    summarizer_max_tokens_ceiling: int
    executor_max_workers: int
    executor_batch_timeout_seconds: float
    executor_circuit_breaker_threshold: int
    executor_throttle_enabled: bool
    user_timezone: str
    debug_trace: bool
    debug_payloads: bool
    langsmith_hide_payloads: bool
    postgres_dsn: Optional[str]
    redis_url: Optional[str]
    checkpoint_backend: str
    run_checkpoint_setup: bool
    idempotency_request_ttl_seconds: int
    idempotency_operation_ttl_seconds: int
    idempotency_lease_seconds: int
    idempotency_wait_seconds: float
    idempotency_poll_interval_seconds: float
    idempotency_cleanup_interval_seconds: int


def load_settings() -> Settings:
    postgres_dsn = os.getenv("JARVIS_POSTGRES_DSN") or os.getenv("DATABASE_URL")
    configured_checkpoint_backend = os.getenv("JARVIS_CHECKPOINT_BACKEND")
    checkpoint_backend = (
        configured_checkpoint_backend.strip().lower()
        if configured_checkpoint_backend and configured_checkpoint_backend.strip()
        else ("postgres" if postgres_dsn else "memory")
    )
    idempotency_request_ttl_seconds = _positive_int_env(
        "JARVIS_IDEMPOTENCY_REQUEST_TTL_SECONDS",
        14400,
    )
    idempotency_operation_ttl_seconds = _positive_int_env(
        "JARVIS_IDEMPOTENCY_OPERATION_TTL_SECONDS",
        7200,
    )
    idempotency_lease_seconds = _positive_int_env(
        "JARVIS_IDEMPOTENCY_LEASE_SECONDS",
        60,
    )
    if idempotency_lease_seconds > min(
        idempotency_request_ttl_seconds,
        idempotency_operation_ttl_seconds,
    ):
        raise ValueError(
            "JARVIS_IDEMPOTENCY_LEASE_SECONDS must not exceed either idempotency TTL."
        )

    return Settings(
        api_title=os.getenv("JARVIS_AGENT_API_TITLE", "Jarvis LangGraph Agent API"),
        api_key=os.getenv("LANGGRAPH_AGENT_API_KEY"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_reasoning_effort=os.getenv("DEEPSEEK_REASONING_EFFORT", "max"),
        deepseek_request_timeout_seconds=_float_env("DEEPSEEK_REQUEST_TIMEOUT_SECONDS", 30.0),
        deepseek_max_retry_attempts=_int_env("DEEPSEEK_MAX_RETRY_ATTEMPTS", 3),
        deepseek_retry_max_delay_seconds=_float_env("DEEPSEEK_RETRY_MAX_DELAY_SECONDS", 8.0),
        todoist_rest_base_url=os.getenv(
            "TODOIST_REST_BASE_URL",
            "https://api.todoist.com/api/v1",
        ),
        allow_mutations=_bool_env("JARVIS_ALLOW_MUTATIONS", True),
        max_agent_turns=_int_env("JARVIS_MAX_AGENT_TURNS", 20),
        todoist_max_retry_attempts=_int_env("TODOIST_MAX_RETRY_ATTEMPTS", 3),
        todoist_retry_total_timeout_seconds=_float_env(
            "TODOIST_RETRY_TOTAL_TIMEOUT_SECONDS",
            8.0,
        ),
        todoist_retry_base_delay_seconds=_float_env("TODOIST_RETRY_BASE_DELAY_SECONDS", 0.5),
        todoist_retry_max_delay_seconds=_float_env("TODOIST_RETRY_MAX_DELAY_SECONDS", 4.0),
        confirm_bulk_threshold=_int_env("JARVIS_CONFIRM_BULK_THRESHOLD", 5),
        summarizer_model=os.getenv(
            "JARVIS_SUMMARIZER_MODEL",
            os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        ),
        summarize_threshold=_int_env("JARVIS_SUMMARIZE_THRESHOLD", 50),
        summarizer_request_timeout_seconds=_float_env("JARVIS_SUMMARIZER_TIMEOUT_SECONDS", 30.0),
        summarizer_max_retry_attempts=_int_env("JARVIS_SUMMARIZER_MAX_RETRY_ATTEMPTS", 3),
        summarizer_retry_max_delay_seconds=_float_env("JARVIS_SUMMARIZER_RETRY_MAX_DELAY_SECONDS", 8.0),
        summarizer_min_id_coverage=_float_env("JARVIS_SUMMARIZER_MIN_ID_COVERAGE", 0.7),
        summarizer_max_tokens_ceiling=_int_env("JARVIS_SUMMARIZER_MAX_TOKENS_CEILING", 15000),
        executor_max_workers=_int_env("JARVIS_EXECUTOR_MAX_WORKERS", 5),
        executor_batch_timeout_seconds=_float_env("JARVIS_EXECUTOR_BATCH_TIMEOUT_SECONDS", 45.0),
        executor_circuit_breaker_threshold=_int_env("JARVIS_EXECUTOR_CIRCUIT_BREAKER_THRESHOLD", 2),
        executor_throttle_enabled=_bool_env("JARVIS_EXECUTOR_THROTTLE_ENABLED", True),
        user_timezone=os.getenv("JARVIS_USER_TIMEZONE", "Asia/Singapore"),
        debug_trace=_bool_env("JARVIS_DEBUG", True),
        debug_payloads=_bool_env("JARVIS_DEBUG_PAYLOADS", True),
        # Raw prompts/outputs are hidden from LangSmith by default. Set
        # JARVIS_TRACE_PAYLOADS=1 to temporarily capture full payloads for debugging.
        langsmith_hide_payloads=not _bool_env("JARVIS_TRACE_PAYLOADS", True),
        postgres_dsn=postgres_dsn,
        redis_url=os.getenv("JARVIS_REDIS_URL") or os.getenv("REDIS_URL"),
        checkpoint_backend=checkpoint_backend,
        run_checkpoint_setup=_bool_env("JARVIS_RUN_CHECKPOINT_SETUP", False),
        idempotency_request_ttl_seconds=idempotency_request_ttl_seconds,
        idempotency_operation_ttl_seconds=idempotency_operation_ttl_seconds,
        idempotency_lease_seconds=idempotency_lease_seconds,
        idempotency_wait_seconds=_positive_float_env(
            "JARVIS_IDEMPOTENCY_WAIT_SECONDS",
            30.0,
        ),
        idempotency_poll_interval_seconds=_positive_float_env(
            "JARVIS_IDEMPOTENCY_POLL_INTERVAL_SECONDS",
            0.1,
        ),
        idempotency_cleanup_interval_seconds=_positive_int_env(
            "JARVIS_IDEMPOTENCY_CLEANUP_INTERVAL_SECONDS",
            1800,
        ),
    )


def apply_langsmith_env_defaults(active_settings: Settings) -> None:
    """Set LangSmith payload-privacy env vars before any tracer initializes.

    By default raw inputs (prompts, tool args) and outputs (completions, reasoning
    content) are hidden from LangSmith while safe metadata/tags are retained.
    When JARVIS_TRACE_PAYLOADS opts in, payload capture is enabled explicitly.
    """

    if active_settings.langsmith_hide_payloads:
        os.environ.setdefault("LANGSMITH_HIDE_INPUTS", "true")
        os.environ.setdefault("LANGSMITH_HIDE_OUTPUTS", "true")
    else:
        os.environ["LANGSMITH_HIDE_INPUTS"] = "false"
        os.environ["LANGSMITH_HIDE_OUTPUTS"] = "false"


settings = load_settings()
apply_langsmith_env_defaults(settings)
