"""Environment-backed settings for the Jarvis agent API."""

import math
import os
from dataclasses import dataclass, field
from typing import Optional

from agents.agent_api.app.llm.provider import (
    DeepSeekProfile,
    LLMProvider,
    LLMProviderError,
    LLMProviderProfile,
    LLMRole,
    OpenAIChatProfile,
    OpenAIResponsesProfile,
    validate_model_for_provider,
    validate_profile,
    validate_reasoning_for_profile,
)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()


# Authoritative Python-side turn budget. Outer Node/Telegraf watchdog defaults
# live in src/config/turn-timeout.config.ts and are verified against this value.
DEFAULT_RUN_DEADLINE_SECONDS = 150.0


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


def _non_negative_int_env(name: str, default: int) -> int:
    value = _int_env(name, default)
    if value < 0:
        raise ValueError(f"{name} must be zero or greater.")
    return value


def _positive_float_env(name: str, default: float) -> float:
    value = _float_env(name, default)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and greater than zero.")
    return value


def _non_empty_env(name: str, default: str) -> str:
    raw_value = os.getenv(name)
    value = default if raw_value is None else raw_value
    normalized = value.strip()
    if not normalized:
        raise LLMProviderError("configuration", f"{name} must not be empty.")
    return normalized


def _optional_non_empty_env(name: str) -> Optional[str]:
    raw_value = os.getenv(name)
    if raw_value is None:
        return None
    normalized = raw_value.strip()
    if not normalized:
        raise LLMProviderError("configuration", f"{name} must not be empty.")
    return normalized


def _required_non_empty_env(name: str) -> str:
    value = _optional_non_empty_env(name)
    if value is None:
        raise LLMProviderError("configuration", f"{name} is required.")
    return value


def _unused_secret_env(name: str) -> Optional[str]:
    """Read an inactive provider secret without requiring or validating it."""

    raw_value = os.getenv(name)
    if raw_value is None:
        return None
    return raw_value.strip() or None


@dataclass(frozen=True)
class Settings:
    api_title: str
    api_key: Optional[str] = field(repr=False)
    llm_provider: LLMProvider
    llm_safety_identifier_secret: Optional[str] = field(repr=False)
    orchestrator_llm: LLMProviderProfile
    router_llm: LLMProviderProfile
    summarizer_llm: LLMProviderProfile
    deepseek_model: str
    deepseek_complex_model: str
    deepseek_base_url: str
    deepseek_reasoning_effort: str
    deepseek_request_timeout_seconds: float
    deepseek_max_retry_attempts: int
    deepseek_retry_max_delay_seconds: float
    deepseek_sdk_max_retries: int
    deepseek_max_tokens: int
    deepseek_thinking_enabled: bool
    openai_api_key: Optional[str] = field(repr=False)
    openai_model: str
    openai_complex_model: str
    openai_vision_model: str
    openai_base_url: str
    openai_max_completion_tokens: int
    openai_reasoning_effort: str
    openai_request_timeout_seconds: float
    openai_max_retry_attempts: int
    openai_retry_max_delay_seconds: float
    openai_sdk_max_retries: int
    # Query router (pre-orchestrator domain classifier). Opt-in via router_enabled;
    # tool_selector selects the strategy ("static" | "router"). The
    # router reuses the DeepSeek OpenAI-compatible endpoint but with tighter,
    # non-critical retry/timeout budgets since it is never a hard-failure path.
    router_enabled: bool
    router_model: str
    router_base_url: str
    router_api_key: Optional[str] = field(repr=False)
    router_reasoning_effort: str
    router_request_timeout_seconds: float
    router_max_retry_attempts: int
    router_retry_max_delay_seconds: float
    tool_selector: str
    todoist_rest_base_url: str
    allow_mutations: bool
    max_agent_turns: int
    max_concurrent_runs: int
    run_deadline_seconds: float
    todoist_max_retry_attempts: int
    todoist_retry_total_timeout_seconds: float
    todoist_retry_base_delay_seconds: float
    todoist_retry_max_delay_seconds: float
    todoist_http_timeout_seconds: float
    todoist_http_max_keepalive_connections: int
    todoist_http_max_connections: int
    confirm_bulk_threshold: int
    summarizer_model: str
    summarize_threshold: int
    summarizer_request_timeout_seconds: float
    summarizer_max_retry_attempts: int
    summarizer_retry_max_delay_seconds: float
    summarizer_min_id_coverage: float
    summarizer_max_tokens_ceiling: int
    summarizer_max_concurrency: int
    executor_max_workers: int
    executor_batch_timeout_seconds: float
    executor_circuit_breaker_threshold: int
    executor_throttle_enabled: bool
    user_timezone: str
    debug_trace: bool
    debug_payloads: bool
    langsmith_hide_payloads: bool
    postgres_dsn: Optional[str]
    checkpoint_backend: str
    run_checkpoint_setup: bool
    idempotency_request_ttl_seconds: int
    idempotency_operation_ttl_seconds: int
    idempotency_lease_seconds: int
    idempotency_wait_seconds: float
    idempotency_poll_interval_seconds: float
    idempotency_cleanup_interval_seconds: int
    # Model router: dynamically selects model + reasoning effort per turn using
    # fused query complexity, domain count, and uncertainty signals.
    model_router_enabled: bool
    model_router_default_model: str
    model_router_default_reasoning: str
    model_router_default_timeout_seconds: float
    model_router_simple_reasoning: str
    model_router_complex_model: str
    model_router_complex_reasoning: str
    model_router_complex_timeout_seconds: float
    model_router_multi_domain_reasoning: str
    model_router_multi_domain_timeout_seconds: float

    def profile_for_role(self, role: LLMRole | str) -> LLMProviderProfile:
        resolved_role = role if isinstance(role, LLMRole) else LLMRole(role)
        if resolved_role is LLMRole.ORCHESTRATOR:
            return self.orchestrator_llm
        if resolved_role is LLMRole.ROUTER:
            return self.router_llm
        return self.summarizer_llm


