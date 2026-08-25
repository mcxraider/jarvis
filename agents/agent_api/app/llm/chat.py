"""Typed Chat Completions request, response, and usage boundary."""

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence, TypeAlias, cast

from openai.types.chat.completion_create_params import (
    CompletionCreateParamsNonStreaming,
)

from agents.agent_api.app.llm.messages import (
    CanonicalAssistantMessage,
    CanonicalMessage,
    CanonicalMessageBatch,
    CanonicalToolCall,
    DeepSeekContinuation,
    serialize_messages,
)
from agents.agent_api.app.llm.provider import (
    DeepSeekProfile,
    LLMProvider,
    LLMProviderError,
    LLMProviderProfile,
    OpenAIChatProfile,
    validate_model_for_profile,
    validate_reasoning_for_profile,
)

FinishReason: TypeAlias = Literal[
    "stop",
    "tool_calls",
    "length",
    "content_filter",
    "function_call",
]
_FINISH_REASONS = {
    "stop",
    "tool_calls",
    "length",
    "content_filter",
    "function_call",
}


@dataclass(frozen=True)
class ChatCompletionCall:
    params: CompletionCreateParamsNonStreaming
    extra_body: Mapping[str, object] | None = None

    def as_kwargs(self) -> dict[str, object]:
        kwargs = dict(self.params)
        if self.extra_body is not None:
            kwargs["extra_body"] = dict(self.extra_body)
        return kwargs


@dataclass(frozen=True, kw_only=True)
class UsageRecord:
    provider: LLMProvider
    requested_model: str
    returned_model: str
    prompt_tokens: int
    completion_tokens: int
    cached_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    request_input_tokens: int
    pricing_tier: str | None = None


@dataclass
class UsageLedger:
    calls: list[UsageRecord] = field(default_factory=list)

    def add(self, record: UsageRecord | None) -> None:
        if record is not None:
            self.calls.append(record)

    def extend(self, records: Sequence[UsageRecord]) -> None:
        self.calls.extend(records)

    def totals(self) -> dict[str, int]:
        return {
            "prompt_tokens": sum(call.prompt_tokens for call in self.calls),
            "completion_tokens": sum(call.completion_tokens for call in self.calls),
            "cached_read_tokens": sum(call.cached_read_tokens for call in self.calls),
            "cache_write_tokens": sum(call.cache_write_tokens for call in self.calls),
            "reasoning_tokens": sum(call.reasoning_tokens for call in self.calls),
        }


@dataclass(frozen=True, kw_only=True)
class ModelCallResult:
    message: CanonicalAssistantMessage
    finish_reason: FinishReason
    usage: UsageRecord | None
    provider: LLMProvider
    requested_model: str
    returned_model: str
    provider_request_id: str | None
    refusal: str | None


def derive_safety_identifier(
    secret: str,
    internal_user_id: str,
    *,
    namespace: str = "jarvis:user",
) -> str:
    """Derive a stable, non-reversible end-user identifier for OpenAI."""

    if not secret.strip():
        raise LLMProviderError(
            "configuration", "LLM_SAFETY_IDENTIFIER_SECRET must not be empty."
        )
    if not internal_user_id.strip():
        raise LLMProviderError(
            "configuration", "Safety identifier source must not be empty."
        )
    source = f"{namespace}:{internal_user_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), source, hashlib.sha256).hexdigest()


