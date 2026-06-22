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


@dataclass(frozen=True)
class Settings:
    api_title: str
    api_key: Optional[str]
    deepseek_model: str
    deepseek_base_url: str
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
    debug_trace: bool
    debug_payloads: bool
    langsmith_hide_payloads: bool
    postgres_dsn: Optional[str]
    redis_url: Optional[str]

    @property
    def checkpoint_backend(self) -> str:
        configured = os.getenv("JARVIS_CHECKPOINT_BACKEND")
        if configured:
            return configured.strip().lower()
        return "postgres" if self.postgres_dsn else "memory"


def load_settings() -> Settings:
    return Settings(
        api_title=os.getenv("JARVIS_AGENT_API_TITLE", "Jarvis LangGraph Agent API"),
        api_key=os.getenv("LANGGRAPH_AGENT_API_KEY"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_request_timeout_seconds=_float_env("DEEPSEEK_REQUEST_TIMEOUT_SECONDS", 30.0),
        deepseek_max_retry_attempts=_int_env("DEEPSEEK_MAX_RETRY_ATTEMPTS", 3),
        deepseek_retry_max_delay_seconds=_float_env("DEEPSEEK_RETRY_MAX_DELAY_SECONDS", 8.0),
        todoist_rest_base_url=os.getenv(
            "TODOIST_REST_BASE_URL",
            "https://api.todoist.com/api/v1",
        ),
        allow_mutations=_bool_env("JARVIS_ALLOW_MUTATIONS", True),
        max_agent_turns=_int_env("JARVIS_MAX_AGENT_TURNS", 8),
        todoist_max_retry_attempts=_int_env("TODOIST_MAX_RETRY_ATTEMPTS", 3),
        todoist_retry_total_timeout_seconds=_float_env(
            "TODOIST_RETRY_TOTAL_TIMEOUT_SECONDS",
            8.0,
        ),
        todoist_retry_base_delay_seconds=_float_env("TODOIST_RETRY_BASE_DELAY_SECONDS", 0.5),
        todoist_retry_max_delay_seconds=_float_env("TODOIST_RETRY_MAX_DELAY_SECONDS", 4.0),
        debug_trace=_bool_env("JARVIS_DEBUG", True),
        debug_payloads=_bool_env("JARVIS_DEBUG_PAYLOADS", True),
        # Raw prompts/outputs are hidden from LangSmith by default. Set
        # JARVIS_TRACE_PAYLOADS=1 to temporarily capture full payloads for debugging.
        langsmith_hide_payloads=not _bool_env("JARVIS_TRACE_PAYLOADS", False),
        postgres_dsn=os.getenv("JARVIS_POSTGRES_DSN") or os.getenv("DATABASE_URL"),
        redis_url=os.getenv("JARVIS_REDIS_URL") or os.getenv("REDIS_URL"),
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