def _role_provider(name: str, inherited: LLMProvider) -> LLMProvider:
    raw_value = os.getenv(name)
    if raw_value is None:
        return inherited
    return LLMProvider.parse(raw_value, setting_name=name)


def _role_model(name: str, *, provider: LLMProvider, default: str) -> str:
    return validate_model_for_provider(provider, _non_empty_env(name, default))


def _profile_for_role(
    role: LLMRole,
    provider: LLMProvider,
    *,
    api_key: str,
    deepseek_model: str,
    deepseek_base_url: str,
    deepseek_max_tokens: int,
    deepseek_request_timeout_seconds: float,
    deepseek_max_retry_attempts: int,
    deepseek_retry_max_delay_seconds: float,
    deepseek_sdk_max_retries: int,
    deepseek_reasoning_effort: str,
    deepseek_thinking_enabled: bool,
    openai_model: str,
    openai_base_url: str,
    openai_max_completion_tokens: int,
    openai_request_timeout_seconds: float,
    openai_max_retry_attempts: int,
    openai_retry_max_delay_seconds: float,
    openai_sdk_max_retries: int,
    openai_reasoning_effort: str,
    router_model: Optional[str],
    router_reasoning_effort: str,
    router_request_timeout_seconds: float,
    router_max_retry_attempts: int,
    router_retry_max_delay_seconds: float,
    summarizer_model: Optional[str],
    summarizer_request_timeout_seconds: float,
    summarizer_max_retry_attempts: int,
    summarizer_retry_max_delay_seconds: float,
    summarizer_max_tokens_ceiling: int,
) -> LLMProviderProfile:
    provider_model = deepseek_model if provider is LLMProvider.DEEPSEEK else openai_model
    model_override = router_model if role is LLMRole.ROUTER else summarizer_model
    model = validate_model_for_provider(provider, model_override or provider_model)
    if role is LLMRole.ROUTER:
        max_output_tokens = 400
        request_timeout_seconds = router_request_timeout_seconds
        max_retry_attempts = router_max_retry_attempts
        retry_max_delay_seconds = router_retry_max_delay_seconds
        reasoning_effort = router_reasoning_effort
        thinking_enabled = reasoning_effort.strip().lower() != "off"
    elif role is LLMRole.SUMMARIZER:
        max_output_tokens = summarizer_max_tokens_ceiling
        request_timeout_seconds = summarizer_request_timeout_seconds
        max_retry_attempts = summarizer_max_retry_attempts
        retry_max_delay_seconds = summarizer_retry_max_delay_seconds
        reasoning_effort = "off"
        thinking_enabled = False
    else:
        max_output_tokens = (
            deepseek_max_tokens
            if provider is LLMProvider.DEEPSEEK
            else openai_max_completion_tokens
        )
        request_timeout_seconds = (
            deepseek_request_timeout_seconds
            if provider is LLMProvider.DEEPSEEK
            else openai_request_timeout_seconds
        )
        max_retry_attempts = (
            deepseek_max_retry_attempts
            if provider is LLMProvider.DEEPSEEK
            else openai_max_retry_attempts
        )
        retry_max_delay_seconds = (
            deepseek_retry_max_delay_seconds
            if provider is LLMProvider.DEEPSEEK
            else openai_retry_max_delay_seconds
        )
        reasoning_effort = (
            deepseek_reasoning_effort
            if provider is LLMProvider.DEEPSEEK
            else openai_reasoning_effort
        )
        thinking_enabled = deepseek_thinking_enabled
    if provider is LLMProvider.DEEPSEEK:
        return validate_profile(
            DeepSeekProfile(
                api_key=api_key,
                base_url=deepseek_base_url,
                model=model,
                max_output_tokens=max_output_tokens,
                request_timeout_seconds=request_timeout_seconds,
                max_retry_attempts=max_retry_attempts,
                retry_max_delay_seconds=retry_max_delay_seconds,
                sdk_max_retries=deepseek_sdk_max_retries,
                reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
                thinking_enabled=thinking_enabled,
            )
        )
    openai_profile_type = (
        OpenAIResponsesProfile
        if role is LLMRole.ORCHESTRATOR
        else OpenAIChatProfile
    )
    return validate_profile(
        openai_profile_type(
            api_key=api_key,
            base_url=openai_base_url,
            model=model,
            max_output_tokens=(
                max_output_tokens
                if role is not LLMRole.ORCHESTRATOR
                else openai_max_completion_tokens
            ),
            request_timeout_seconds=(
                request_timeout_seconds
                if role is not LLMRole.ORCHESTRATOR
                else openai_request_timeout_seconds
            ),
            max_retry_attempts=(
                max_retry_attempts
                if role is not LLMRole.ORCHESTRATOR
                else openai_max_retry_attempts
            ),
            retry_max_delay_seconds=(
                retry_max_delay_seconds
                if role is not LLMRole.ORCHESTRATOR
                else openai_retry_max_delay_seconds
            ),
            sdk_max_retries=openai_sdk_max_retries,
            **(
                {"reasoning_effort": reasoning_effort}
                if role is LLMRole.ORCHESTRATOR
                else {}
            ),
        )
    )