def build_chat_completion_call(
    profile: LLMProviderProfile,
    *,
    messages: (
        CanonicalMessageBatch
        | Mapping[str, Any]
        | Sequence[CanonicalMessage | Mapping[str, Any]]
    ),
    tools: Sequence[Mapping[str, Any]] = (),
    response_format: Mapping[str, Any] | None = None,
    safety_identifier: str | None = None,
    model: str | None = None,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
    tool_choice: str | Mapping[str, Any] | None = None,
    temperature: float | None = None,
    timeout_seconds: float | None = None,
    include_thinking: bool = True,
) -> ChatCompletionCall:
    """Build one provider-safe kwargs object for sync or async transports."""

    requested_model = validate_model_for_profile(profile, model or profile.model)
    effort = validate_reasoning_for_profile(profile, reasoning_effort)
    output_tokens = (
        profile.max_output_tokens if max_output_tokens is None else max_output_tokens
    )
    if output_tokens <= 0:
        raise LLMProviderError(
            "configuration", "max_output_tokens must be greater than zero."
        )
    timeout = (
        profile.request_timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    if timeout <= 0:
        raise LLMProviderError(
            "configuration", "timeout_seconds must be greater than zero."
        )

    params: dict[str, Any] = {
        "model": requested_model,
        "messages": serialize_messages(profile, messages),
        "timeout": timeout,
    }
    if tools:
        params["tools"] = [dict(tool) for tool in tools]
    if tool_choice is not None:
        params["tool_choice"] = tool_choice
    if response_format is not None:
        params["response_format"] = dict(response_format)

    extra_body: Mapping[str, object] | None = None
    if isinstance(profile, DeepSeekProfile):
        params["max_tokens"] = output_tokens
        if effort != "off":
            params["reasoning_effort"] = effort
        if temperature is not None:
            params["temperature"] = temperature
        if include_thinking:
            extra_body = {
                "thinking": {
                    "type": "enabled" if profile.thinking_enabled else "disabled"
                }
            }
    elif isinstance(profile, OpenAIChatProfile):
        params["max_completion_tokens"] = output_tokens
        params["reasoning_effort"] = "none"
        if safety_identifier is not None:
            identifier = safety_identifier.strip().lower()
            if len(identifier) != 64 or any(
                character not in "0123456789abcdef" for character in identifier
            ):
                raise LLMProviderError(
                    "configuration",
                    "OpenAI safety_identifier must be a 64-character lowercase hex digest.",
                    provider=profile.provider,
                    model=requested_model,
                )
            params["safety_identifier"] = identifier
        # GPT-5.6 Chat Completions with tools uses effort=none. Sampling fields
        # and DeepSeek extensions are intentionally omitted for this profile.
    else:  # pragma: no cover - the discriminated union should make this impossible.
        raise LLMProviderError("configuration", "Unsupported LLM profile.")
    return ChatCompletionCall(
        params=cast(CompletionCreateParamsNonStreaming, params),
        extra_body=extra_body,
    )


def _read(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _integer(source: Any, name: str) -> int:
    value = _read(source, name)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LLMProviderError("invalid_response", f"Usage field {name!r} is invalid.")
    integer = int(value)
    if integer < 0 or integer != value:
        raise LLMProviderError("invalid_response", f"Usage field {name!r} is invalid.")
    return integer


def _usage_record(
    response: Any,
    *,
    profile: LLMProviderProfile,
    requested_model: str,
    returned_model: str,
) -> UsageRecord | None:
    usage = _read(response, "usage")
    if usage is None:
        return None
    prompt_details = _read(usage, "prompt_tokens_details")
    completion_details = _read(usage, "completion_tokens_details")
    cached_read = _integer(usage, "prompt_cache_hit_tokens")
    if prompt_details is not None and _read(prompt_details, "cached_tokens") is not None:
        cached_read = _integer(prompt_details, "cached_tokens")
    cache_write = _integer(usage, "prompt_cache_miss_tokens")
    if prompt_details is not None:
        for field_name in ("cache_write_tokens", "cache_write_input_tokens"):
            if _read(prompt_details, field_name) is not None:
                cache_write = _integer(prompt_details, field_name)
                break
    prompt_tokens = _integer(usage, "prompt_tokens")
    return UsageRecord(
        provider=profile.provider,
        requested_model=requested_model,
        returned_model=returned_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=_integer(usage, "completion_tokens"),
        cached_read_tokens=cached_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=_integer(completion_details, "reasoning_tokens"),
        request_input_tokens=prompt_tokens,
        # API service tiers (for example "default" or "priority") are not
        # pricing tiers. The pricing layer derives context-sensitive tiers from
        # this call's request_input_tokens before aggregation.
        pricing_tier=None,
    )


def _normalize_tool_calls(raw_calls: Any) -> tuple[CanonicalToolCall, ...]:
    if raw_calls is None:
        return ()
    if not isinstance(raw_calls, (list, tuple)):
        raise LLMProviderError("invalid_response", "Assistant tool_calls must be a list.")
    calls: list[CanonicalToolCall] = []
    seen: set[str] = set()
    for raw_call in raw_calls:
        call_id = _read(raw_call, "id")
        call_type = _read(raw_call, "type", "function")
        function = _read(raw_call, "function")
        name = _read(function, "name")
        arguments = _read(function, "arguments")
        if call_type != "function" or not all(
            isinstance(item, str) for item in (call_id, name, arguments)
        ):
            raise LLMProviderError("invalid_response", "Malformed function tool call.")
        if call_id in seen:
            raise LLMProviderError(
                "invalid_response", f"Duplicate tool-call ID: {call_id!r}."
            )
        try:
            call = CanonicalToolCall(id=call_id, name=name, arguments=arguments)
        except (TypeError, ValueError) as error:
            raise LLMProviderError("invalid_response", str(error)) from error
        seen.add(call_id)
        calls.append(call)
    return tuple(calls)


def normalize_chat_completion(
    response: Any,
    profile: LLMProviderProfile,
    *,
    requested_model: str | None = None,
) -> ModelCallResult:
    """Normalize dict or SDK response shapes before graph logic consumes them."""

    request_model = validate_model_for_profile(
        profile, requested_model or profile.model
    )
    choices = _read(response, "choices")
    if not isinstance(choices, (list, tuple)) or not choices:
        raise LLMProviderError(
            "invalid_response",
            "Chat completion response contains no choices.",
            provider=profile.provider,
            model=request_model,
        )
    choice = choices[0]
    message = _read(choice, "message")
    if message is None:
        raise LLMProviderError("invalid_response", "Completion choice has no message.")
    finish_reason = _read(choice, "finish_reason")
    if finish_reason not in _FINISH_REASONS:
        raise LLMProviderError(
            "invalid_response", f"Unsupported finish reason: {finish_reason!r}."
        )
    content = _read(message, "content")
    if content is not None and not isinstance(content, str):
        raise LLMProviderError("invalid_response", "Assistant content must be text or null.")
    refusal = _read(message, "refusal")
    if refusal is not None and not isinstance(refusal, str):
        raise LLMProviderError("invalid_response", "Assistant refusal must be text or null.")
    calls = _normalize_tool_calls(_read(message, "tool_calls"))
    empty_content = content is None or not content.strip()
    if empty_content and not calls and refusal is None:
        if finish_reason not in {"length", "content_filter"}:
            raise LLMProviderError(
                "invalid_response", "Assistant response contains no content or tool calls."
            )
    # A refusal is output metadata, not checkpoint content. Keep the canonical
    # message structurally valid for the typed result while exposing refusal
    # separately so callers do not treat it as a successful final answer.
    canonical_content = content if content is not None else ""
    continuation = None
    reasoning_content = _read(message, "reasoning_content")
    if (
        isinstance(profile, DeepSeekProfile)
        and calls
        and reasoning_content is not None
    ):
        if not isinstance(reasoning_content, str) or not reasoning_content.strip():
            raise LLMProviderError(
                "invalid_response", "DeepSeek reasoning_content must be non-empty text."
            )
        continuation = DeepSeekContinuation(reasoning_content=reasoning_content)
    canonical_message = CanonicalAssistantMessage(
        content=canonical_content,
        tool_calls=calls,
        continuation=continuation,
    )
    returned_model = _read(response, "model", request_model)
    if not isinstance(returned_model, str) or not returned_model.strip():
        returned_model = request_model
    request_id = _read(response, "_request_id") or _read(response, "request_id")
    if request_id is None:
        request_id = _read(response, "id")
    if request_id is not None and not isinstance(request_id, str):
        request_id = None
    usage = _usage_record(
        response,
        profile=profile,
        requested_model=request_model,
        returned_model=returned_model,
    )
    return ModelCallResult(
        message=canonical_message,
        finish_reason=cast(FinishReason, finish_reason),
        usage=usage,
        provider=profile.provider,
        requested_model=request_model,
        returned_model=returned_model,
        provider_request_id=request_id,
        refusal=refusal,
    )


__all__ = [
    "ChatCompletionCall",
    "FinishReason",
    "ModelCallResult",
    "UsageLedger",
    "UsageRecord",
    "build_chat_completion_call",
    "derive_safety_identifier",
    "normalize_chat_completion",
]
