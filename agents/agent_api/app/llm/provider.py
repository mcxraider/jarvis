"""Validated, immutable LLM provider profiles.

Profiles describe provider capabilities, not merely credentials and a base URL.
Keeping the two Chat Completions dialects as distinct dataclasses prevents an
OpenAI request from accidentally inheriting DeepSeek-only fields.
"""

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, TypeAlias


class LLMProvider(str, Enum):
    DEEPSEEK = "deepseek"
    OPENAI = "openai"

    @classmethod
    def parse(cls, value: str, *, setting_name: str = "LLM_PROVIDER") -> "LLMProvider":
        normalized = value.strip().lower()
        if not normalized:
            raise LLMProviderError(
                "configuration",
                f"{setting_name} must not be empty.",
            )
        try:
            return cls(normalized)
        except ValueError as error:
            choices = ", ".join(provider.value for provider in cls)
            raise LLMProviderError(
                "configuration",
                f"{setting_name} must be one of: {choices}.",
            ) from error


class LLMRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    ROUTER = "router"
    SUMMARIZER = "summarizer"


LLMErrorCategory: TypeAlias = Literal[
    "configuration",
    "timeout",
    "rate_limited",
    "provider_unavailable",
    "auth",
    "invalid_response",
    "incompatible_checkpoint",
]


class LLMProviderError(RuntimeError):
    """A safe, provider-neutral boundary error."""

    def __init__(
        self,
        category: LLMErrorCategory,
        message: str,
        *,
        provider: LLMProvider | None = None,
        model: str | None = None,
    ) -> None:
        self.category = category
        self.provider = provider
        self.model = model
        super().__init__(message)


DeepSeekReasoningEffort: TypeAlias = Literal["off", "low", "high", "max"]
OpenAIReasoningEffort: TypeAlias = Literal[
    "none", "low", "medium", "high", "xhigh", "max"
]


@dataclass(frozen=True, kw_only=True)
class BaseLLMProfile:
    api_key: str = field(repr=False)
    base_url: str
    model: str
    max_output_tokens: int
    request_timeout_seconds: float
    max_retry_attempts: int
    retry_max_delay_seconds: float
    sdk_max_retries: int


@dataclass(frozen=True, kw_only=True)
class DeepSeekProfile(BaseLLMProfile):
    provider: Literal[LLMProvider.DEEPSEEK] = LLMProvider.DEEPSEEK
    reasoning_effort: DeepSeekReasoningEffort
    thinking_enabled: bool


@dataclass(frozen=True, kw_only=True)
class OpenAIChatProfile(BaseLLMProfile):
    provider: Literal[LLMProvider.OPENAI] = LLMProvider.OPENAI
    reasoning_effort: Literal["none"] = "none"


@dataclass(frozen=True, kw_only=True)
class OpenAIResponsesProfile(BaseLLMProfile):
    provider: Literal[LLMProvider.OPENAI] = LLMProvider.OPENAI
    reasoning_effort: OpenAIReasoningEffort = "medium"


LLMProviderProfile: TypeAlias = (
    DeepSeekProfile | OpenAIChatProfile | OpenAIResponsesProfile
)


def validate_model_for_provider(provider: LLMProvider, model: str) -> str:
    """Return a normalized model or reject a foreign-provider model."""

    normalized = model.strip()
    if not normalized:
        raise LLMProviderError("configuration", "LLM model must not be empty.")
    compatible = (
        normalized.startswith("deepseek-")
        if provider is LLMProvider.DEEPSEEK
        else normalized.startswith("gpt-")
    )
    if not compatible:
        raise LLMProviderError(
            "configuration",
            f"Model {normalized!r} is incompatible with provider {provider.value!r}.",
            provider=provider,
            model=normalized,
        )
    return normalized


def validate_model_for_profile(profile: LLMProviderProfile, model: str) -> str:
    normalized = validate_model_for_provider(profile.provider, model)
    if isinstance(profile, OpenAIResponsesProfile) and not normalized.startswith(
        "gpt-5.6"
    ):
        raise LLMProviderError(
            "configuration",
            "OpenAI Responses reasoning requires a GPT-5.6 model.",
            provider=profile.provider,
            model=normalized,
        )
    return normalized


def validate_reasoning_for_profile(
    profile: LLMProviderProfile,
    reasoning_effort: str | None,
) -> str:
    """Validate a request/user reasoning pin against endpoint capabilities."""

    if reasoning_effort is None:
        return profile.reasoning_effort
    normalized = reasoning_effort.strip().lower()
    if isinstance(profile, OpenAIChatProfile):
        if normalized != "none":
            raise LLMProviderError(
                "configuration",
                "OpenAI Chat Completions requires reasoning_effort='none'.",
                provider=profile.provider,
                model=profile.model,
            )
        return "none"
    if isinstance(profile, OpenAIResponsesProfile):
        if normalized not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise LLMProviderError(
                "configuration",
                "OpenAI Responses reasoning_effort must be one of: "
                "none, low, medium, high, xhigh, max.",
                provider=profile.provider,
                model=profile.model,
            )
        return normalized
    if normalized not in {"off", "low", "high", "max"}:
        raise LLMProviderError(
            "configuration",
            "DeepSeek reasoning_effort must be one of: off, low, high, max.",
            provider=profile.provider,
            model=profile.model,
        )
    return normalized


def require_vision_provider(
    profile: LLMProviderProfile, has_images: bool
) -> None:
    """Reject image input unless the provider supports vision."""
    if has_images and not isinstance(profile, OpenAIResponsesProfile):
        raise LLMProviderError(
            "configuration", "Photo input requires the OpenAI Responses provider."
        )


def validate_profile(profile: LLMProviderProfile) -> LLMProviderProfile:
    """Validate fields shared by profiles constructed outside configuration."""

    if not profile.api_key.strip():
        raise LLMProviderError("configuration", "LLM API key must not be empty.")
    if not profile.base_url.strip():
        raise LLMProviderError("configuration", "LLM base URL must not be empty.")
    validate_model_for_profile(profile, profile.model)
    for name, value in (
        ("max_output_tokens", profile.max_output_tokens),
        ("max_retry_attempts", profile.max_retry_attempts),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise LLMProviderError("configuration", f"{name} must be greater than zero.")
    if (
        isinstance(profile.sdk_max_retries, bool)
        or not isinstance(profile.sdk_max_retries, int)
        or profile.sdk_max_retries < 0
    ):
        raise LLMProviderError("configuration", "sdk_max_retries must be zero or greater.")
    for name, value in (
        ("request_timeout_seconds", profile.request_timeout_seconds),
        ("retry_max_delay_seconds", profile.retry_max_delay_seconds),
    ):
        if not math.isfinite(value) or value <= 0:
            raise LLMProviderError(
                "configuration", f"{name} must be finite and greater than zero."
            )
    validate_reasoning_for_profile(profile, profile.reasoning_effort)
    return profile


__all__ = [
    "BaseLLMProfile",
    "DeepSeekProfile",
    "DeepSeekReasoningEffort",
    "LLMErrorCategory",
    "LLMProvider",
    "LLMProviderError",
    "LLMProviderProfile",
    "LLMRole",
    "OpenAIChatProfile",
    "OpenAIReasoningEffort",
    "OpenAIResponsesProfile",
    "require_vision_provider",
    "validate_model_for_profile",
    "validate_model_for_provider",
    "validate_profile",
    "validate_reasoning_for_profile",
]