def load_settings() -> Settings:
    llm_provider = LLMProvider.parse(os.getenv("LLM_PROVIDER", "openai"))
    router_provider = _role_provider("ROUTER_PROVIDER", llm_provider)
    summarizer_provider = _role_provider("SUMMARIZER_PROVIDER", llm_provider)
    router_enabled = _bool_env("ROUTER_ENABLED", True)
    tool_selector = _non_empty_env("TOOL_SELECTOR", "router").lower()
    router_is_active = router_enabled and tool_selector == "router"
    router_profile_provider = router_provider if router_is_active else llm_provider
    active_providers = {llm_provider, summarizer_provider}
    if router_is_active:
        active_providers.add(router_provider)
    deepseek_api_key = (
        _required_non_empty_env("DEEPSEEK_API_KEY")
        if LLMProvider.DEEPSEEK in active_providers
        else _unused_secret_env("DEEPSEEK_API_KEY")
    )
    openai_api_key = (
        _required_non_empty_env("OPENAI_API_KEY")
        if LLMProvider.OPENAI in active_providers
        else _unused_secret_env("OPENAI_API_KEY")
    )
    llm_safety_identifier_secret = (
        _required_non_empty_env("LLM_SAFETY_IDENTIFIER_SECRET")
        if LLMProvider.OPENAI in active_providers
        else _unused_secret_env("LLM_SAFETY_IDENTIFIER_SECRET")
    )
    deepseek_model = validate_model_for_provider(
        LLMProvider.DEEPSEEK,
        _non_empty_env("DEEPSEEK_MODEL", "deepseek-v4-flash"),
    )
    deepseek_complex_model = validate_model_for_provider(
        LLMProvider.DEEPSEEK,
        _non_empty_env("DEEPSEEK_COMPLEX_MODEL", "deepseek-v4-pro"),
    )
    deepseek_base_url = _non_empty_env(
        "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
    )
    deepseek_reasoning_effort = _non_empty_env(
        "DEEPSEEK_REASONING_EFFORT", "max"
    ).lower()
    deepseek_request_timeout_seconds = _positive_float_env(
        "DEEPSEEK_REQUEST_TIMEOUT_SECONDS",
        30.0,
    )
    deepseek_max_retry_attempts = _positive_int_env(
        "DEEPSEEK_MAX_RETRY_ATTEMPTS", 3
    )
    deepseek_retry_max_delay_seconds = _positive_float_env(
        "DEEPSEEK_RETRY_MAX_DELAY_SECONDS", 8.0
    )
    deepseek_sdk_max_retries = _non_negative_int_env(
        "DEEPSEEK_SDK_MAX_RETRIES", 0
    )
    deepseek_max_tokens = _positive_int_env("DEEPSEEK_MAX_TOKENS", 13000)
    deepseek_thinking_enabled = _bool_env("DEEPSEEK_THINKING_ENABLED", True)

    openai_model = validate_model_for_provider(
        LLMProvider.OPENAI,
        _non_empty_env("OPENAI_MODEL", "gpt-5.6-luna"),
    )
    openai_complex_model = validate_model_for_provider(
        LLMProvider.OPENAI,
        _non_empty_env("OPENAI_COMPLEX_MODEL", "gpt-5.6-luna"),
    )
    openai_vision_model = validate_model_for_provider(
        LLMProvider.OPENAI,
        _non_empty_env("OPENAI_VISION_MODEL", "gpt-5.6-luna"),
    )
    openai_base_url = _non_empty_env(
        "OPENAI_BASE_URL", "https://api.openai.com/v1"
    )
    openai_max_completion_tokens = _positive_int_env(
        "OPENAI_MAX_COMPLETION_TOKENS", 20000
    )
    openai_request_timeout_seconds = _positive_float_env(
        "OPENAI_REQUEST_TIMEOUT_SECONDS", 60.0
    )
    openai_max_retry_attempts = _positive_int_env(
        "OPENAI_MAX_RETRY_ATTEMPTS", 3
    )
    openai_retry_max_delay_seconds = _positive_float_env(
        "OPENAI_RETRY_MAX_DELAY_SECONDS", 8.0
    )
    openai_sdk_max_retries = _non_negative_int_env("OPENAI_SDK_MAX_RETRIES", 0)
    openai_reasoning_effort = _non_empty_env(
        "OPENAI_REASONING_EFFORT", "medium"
    ).lower()

    router_model_override = _optional_non_empty_env("ROUTER_MODEL")
    router_reasoning_effort = _non_empty_env(
        "ROUTER_REASONING_EFFORT", "off"
    ).lower()
    router_request_timeout_seconds = _positive_float_env(
        "ROUTER_REQUEST_TIMEOUT_SECONDS", 5.0
    )
    router_max_retry_attempts = _positive_int_env("ROUTER_MAX_RETRY_ATTEMPTS", 2)
    router_retry_max_delay_seconds = _positive_float_env(
        "ROUTER_RETRY_MAX_DELAY_SECONDS", 2.0
    )

    summarizer_model_override = _optional_non_empty_env("JARVIS_SUMMARIZER_MODEL")
    summarizer_request_timeout_seconds = _positive_float_env(
        "JARVIS_SUMMARIZER_TIMEOUT_SECONDS", 30.0
    )
    summarizer_max_retry_attempts = _positive_int_env(
        "JARVIS_SUMMARIZER_MAX_RETRY_ATTEMPTS", 3
    )
    summarizer_retry_max_delay_seconds = _positive_float_env(
        "JARVIS_SUMMARIZER_RETRY_MAX_DELAY_SECONDS", 8.0
    )
    summarizer_max_tokens_ceiling = _positive_int_env(
        "JARVIS_SUMMARIZER_MAX_TOKENS_CEILING", 15000
    )

    def role_profile(role: LLMRole, provider: LLMProvider) -> LLMProviderProfile:
        api_key = deepseek_api_key if provider is LLMProvider.DEEPSEEK else openai_api_key
        assert api_key is not None
        if (
            role is LLMRole.ROUTER
            and router_is_active
            and provider is LLMProvider.OPENAI
        ):
            configured_reasoning = os.getenv("ROUTER_REASONING_EFFORT")
            if configured_reasoning is not None and configured_reasoning.strip().lower() != "none":
                raise LLMProviderError(
                    "configuration",
                    "ROUTER_REASONING_EFFORT must be 'none' for OpenAI Chat Completions.",
                    provider=provider,
                    model=openai_model,
                )
        return _profile_for_role(
            role,
            provider,
            api_key=api_key,
            deepseek_model=deepseek_model,
            deepseek_base_url=deepseek_base_url,
            deepseek_max_tokens=deepseek_max_tokens,
            deepseek_request_timeout_seconds=deepseek_request_timeout_seconds,
            deepseek_max_retry_attempts=deepseek_max_retry_attempts,
            deepseek_retry_max_delay_seconds=deepseek_retry_max_delay_seconds,
            deepseek_sdk_max_retries=deepseek_sdk_max_retries,
            deepseek_reasoning_effort=deepseek_reasoning_effort,
            deepseek_thinking_enabled=deepseek_thinking_enabled,
            openai_model=openai_model,
            openai_base_url=openai_base_url,
            openai_max_completion_tokens=openai_max_completion_tokens,
            openai_request_timeout_seconds=openai_request_timeout_seconds,
            openai_max_retry_attempts=openai_max_retry_attempts,
            openai_retry_max_delay_seconds=openai_retry_max_delay_seconds,
            openai_sdk_max_retries=openai_sdk_max_retries,
            openai_reasoning_effort=openai_reasoning_effort,
            router_model=router_model_override if router_is_active else None,
            router_reasoning_effort=(
                router_reasoning_effort if router_is_active else "off"
            ),
            router_request_timeout_seconds=router_request_timeout_seconds,
            router_max_retry_attempts=router_max_retry_attempts,
            router_retry_max_delay_seconds=router_retry_max_delay_seconds,
            summarizer_model=summarizer_model_override,
            summarizer_request_timeout_seconds=summarizer_request_timeout_seconds,
            summarizer_max_retry_attempts=summarizer_max_retry_attempts,
            summarizer_retry_max_delay_seconds=summarizer_retry_max_delay_seconds,
            summarizer_max_tokens_ceiling=summarizer_max_tokens_ceiling,
        )

    orchestrator_llm = role_profile(LLMRole.ORCHESTRATOR, llm_provider)
    router_llm = role_profile(LLMRole.ROUTER, router_profile_provider)
    summarizer_llm = role_profile(LLMRole.SUMMARIZER, summarizer_provider)
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
    todoist_http_timeout_seconds = _positive_float_env(
        "TODOIST_HTTP_TIMEOUT_SECONDS",
        30.0,
    )
    todoist_http_max_keepalive_connections = _positive_int_env(
        "TODOIST_HTTP_MAX_KEEPALIVE_CONNECTIONS",
        10,
    )
    todoist_http_max_connections = _positive_int_env(
        "TODOIST_HTTP_MAX_CONNECTIONS",
        20,
    )
    if todoist_http_max_keepalive_connections > todoist_http_max_connections:
        raise ValueError(
            "TODOIST_HTTP_MAX_KEEPALIVE_CONNECTIONS must not exceed "
            "TODOIST_HTTP_MAX_CONNECTIONS."
        )

    return Settings(
        api_title=os.getenv("JARVIS_AGENT_API_TITLE", "Jarvis LangGraph Agent API"),
        api_key=os.getenv("LANGGRAPH_AGENT_API_KEY"),
        llm_provider=llm_provider,
        llm_safety_identifier_secret=llm_safety_identifier_secret,
        orchestrator_llm=orchestrator_llm,
        router_llm=router_llm,
        summarizer_llm=summarizer_llm,
        deepseek_model=deepseek_model,
        deepseek_complex_model=deepseek_complex_model,
        deepseek_base_url=deepseek_base_url,
        deepseek_reasoning_effort=deepseek_reasoning_effort,
        deepseek_request_timeout_seconds=deepseek_request_timeout_seconds,
        deepseek_max_retry_attempts=deepseek_max_retry_attempts,
        deepseek_retry_max_delay_seconds=deepseek_retry_max_delay_seconds,
        deepseek_sdk_max_retries=deepseek_sdk_max_retries,
        deepseek_max_tokens=deepseek_max_tokens,
        deepseek_thinking_enabled=deepseek_thinking_enabled,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        openai_complex_model=openai_complex_model,
        openai_vision_model=openai_vision_model,
        openai_base_url=openai_base_url,
        openai_max_completion_tokens=openai_max_completion_tokens,
        openai_request_timeout_seconds=openai_request_timeout_seconds,
        openai_max_retry_attempts=openai_max_retry_attempts,
        openai_retry_max_delay_seconds=openai_retry_max_delay_seconds,
        openai_sdk_max_retries=openai_sdk_max_retries,
        openai_reasoning_effort=openai_reasoning_effort,
        # Router defaults: reuse the DeepSeek endpoint/key, reasoning off, and a
        # modest budget (per-attempt 5s timeout, 2 attempts) — the router is
        # non-critical and always degrades to the static selector on failure.
        # Enabled by default (paired with tool_selector="router" below); set
        # ROUTER_ENABLED=false to fall back to loading every domain each turn.
        router_enabled=router_enabled,
        router_model=router_llm.model,
        router_base_url=router_llm.base_url,
        router_api_key=router_llm.api_key,
        router_reasoning_effort=router_llm.reasoning_effort,
        router_request_timeout_seconds=router_request_timeout_seconds,
        router_max_retry_attempts=router_max_retry_attempts,
        router_retry_max_delay_seconds=router_retry_max_delay_seconds,
        tool_selector=tool_selector,
        todoist_rest_base_url=os.getenv(
            "TODOIST_REST_BASE_URL",
            "https://api.todoist.com/api/v1",
        ),
        allow_mutations=_bool_env("JARVIS_ALLOW_MUTATIONS", True),
        max_agent_turns=_int_env("JARVIS_MAX_AGENT_TURNS", 20),
        max_concurrent_runs=_positive_int_env("JARVIS_MAX_CONCURRENT_RUNS", 12),
        run_deadline_seconds=_positive_float_env(
            "JARVIS_RUN_DEADLINE_SECONDS",
            DEFAULT_RUN_DEADLINE_SECONDS,
        ),
        todoist_max_retry_attempts=_int_env("TODOIST_MAX_RETRY_ATTEMPTS", 3),
        todoist_retry_total_timeout_seconds=_positive_float_env(
            "TODOIST_RETRY_TOTAL_TIMEOUT_SECONDS",
            8.0,
        ),
        todoist_retry_base_delay_seconds=_float_env("TODOIST_RETRY_BASE_DELAY_SECONDS", 0.5),
        todoist_retry_max_delay_seconds=_float_env("TODOIST_RETRY_MAX_DELAY_SECONDS", 4.0),
        todoist_http_timeout_seconds=todoist_http_timeout_seconds,
        todoist_http_max_keepalive_connections=todoist_http_max_keepalive_connections,
        todoist_http_max_connections=todoist_http_max_connections,
        confirm_bulk_threshold=_int_env("JARVIS_CONFIRM_BULK_THRESHOLD", 5),
        summarizer_model=summarizer_llm.model,
        summarize_threshold=_int_env("JARVIS_SUMMARIZE_THRESHOLD", 50),
        summarizer_request_timeout_seconds=summarizer_request_timeout_seconds,
        summarizer_max_retry_attempts=summarizer_max_retry_attempts,
        summarizer_retry_max_delay_seconds=summarizer_retry_max_delay_seconds,
        summarizer_min_id_coverage=_float_env("JARVIS_SUMMARIZER_MIN_ID_COVERAGE", 0.7),
        summarizer_max_tokens_ceiling=summarizer_max_tokens_ceiling,
        summarizer_max_concurrency=_positive_int_env(
            "JARVIS_SUMMARIZER_MAX_CONCURRENCY",
            4,
        ),
        executor_max_workers=_int_env("JARVIS_EXECUTOR_MAX_WORKERS", 5),
        executor_batch_timeout_seconds=_float_env("JARVIS_EXECUTOR_BATCH_TIMEOUT_SECONDS", 45.0),
        executor_circuit_breaker_threshold=_int_env("JARVIS_EXECUTOR_CIRCUIT_BREAKER_THRESHOLD", 2),
        executor_throttle_enabled=_bool_env("JARVIS_EXECUTOR_THROTTLE_ENABLED", True),
        user_timezone=os.getenv("JARVIS_USER_TIMEZONE", "Asia/Singapore"),
        debug_trace=_bool_env("JARVIS_DEBUG", True),
        debug_payloads=_bool_env("JARVIS_DEBUG_PAYLOADS", True),
        # Raw prompts/outputs are captured in LangSmith by default. Set
        # JARVIS_TRACE_PAYLOADS=0 for deployments that must hide full payloads.
        langsmith_hide_payloads=not _bool_env("JARVIS_TRACE_PAYLOADS", True),
        postgres_dsn=postgres_dsn,
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
        model_router_enabled=_bool_env("MODEL_ROUTER_ENABLED", True),
        model_router_default_model=_role_model(
            "MODEL_ROUTER_DEFAULT_MODEL",
            provider=llm_provider,
            default=orchestrator_llm.model,
        ),
        model_router_default_reasoning=validate_reasoning_for_profile(
            orchestrator_llm,
            os.getenv("MODEL_ROUTER_DEFAULT_REASONING")
            or (
                "high"
                if llm_provider is LLMProvider.DEEPSEEK
                else openai_reasoning_effort
            ),
        ),
        model_router_default_timeout_seconds=_positive_float_env(
            "MODEL_ROUTER_DEFAULT_TIMEOUT_SECONDS",
            orchestrator_llm.request_timeout_seconds,
        ),
        model_router_simple_reasoning=validate_reasoning_for_profile(
            orchestrator_llm,
            os.getenv("MODEL_ROUTER_SIMPLE_REASONING") or "low",
        ),
        model_router_complex_model=_role_model(
            "MODEL_ROUTER_COMPLEX_MODEL",
            provider=llm_provider,
            default=(
                deepseek_complex_model
                if llm_provider is LLMProvider.DEEPSEEK
                else openai_complex_model
            ),
        ),
        model_router_complex_reasoning=validate_reasoning_for_profile(
            orchestrator_llm,
            os.getenv("MODEL_ROUTER_COMPLEX_REASONING")
            or (
                "high"
                if llm_provider is LLMProvider.DEEPSEEK
                else openai_reasoning_effort
            ),
        ),
        model_router_complex_timeout_seconds=_positive_float_env(
            "MODEL_ROUTER_COMPLEX_TIMEOUT_SECONDS",
            90.0,
        ),
        model_router_multi_domain_reasoning=validate_reasoning_for_profile(
            orchestrator_llm,
            os.getenv("MODEL_ROUTER_MULTI_DOMAIN_REASONING")
            or (
                "high"
                if llm_provider is LLMProvider.DEEPSEEK
                else openai_reasoning_effort
            ),
        ),
        model_router_multi_domain_timeout_seconds=_positive_float_env(
            "MODEL_ROUTER_MULTI_DOMAIN_TIMEOUT_SECONDS",
            60.0,
        ),
    )


def apply_langsmith_env_defaults(active_settings: Settings) -> None:
    """Set LangSmith payload-privacy env vars before any tracer initializes.

    Raw inputs (prompts, tool args) and outputs (completions, reasoning content)
    are captured by default. JARVIS_TRACE_PAYLOADS=0 enables privacy-preserving
    tracing that retains safe metadata and tags while hiding full payloads.
    """

    if active_settings.langsmith_hide_payloads:
        os.environ.setdefault("LANGSMITH_HIDE_INPUTS", "true")
        os.environ.setdefault("LANGSMITH_HIDE_OUTPUTS", "true")
    else:
        os.environ["LANGSMITH_HIDE_INPUTS"] = "false"
        os.environ["LANGSMITH_HIDE_OUTPUTS"] = "false"


settings = load_settings()
ORCHESTRATOR_LLM = settings.orchestrator_llm
ROUTER_LLM = settings.router_llm
SUMMARIZER_LLM = settings.summarizer_llm
apply_langsmith_env_defaults(settings)


__all__ = [
    "DEFAULT_RUN_DEADLINE_SECONDS",
    "ORCHESTRATOR_LLM",
    "ROUTER_LLM",
    "SUMMARIZER_LLM",
    "Settings",
    "apply_langsmith_env_defaults",
    "load_settings",
    "settings",
]
