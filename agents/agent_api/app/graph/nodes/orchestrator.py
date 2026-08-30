"""Orchestrator graph node and provider-neutral LLM client."""

import copy
import inspect
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langsmith import traceable, tracing_context
from langsmith.run_helpers import get_current_run_tree
from langsmith.wrappers import wrap_openai
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAI,
    RateLimitError,
)
from pydantic import ValidationError
from tenacity import (
    AsyncRetrying,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)
from tenacity.nap import sleep as tenacity_sleep

from agents.agent_api.app.async_offload import bounded_to_thread
from agents.agent_api.app.config import settings
from agents.agent_api.app.constants import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MAX_TOKENS,
    DEEPSEEK_MAX_RETRY_ATTEMPTS,
    DEEPSEEK_MODEL,
    DEEPSEEK_REASONING_EFFORT,
    DEEPSEEK_REQUEST_TIMEOUT_SECONDS,
    DEEPSEEK_RETRY_MAX_DELAY_SECONDS,
    DEEPSEEK_SDK_MAX_RETRIES,
    DEEPSEEK_THINKING_ENABLED,
)
from agents.agent_api.app.graph.prompts.context import build_user_request_context
from agents.agent_api.app.graph.prompts.orchestrator import get_system_prompt
from agents.agent_api.app.graph.run_deps import deps_from_config
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.router.model_router import ModelRouter
from agents.agent_api.app.router.prompt import effective_router_domains
from agents.agent_api.app.tools.base import ToolRegistry
from agents.agent_api.app.tools.control import is_ask_user_tool_call
from agents.agent_api.app.tools.selection import DEFAULT_TOOL_SELECTOR, ToolSelector
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter
from agents.agent_api.app.user_context.runtime import RuntimeContextSnapshot
from agents.agent_api.app.llm.chat import (
    UsageRecord,
    build_chat_completion_call,
    derive_safety_identifier,
    normalize_chat_completion,
)
from agents.agent_api.app.llm.messages import CanonicalMessageBatch
from agents.agent_api.app.llm.provider import (
    DeepSeekProfile,
    LLMProviderError,
    LLMProviderProfile,
    OpenAIChatProfile,
    OpenAIResponsesProfile,
    validate_model_for_profile,
    validate_reasoning_for_profile,
)
from agents.agent_api.app.llm.responses import (
    ImageContext,
    build_responses_call,
    normalize_response,
)
from agents.agent_api.app.llm.streaming import (
    consume_async_response_stream,
    consume_response_stream,
    is_summary_rejection,
    strip_summary_from_params,
)


LLM_FAILURE_MESSAGE = "Jarvis could not reach the language model reliably. Please try again in a moment."

_QUESTION_PHRASES = re.compile(
    r"could you (let me know|provide|tell me|clarify)"
    r"|can you (tell me|provide|let me know)"
    r"|please (provide|let me know|clarify|specify)"
    r"|what (would you like|event|title|name|details)"
    r"|which (one|calendar|project)"
    r"|do you (want|mean|prefer)"
    r"|shall I (go ahead|create|proceed|add|schedule|set)"
    r"|would you like me to"
    r"|should I (go ahead|create|proceed|add|schedule|set)",
    re.IGNORECASE,
)

def _last_sentence(text: str) -> str:
    """Extract the last sentence from text for phrase matching."""
    for sep in ("\n", ". ", "! "):
        idx = text.rfind(sep)
        if idx != -1:
            candidate = text[idx:].strip()
            if len(candidate) > 10:
                return candidate
    return text[-200:] if len(text) > 200 else text


def _looks_like_question(content: str) -> bool:
    """Detect if a text-only LLM response is actually a clarification question.

    Returns True when the text ends with '?' (any length) or the last sentence
    contains ask-user-like phrases — indicating the model failed to call ask_user.
    """
    text = content.strip()
    if not text:
        return False
    if text.endswith("?"):
        return True
    return bool(_QUESTION_PHRASES.search(_last_sentence(text)))


def _tool_schema_names(tool_schemas: List[Dict[str, Any]]) -> List[str]:
    """Extract function names from the schemas sent to the model."""

    names: List[str] = []
    for schema in tool_schemas:
        name = schema.get("function", {}).get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


@dataclass
class UsageSummary:
    """Token usage aggregated across DeepSeek calls within one Jarvis run.

    ``cached_tokens`` and ``reasoning_tokens`` are optional provider extras
    (DeepSeek reports prompt cache hits and reasoning tokens); they stay 0 when
    the provider omits them. Monetary cost is intentionally not computed here —
    there is no maintained pricing source yet.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    # Distinct models that served this run's completions. Kept out of as_dict()
    # (which stays token-only for the run-usage emptiness check); the builder
    # derives one effective model for pricing/persistence via effective_run_model.
    models: set = field(default_factory=set)
    records: List[UsageRecord] = field(default_factory=list)

    def add(self, other: "UsageSummary") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cached_tokens += other.cached_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.models |= other.models
        self.records.extend(other.records)

    def add_record(self, record: Optional[UsageRecord]) -> None:
        if record is None:
            return
        self.prompt_tokens += record.prompt_tokens
        self.completion_tokens += record.completion_tokens
        self.total_tokens += record.prompt_tokens + record.completion_tokens
        self.cached_tokens += record.cached_read_tokens
        self.cache_write_tokens += record.cache_write_tokens
        self.reasoning_tokens += record.reasoning_tokens
        self.models.add(record.returned_model)
        self.records.append(record)

    def as_dict(self) -> Dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


def _int_attr(source: Any, name: str) -> int:
    """Read an optional integer usage field from a dict or SDK object."""

    if source is None:
        return 0
    value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
    return int(value) if isinstance(value, (int, float)) else 0


def usage_from_response(response: Any) -> UsageSummary:
    """Extract a UsageSummary from an OpenAI-compatible completion response.

    Tolerates missing optional fields so a provider that omits cache/reasoning
    token counts still yields valid prompt/completion/total numbers.
    """

    usage = getattr(response, "usage", None)
    if usage is None:
        return UsageSummary()

    details = (
        usage.get("completion_tokens_details")
        if isinstance(usage, dict)
        else getattr(usage, "completion_tokens_details", None)
    )
    return UsageSummary(
        prompt_tokens=_int_attr(usage, "prompt_tokens"),
        completion_tokens=_int_attr(usage, "completion_tokens"),
        total_tokens=_int_attr(usage, "total_tokens"),
        cached_tokens=_int_attr(usage, "prompt_cache_hit_tokens"),
        reasoning_tokens=_int_attr(details, "reasoning_tokens"),
    )


def raw_message_from_openai(message: Any) -> Dict[str, Any]:
    """Convert an OpenAI SDK message object into a raw dict without extras loss."""

    # DeepSeek can include provider-specific fields such as reasoning_content.
    # Keeping the raw shape prevents later tool turns from losing that metadata.
    if isinstance(message, dict):
        return copy.deepcopy(message)

    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)

    if hasattr(message, "to_dict"):
        return message.to_dict()

    raise TypeError(f"Unsupported message type: {type(message)!r}")


class LLMAgentClientError(RuntimeError):
    """Terminal LLM client failure with graph-safe structured metadata."""

    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        super().__init__(json.dumps(payload, sort_keys=True))


def _model_trace_inputs(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Keep image payloads out of the outer LangSmith model span."""

    safe = dict(inputs)
    safe.pop("self", None)
    ctx = safe.pop("image_context", None)
    if ctx:
        images = ctx.get("images") or ()
        prior = ctx.get("prior_batches") or ()
        image_count = len(images) + sum(len(batch) for batch in prior)
        if image_count:
            safe["image_count"] = image_count
    return safe


class LLMAgentClient:
    """Request-bound provider-neutral Chat Completions client.

    Production wrappers are cheap, immutable bindings over process-wide SDK
    transports.  The tracer and usage accumulator belong to one wrapper/run;
    model and reasoning overrides are supplied per call.  Explicitly
    configured clients keep their own transport for tests and CLI callers.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        tracer: Optional[TracePrinter] = None,
        request_timeout_seconds: Optional[float] = None,
        max_retry_attempts: Optional[int] = None,
        retry_max_delay_seconds: Optional[float] = None,
        retry_sleep: Optional[Any] = None,
        client: Optional[Any] = None,
        async_client: Optional[Any] = None,
        profile: Optional[LLMProviderProfile] = None,
        safety_identifier: Optional[str] = None,
    ):
        if profile is None:
            if api_key is None:
                profile = settings.orchestrator_llm
            else:
                profile = DeepSeekProfile(
                    api_key=api_key,
                    base_url=base_url or DEEPSEEK_BASE_URL,
                    model=model or DEEPSEEK_MODEL,
                    max_output_tokens=DEEPSEEK_MAX_TOKENS,
                    request_timeout_seconds=(
                        request_timeout_seconds or DEEPSEEK_REQUEST_TIMEOUT_SECONDS
                    ),
                    max_retry_attempts=(
                        max_retry_attempts or DEEPSEEK_MAX_RETRY_ATTEMPTS
                    ),
                    retry_max_delay_seconds=(
                        retry_max_delay_seconds or DEEPSEEK_RETRY_MAX_DELAY_SECONDS
                    ),
                    sdk_max_retries=DEEPSEEK_SDK_MAX_RETRIES,
                    reasoning_effort=(reasoning_effort or DEEPSEEK_REASONING_EFFORT),
                    thinking_enabled=DEEPSEEK_THINKING_ENABLED,
                )
        self.profile = profile
        self.provider = profile.provider
        self.api_key = profile.api_key
        self.model = model or profile.model
        self.base_url = base_url or profile.base_url
        self.reasoning_effort = reasoning_effort or profile.reasoning_effort
        self.safety_identifier = safety_identifier
        if isinstance(
            profile, (OpenAIChatProfile, OpenAIResponsesProfile)
        ) and self.safety_identifier is None:
            self.safety_identifier = derive_safety_identifier(
                settings.llm_safety_identifier_secret or "",
                "local-user",
            )
        self._tracer = tracer or NULL_TRACE
        self.request_timeout_seconds = (
            request_timeout_seconds or profile.request_timeout_seconds
        )
        # Token usage accumulated across every turn/retry of one Jarvis run, read
        # by run_jarvis for the per-run log footer. LangSmith gets per-call usage
        # automatically via wrap_openai; this is the on-disk fallback.
        self.usage = UsageSummary()
        self.max_retry_attempts = max(
            1, max_retry_attempts or profile.max_retry_attempts
        )
        self.retry_max_delay_seconds = (
            retry_max_delay_seconds or profile.retry_max_delay_seconds
        )
        self.retry_sleep = retry_sleep
        if not self.api_key:
            raise RuntimeError(
                f"{self.provider.value.upper()} API key is required to run Jarvis."
            )
        # SDK retries disabled (default 0); tenacity wraps calls with backoff + per-attempt tracing.
        self._owns_client = client is None
        # An explicitly configured wrapper must never silently switch to the
        # process-global/env-backed async transport. Production binds the shared
        # async client in ``get_shared_agent_client`` below.
        self.async_client = async_client
        self.client = (
            client
            if client is not None
            else wrap_openai(
                OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=self.request_timeout_seconds,
                    max_retries=profile.sdk_max_retries,
                ),
                chat_name=f"orchestrator.llm.{profile.provider.value}",
                completions_name=f"orchestrator.llm.{profile.provider.value}",
            )
        )

    @property
    def tracer(self) -> "TracePrinter":
        return self._tracer

    def with_tracer(self, tracer: "TracePrinter") -> "LLMAgentClient":
        """Return a per-run binding without mutating or rebuilding transport."""

        clone = copy.copy(self)
        clone._tracer = tracer
        # ``copy.copy`` intentionally reuses the SDK connection pool, but usage
        # is mutable request context and must never cross run boundaries.
        clone.usage = UsageSummary()
        clone._owns_client = False
        return clone

    def _resolve_call_params(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model: Optional[str],
        reasoning_effort: Optional[str],
        request_timeout_seconds: Optional[float],
        tracer: Optional[TracePrinter],
        usage_accumulator: Optional[UsageSummary],
        async_label: bool,
    ):
        """Resolve tracer/usage/model/effort/timeout and emit the request trace."""
        call_tracer = tracer or self.tracer
        call_usage = usage_accumulator if usage_accumulator is not None else self.usage
        use_model = validate_model_for_profile(self.profile, model or self.model)
        use_effort = validate_reasoning_for_profile(
            self.profile, reasoning_effort or self.reasoning_effort
        )
        use_timeout = (
            request_timeout_seconds
            if request_timeout_seconds is not None
            else self.request_timeout_seconds
        )
        call_tracer.event(
            "agent.request",
            "Calling language model (async)." if async_label else "Calling language model.",
            provider=self.provider.value,
            api_surface=(
                "responses"
                if isinstance(self.profile, OpenAIResponsesProfile)
                else "chat_completions"
            ),
            model=use_model,
            reasoning_effort=use_effort,
            messages=len(messages),
            tools=len(tools),
            request_timeout_seconds=use_timeout,
            sdk_max_retries=self.profile.sdk_max_retries,
            max_retry_attempts=self.max_retry_attempts,
            retry_max_delay_seconds=self.retry_max_delay_seconds,
            max_tokens=self.profile.max_output_tokens,
            thinking_enabled=(
                self.profile.thinking_enabled
                if isinstance(self.profile, DeepSeekProfile)
                else False
            ),
            base_url=self.base_url,
        )
        return call_tracer, call_usage, use_model, use_effort, use_timeout

    def _build_result(
        self,
        response: Any,
        *,
        use_model: str,
        call_usage: UsageSummary,
        captured_summary: Optional[str],
    ):
        """Normalize a completion, record usage, and build the checkpoint message."""
        result = (
            normalize_response(response, self.profile, requested_model=use_model)
            if isinstance(self.profile, OpenAIResponsesProfile)
            else normalize_chat_completion(
                response, self.profile, requested_model=use_model
            )
        )
        self._validate_result(result)
        turn_usage = UsageSummary()
        turn_usage.add_record(result.usage)
        call_usage.add(turn_usage)
        message = CanonicalMessageBatch(
            messages=(result.message,)
        ).to_checkpoint()["messages"][0]

        rt = get_current_run_tree()
        if rt:
            trace_meta: Dict[str, Any] = {}
            if captured_summary:
                trace_meta["reasoning_summary"] = captured_summary
            if message.get("tool_calls"):
                trace_meta["tool_calls"] = message["tool_calls"]
            if trace_meta:
                rt.add_metadata(trace_meta)
        return result, turn_usage, message

    def _build_failure_payload(
        self,
        error: Exception,
        attempts: int,
        *,
        call_tracer: TracePrinter,
        use_timeout: float,
        request_started: float,
        async_label: bool,
    ) -> Dict[str, Any]:
        """Build the failure payload and emit the error trace; caller raises."""
        total_elapsed_ms = round((time.monotonic() - request_started) * 1000, 1)
        payload = self._failure_payload(
            error,
            attempts,
            request_timeout_seconds=use_timeout,
        )
        payload["total_elapsed_ms"] = total_elapsed_ms
        call_tracer.event(
            "agent.error",
            "Language-model async chat completion failed."
            if async_label
            else "Language-model chat completion failed.",
            provider=self.provider.value,
            error_type=payload["type"],
            retryable=payload["retryable"],
            attempts=payload["attempts"],
            status_code=payload.get("status_code"),
            provider_request_id=payload.get("provider_request_id"),
            exception_type=payload.get("exception_type"),
            exception_module=payload.get("exception_module"),
            timeout_kind=payload.get("timeout_kind"),
            base_url=payload.get("base_url"),
            request_timeout_seconds=payload.get("request_timeout_seconds"),
            sdk_max_retries=payload.get("sdk_max_retries"),
            max_retry_attempts=payload.get("max_retry_attempts"),
            retry_max_delay_seconds=payload.get("retry_max_delay_seconds"),
            total_elapsed_ms=payload.get("total_elapsed_ms"),
        )
        return payload

    def _trace_response(
        self,
        call_tracer: TracePrinter,
        *,
        result: Any,
        message: Dict[str, Any],
        turn_usage: UsageSummary,
        use_model: str,
        summary_fallback_attempted: bool,
        async_label: bool,
    ) -> None:
        """Emit the assistant-response trace for a completed call."""
        cache_hit_rate = (
            round(turn_usage.cached_tokens / turn_usage.prompt_tokens * 100, 1)
            if turn_usage.prompt_tokens > 0 and turn_usage.cached_tokens > 0
            else None
        )
        call_tracer.event(
            "agent.response",
            "Received assistant message (async)."
            if async_label
            else "Received assistant message.",
            provider=self.provider.value,
            model=getattr(result, "returned_model", use_model),
            provider_request_id=getattr(result, "provider_request_id", None),
            has_tool_calls=bool(message.get("tool_calls")),
            tool_calls=len(message.get("tool_calls") or []),
            has_content=bool(message.get("content")),
            has_reasoning=bool(result.message.continuation),
            reasoning_summary_requested=isinstance(self.profile, OpenAIResponsesProfile),
            reasoning_summary_fallback=summary_fallback_attempted,
            prompt_tokens=turn_usage.prompt_tokens or None,
            completion_tokens=turn_usage.completion_tokens or None,
            total_tokens=turn_usage.total_tokens or None,
            cached_tokens=turn_usage.cached_tokens or None,
            cache_hit_rate=cache_hit_rate,
        )

    @traceable(
        name="orchestrator.llm",
        run_type="llm",
        process_inputs=_model_trace_inputs,
    )
    def create_message(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        request_timeout_seconds: Optional[float] = None,
        tracer: Optional[TracePrinter] = None,
        usage_accumulator: Optional[UsageSummary] = None,
        image_context: "ImageContext | None" = None,
    ) -> Dict[str, Any]:
        """Create one message while preserving the legacy synchronous result.

        Request context can be passed explicitly for compile-once graph runs.
        Older callers may keep using a tracer-bound wrapper and reading
        ``client.usage``; both paths update only a run-local accumulator.
        """

        call_tracer, call_usage, use_model, use_effort, use_timeout = (
            self._resolve_call_params(
                messages=messages,
                tools=tools,
                model=model,
                reasoning_effort=reasoning_effort,
                request_timeout_seconds=request_timeout_seconds,
                tracer=tracer,
                usage_accumulator=usage_accumulator,
                async_label=False,
            )
        )
        attempts = 0
        request_started = time.monotonic()

        summary_fallback_attempted = False
        captured_summary = None

        def _on_summary(text: str) -> None:
            nonlocal captured_summary
            captured_summary = text
            call_tracer.reasoning_summary(text)

        def create_completion() -> Any:
            nonlocal attempts, summary_fallback_attempted
            attempts += 1
            attempt_started = time.monotonic()
            try:
                if isinstance(self.profile, OpenAIResponsesProfile):
                    call = build_responses_call(
                        self.profile,
                        model=use_model,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto" if tools else None,
                        reasoning_effort=use_effort,
                        safety_identifier=self.safety_identifier,
                        timeout_seconds=use_timeout,
                        image_context=image_context,
                        vision_model=settings.openai_vision_model,
                    )
                    kwargs = call.as_kwargs()
                    with tracing_context(enabled=False):
                        try:
                            with self.client.responses.stream(**kwargs) as stream:
                                return consume_response_stream(
                                    stream,
                                    on_summary=_on_summary,
                                )
                        except (APIStatusError,) as summary_err:
                            if (
                                not summary_fallback_attempted
                                and is_summary_rejection(summary_err)
                            ):
                                summary_fallback_attempted = True
                                fallback_kwargs = strip_summary_from_params(kwargs)
                                with self.client.responses.stream(**fallback_kwargs) as stream:
                                    return consume_response_stream(stream)
                            raise
                call = build_chat_completion_call(
                    self.profile,
                    model=use_model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto" if tools else None,
                    reasoning_effort=use_effort,
                    safety_identifier=self.safety_identifier,
                    timeout_seconds=use_timeout,
                )
                return self.client.chat.completions.create(**call.as_kwargs())
            except Exception as error:
                call_tracer.event(
                    "agent.attempt.error",
                    "Language-model request attempt failed.",
                    provider=self.provider.value,
                    attempt=attempts,
                    error_type=self._error_type(error),
                    status_code=self._status_code(error),
                    elapsed_ms=round((time.monotonic() - attempt_started) * 1000, 1),
                    request_timeout_seconds=use_timeout,
                    **self._error_details(error),
                )
                raise

        try:
            response = self._retrying(
                call_tracer,
                request_timeout_seconds=use_timeout,
            )(create_completion)
            result, turn_usage, message = self._build_result(
                response,
                use_model=use_model,
                call_usage=call_usage,
                captured_summary=captured_summary,
            )
        except Exception as error:
            payload = self._build_failure_payload(
                error,
                attempts,
                call_tracer=call_tracer,
                use_timeout=use_timeout,
                request_started=request_started,
                async_label=False,
            )
            raise LLMAgentClientError(payload) from error

        self._trace_response(
            call_tracer,
            result=result,
            message=message,
            turn_usage=turn_usage,
            use_model=use_model,
            summary_fallback_attempted=summary_fallback_attempted,
            async_label=False,
        )
        return message

    @traceable(
        name="orchestrator.llm",
        run_type="llm",
        process_inputs=_model_trace_inputs,
    )
    async def async_create_message(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        request_timeout_seconds: Optional[float] = None,
        tracer: Optional[TracePrinter] = None,
        usage_accumulator: Optional[UsageSummary] = None,
        async_client: Optional[Any] = None,
        image_context: "ImageContext | None" = None,
    ) -> Dict[str, Any]:
        """Async counterpart using the shared transport by default.

        This mirrors :meth:`create_message`'s request-context and return-value
        contract so the later async graph conversion does not need another
        client-interface migration.
        """

        provider_client = (
            async_client
            or self.async_client
            or get_shared_async_agent_client()
        )
        call_tracer, call_usage, use_model, use_effort, use_timeout = (
            self._resolve_call_params(
                messages=messages,
                tools=tools,
                model=model,
                reasoning_effort=reasoning_effort,
                request_timeout_seconds=request_timeout_seconds,
                tracer=tracer,
                usage_accumulator=usage_accumulator,
                async_label=True,
            )
        )
        attempts = 0
        request_started = time.monotonic()
        summary_fallback_attempted = False
        captured_summary_async = None

        def _on_summary_async(text: str) -> None:
            nonlocal captured_summary_async
            captured_summary_async = text
            call_tracer.reasoning_summary(text)

        async def create_completion() -> Any:
            nonlocal attempts, summary_fallback_attempted
            attempts += 1
            attempt_started = time.monotonic()
            try:
                if isinstance(self.profile, OpenAIResponsesProfile):
                    call = build_responses_call(
                        self.profile,
                        model=use_model,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto" if tools else None,
                        reasoning_effort=use_effort,
                        safety_identifier=self.safety_identifier,
                        timeout_seconds=use_timeout,
                        image_context=image_context,
                        vision_model=settings.openai_vision_model,
                    )
                    kwargs = call.as_kwargs()
                    with tracing_context(enabled=False):
                        try:
                            async with provider_client.responses.stream(**kwargs) as stream:
                                return await consume_async_response_stream(
                                    stream,
                                    on_summary=_on_summary_async,
                                )
                        except (APIStatusError,) as summary_err:
                            if (
                                not summary_fallback_attempted
                                and is_summary_rejection(summary_err)
                            ):
                                summary_fallback_attempted = True
                                fallback_kwargs = strip_summary_from_params(kwargs)
                                async with provider_client.responses.stream(**fallback_kwargs) as stream:
                                    return await consume_async_response_stream(stream)
                            raise
                call = build_chat_completion_call(
                    self.profile,
                    model=use_model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto" if tools else None,
                    reasoning_effort=use_effort,
                    safety_identifier=self.safety_identifier,
                    timeout_seconds=use_timeout,
                )
                return await provider_client.chat.completions.create(**call.as_kwargs())
            except Exception as error:
                call_tracer.event(
                    "agent.attempt.error",
                    "Language-model async request attempt failed.",
                    provider=self.provider.value,
                    attempt=attempts,
                    error_type=self._error_type(error),
                    status_code=self._status_code(error),
                    elapsed_ms=round((time.monotonic() - attempt_started) * 1000, 1),
                    request_timeout_seconds=use_timeout,
                    **self._error_details(error),
                )
                raise

        try:
            async for attempt in self._async_retrying(
                call_tracer,
                request_timeout_seconds=use_timeout,
            ):
                with attempt:
                    response = await create_completion()
            result, turn_usage, message = self._build_result(
                response,
                use_model=use_model,
                call_usage=call_usage,
                captured_summary=captured_summary_async,
            )
        except Exception as error:
            payload = self._build_failure_payload(
                error,
                attempts,
                call_tracer=call_tracer,
                use_timeout=use_timeout,
                request_started=request_started,
                async_label=True,
            )
            raise LLMAgentClientError(payload) from error

        self._trace_response(
            call_tracer,
            result=result,
            message=message,
            turn_usage=turn_usage,
            use_model=use_model,
            summary_fallback_attempted=summary_fallback_attempted,
            async_label=True,
        )
        return message

    @staticmethod
    def _validate_result(result: Any) -> None:
        """Reject terminal metadata that must never become an empty success."""

        if result.refusal:
            raise LLMProviderError("invalid_response", "The model refused the request.")
        if result.finish_reason == "length":
            raise LLMProviderError("invalid_response", "The model response was truncated.")
        if result.finish_reason == "content_filter":
            raise LLMProviderError("invalid_response", "The model response was filtered.")
        if result.finish_reason == "function_call" and not result.message.tool_calls:
            raise LLMProviderError(
                "invalid_response", "Legacy function_call output is unsupported."
            )

    def _retrying(
        self,
        tracer: TracePrinter,
        *,
        request_timeout_seconds: Optional[float] = None,
    ) -> Retrying:
        sleep = self.retry_sleep if self.retry_sleep is not None else tenacity_sleep
        use_timeout = (
            request_timeout_seconds
            if request_timeout_seconds is not None
            else self.request_timeout_seconds
        )
        return Retrying(
            retry=retry_if_exception(self._is_retryable_error),
            wait=wait_random_exponential(multiplier=1, max=self.retry_max_delay_seconds),
            stop=stop_after_attempt(self.max_retry_attempts),
            reraise=True,
            sleep=sleep,
            before_sleep=lambda retry_state: self._trace_retry(
                retry_state,
                tracer,
                request_timeout_seconds=use_timeout,
            ),
        )

    def _async_retrying(
        self,
        tracer: TracePrinter,
        *,
        request_timeout_seconds: Optional[float] = None,
    ) -> AsyncRetrying:
        use_timeout = (
            request_timeout_seconds
            if request_timeout_seconds is not None
            else self.request_timeout_seconds
        )
        return AsyncRetrying(
            retry=retry_if_exception(self._is_retryable_error),
            wait=wait_random_exponential(multiplier=1, max=self.retry_max_delay_seconds),
            stop=stop_after_attempt(self.max_retry_attempts),
            reraise=True,
            before_sleep=lambda retry_state: self._trace_retry(
                retry_state,
                tracer,
                request_timeout_seconds=use_timeout,
            ),
        )

    def _trace_retry(
        self,
        retry_state: Any,
        tracer: TracePrinter,
        *,
        request_timeout_seconds: Optional[float] = None,
    ) -> None:
        error = retry_state.outcome.exception() if retry_state.outcome else None
        tracer.event(
            "agent.retry",
            "Retrying language-model request.",
            provider=self.provider.value,
            attempt=retry_state.attempt_number,
            error_type=self._error_type(error),
            status_code=self._status_code(error),
            provider_request_id=self._provider_request_id(error),
            exception_type=type(error).__name__ if error is not None else None,
            exception_module=type(error).__module__ if error is not None else None,
            timeout_kind=self._timeout_kind(error),
            request_timeout_seconds=(
                request_timeout_seconds
                if request_timeout_seconds is not None
                else self.request_timeout_seconds
            ),
            retry_sleep_seconds=(
                round(retry_state.next_action.sleep, 3)
                if getattr(retry_state, "next_action", None) is not None
                else None
            ),
        )
        progress = getattr(tracer, "progress", None)
        if callable(progress):
            progress({
                "phase": "retrying",
                "action": "retrying",
                "retry": {"target": "model", "reason": self._progress_retry_reason(error)},
            })

    def _progress_retry_reason(self, error: Optional[BaseException]) -> str:
        error_type = self._error_type(error)
        if error_type == "rate_limited":
            return "rate_limited"
        if error_type == "provider_unavailable":
            return "service_unavailable"
        if error_type == "timeout":
            return "timeout"
        return "temporary_connection"

    def _is_retryable_error(self, error: BaseException) -> bool:
        if isinstance(error, LLMProviderError):
            return False
        if isinstance(error, (APITimeoutError, APIConnectionError)):
            return True

        status_code = self._status_code(error)
        if status_code == 429:
            return True
        if isinstance(error, APIStatusError) and status_code is not None:
            return status_code >= 500

        return isinstance(error, RateLimitError)

    def _failure_payload(
        self,
        error: BaseException,
        attempts: int,
        *,
        request_timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        status_code = self._status_code(error)
        payload: Dict[str, Any] = {
            "source": self.provider.value,
            "provider": self.provider.value,
            "model": self.model,
            "type": self._error_type(error),
            "retryable": self._is_retryable_error(error),
            "attempts": attempts,
            "message": str(error),
            "error_message": str(error),
            "base_url": self.base_url,
            "request_timeout_seconds": (
                request_timeout_seconds
                if request_timeout_seconds is not None
                else self.request_timeout_seconds
            ),
            "sdk_max_retries": self.profile.sdk_max_retries,
            "retry_max_delay_seconds": self.retry_max_delay_seconds,
            "max_retry_attempts": self.max_retry_attempts,
            **self._error_details(error),
        }
        if status_code is not None:
            payload["status_code"] = status_code
        return payload

    def _error_details(self, error: Optional[BaseException]) -> Dict[str, Any]:
        if error is None:
            return {
                "exception_type": None,
                "exception_module": None,
                "error_message": None,
                "provider_request_id": None,
                "timeout_kind": None,
            }
        return {
            "exception_type": type(error).__name__,
            "exception_module": type(error).__module__,
            "error_message": str(error),
            "provider_request_id": self._provider_request_id(error),
            "timeout_kind": self._timeout_kind(error),
        }

    def _provider_request_id(self, error: Optional[BaseException]) -> Optional[str]:
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        for name in ("x-request-id", "x-ds-request-id", "request-id"):
            value = headers.get(name)
            if value:
                return str(value)
        return None

    def _timeout_kind(self, error: Optional[BaseException]) -> Optional[str]:
        seen: set[int] = set()
        current: Optional[BaseException] = error
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            name = type(current).__name__.lower()
            if "connecttimeout" in name or "connectiontimeout" in name:
                return "connect"
            if "readtimeout" in name:
                return "read"
            if "writetimeout" in name:
                return "write"
            if "pooltimeout" in name:
                return "pool"
            current = (
                current.__cause__
                if current.__cause__ is not None
                else current.__context__
            )
        return "timeout" if isinstance(error, APITimeoutError) else None

    def _error_type(self, error: Optional[BaseException]) -> str:
        if error is None:
            return "unexpected"
        if isinstance(error, LLMProviderError):
            return error.category
        if isinstance(error, APITimeoutError):
            return "timeout"
        if isinstance(error, APIConnectionError):
            return "provider_unavailable"

        status_code = self._status_code(error)
        if status_code == 429 or isinstance(error, RateLimitError):
            return "rate_limited"
        if status_code is not None and status_code >= 500:
            return "provider_unavailable"
        if status_code in {401, 403}:
            return "auth"
        if status_code is not None and 400 <= status_code < 500:
            return "configuration"

        return "unexpected"

    @staticmethod
    def _status_code(error: Optional[BaseException]) -> Optional[int]:
        status_code = getattr(error, "status_code", None)
        return status_code if isinstance(status_code, int) else None

    def close(self) -> None:
        """Close a privately-owned SDK transport.

        Request-bound wrappers returned by :func:`get_shared_agent_client` do
        not own the process transport; it is closed exactly once by the shared
        lifecycle helper.
        """

        if not self._owns_client:
            return
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


# Shared SDK transports, deliberately separate from request-bound wrappers.
# OpenAI/httpx clients retain connection pools and are expensive to rebuild;
# tracer, usage, model, and reasoning state never lives in these singletons.
_shared_openai_client: Optional[OpenAI] = None
_shared_openai_client_lock = threading.Lock()
_shared_async_agent_client: Optional[AsyncOpenAI] = None
_shared_async_agent_client_lock = threading.Lock()


def _get_shared_openai_client() -> OpenAI:
    """Return the lazily-created process-wide synchronous SDK client."""

    global _shared_openai_client
    client = _shared_openai_client
    if client is not None:
        return client
    with _shared_openai_client_lock:
        if _shared_openai_client is None:
            profile = settings.orchestrator_llm
            _shared_openai_client = wrap_openai(
                OpenAI(
                    api_key=profile.api_key,
                    base_url=profile.base_url,
                    timeout=profile.request_timeout_seconds,
                    max_retries=profile.sdk_max_retries,
                ),
                chat_name=f"orchestrator.llm.{profile.provider.value}",
                completions_name=f"orchestrator.llm.{profile.provider.value}",
            )
        return _shared_openai_client


def get_shared_agent_client(
    tracer: Optional[TracePrinter] = None,
    internal_user_id: Optional[str] = None,
) -> LLMAgentClient:
    """Return a fresh run binding over the process-wide sync SDK transport."""

    profile = settings.orchestrator_llm
    safety_identifier = None
    if isinstance(profile, (OpenAIChatProfile, OpenAIResponsesProfile)):
        safety_identifier = derive_safety_identifier(
            settings.llm_safety_identifier_secret or "",
            internal_user_id or "local-user",
        )
    return LLMAgentClient(
        profile=profile,
        tracer=tracer,
        client=_get_shared_openai_client(),
        async_client=get_shared_async_agent_client(),
        safety_identifier=safety_identifier,
    )


def get_shared_async_agent_client() -> AsyncOpenAI:
    """Return the lazily-created process-wide asynchronous SDK client."""

    global _shared_async_agent_client
    client = _shared_async_agent_client
    if client is not None:
        return client
    with _shared_async_agent_client_lock:
        if _shared_async_agent_client is None:
            profile = settings.orchestrator_llm
            _shared_async_agent_client = wrap_openai(
                AsyncOpenAI(
                    api_key=profile.api_key,
                    base_url=profile.base_url,
                    timeout=profile.request_timeout_seconds,
                    max_retries=profile.sdk_max_retries,
                ),
                chat_name=f"orchestrator.llm.{profile.provider.value}",
                completions_name=f"orchestrator.llm.{profile.provider.value}",
            )
        return _shared_async_agent_client


def close_shared_agent_client() -> None:
    """Close and reset the process-wide synchronous SDK transport."""

    global _shared_openai_client
    with _shared_openai_client_lock:
        client = _shared_openai_client
        _shared_openai_client = None
    if client is not None:
        client.close()


async def close_shared_async_agent_client() -> None:
    """Close and reset the process-wide asynchronous SDK transport."""

    global _shared_async_agent_client
    with _shared_async_agent_client_lock:
        client = _shared_async_agent_client
        _shared_async_agent_client = None
    if client is not None:
        await client.close()


def _apply_router_prompt_slimming(
    messages: List[Dict[str, Any]],
    tool_selector: ToolSelector,
    state: JarvisState,
    tracer: TracePrinter,
    selected_tool_names: List[str],
) -> None:
    """Slim the system prompt to the router's routed domains, in place.

    When the selector exposes a router ``decision`` and this run has a runtime
    snapshot, rebuild ONLY ``messages[0]`` (the system message) so its per-domain
    fragments cover just the domains the query needs. Later turns carry
    accumulated tool history in ``messages[1:]``; touching only index 0 keeps that
    history intact (rebuilding the whole list would discard it).

    Non-critical by contract: a missing decision, an absent/unvalidatable
    snapshot, or a non-system first message all leave ``messages`` untouched —
    i.e. today's full-prompt behavior. The decision is stable within a run (the
    query is constant), so re-slimming each turn is idempotent.
    """

    decision = getattr(tool_selector, "decision", None)
    if decision is None:
        return
    raw_context = state.get("runtime_context")
    if not raw_context:
        return
    if not messages or messages[0].get("role") != "system":
        return
    try:
        snapshot = RuntimeContextSnapshot.model_validate(raw_context)
    except ValidationError:
        tracer.event(
            "router.prompt.skipped",
            "Runtime snapshot did not validate; keeping full prompt.",
        )
        return

    relevant = set(effective_router_domains(decision)) & snapshot.active_providers()
    # Include pinned domains so the agent retains domain instructions even if
    # the router narrowed this turn (e.g. HITL resume classified todoist-only).
    active_domains = state.get("active_domains") or []
    if active_domains:
        relevant |= set(active_domains) & snapshot.active_providers()
    messages[0] = {
        **messages[0],
        "content": get_system_prompt(
            runtime_context=snapshot,
            registered_tools=selected_tool_names,
            relevant_domains=relevant,
        ),
    }
    tracer.event(
        "router.prompt.slimmed",
        "Rebuilt system prompt for routed domains.",
        relevant=sorted(relevant) or None,
    )


def create_agent_node(
    agent_client: Any = None,
    registry: Optional[ToolRegistry] = None,
    max_agent_turns: Optional[int] = None,
    tracer: Optional[TracePrinter] = None,
    tool_selector: Optional[ToolSelector] = None,
    model_router: Optional[ModelRouter] = None,
    usage_accumulator: Optional[UsageSummary] = None,
):
    """Create the graph node that asks the model what to do next.

    Production calls resolve request-local objects from ``RunDeps`` in the
    LangGraph config, allowing one compiled node/topology to serve concurrent
    users.  Captured arguments remain fallbacks for direct tests and Studio.
    The selector narrows the run's registry without changing execution scope.
    """

    captured_tracer = tracer or NULL_TRACE
    captured_tool_selector = tool_selector or DEFAULT_TOOL_SELECTOR

    async def agent_node(
        state: JarvisState,
        config: RunnableConfig | None = None,
    ) -> JarvisState:
        deps = deps_from_config(config)
        run_agent_client = (
            deps.agent_client
            if deps is not None and deps.agent_client is not None
            else agent_client
        )
        run_registry = (
            deps.registry if deps is not None and deps.registry is not None else registry
        )
        run_max_agent_turns = (
            deps.max_agent_turns
            if deps is not None and deps.max_agent_turns is not None
            else max_agent_turns
        )
        run_tracer = (
            deps.tracer if deps is not None and deps.tracer is not None else captured_tracer
        )
        run_tool_selector = (
            deps.tool_selector
            if deps is not None and deps.tool_selector is not None
            else captured_tool_selector
        )
        run_model_router = (
            deps.model_router
            if deps is not None and deps.model_router is not None
            else model_router
        )
        run_usage_accumulator = (
            deps.usage_accumulator
            if deps is not None and deps.usage_accumulator is not None
            else usage_accumulator
        )
        if run_agent_client is None or run_registry is None or run_max_agent_turns is None:
            raise RuntimeError(
                "Agent node requires agent_client, registry, and max_agent_turns "
                "from RunDeps or captured fallbacks."
            )

        turn_count = state.get("turn_count", 0)
        run_tracer.event(
            "graph.agent",
            "Entering agent node.",
            turn=turn_count + 1,
            max_turns=run_max_agent_turns,
            messages=len(state.get("messages", [])),
        )
        if turn_count >= run_max_agent_turns:
            error = f"Max agent turns exceeded ({run_max_agent_turns})."
            user_message = "Max number of turns reached for this agent. Simplify your query."
            run_tracer.event(
                "graph.guard", "Stopping graph because max turns was reached.", error=error
            )
            return {
                "error": error,
                "final_response": user_message,
                "next": "end",
            }

        messages = list(state.get("messages", []))
        # Narrow the catalogue to the tools this turn should expose. The default
        # selector returns everything; a future query-aware selector returns a
        # relevant subset (see tools/selection.py). Execution still runs against
        # the full registry, so this only shapes what the model sees.
        user_prompt = state.get("user_prompt", "")
        # After a HITL clarification, route on the latest reply — the user may
        # redirect intent (e.g. from tasks to calendar). For the router selector
        # this is a natural cache miss; for keyword/static selectors it's a no-op.
        clarification_history = state.get("clarification_history") or []
        if clarification_history:
            last_entry = clarification_history[-1]
            last_reply = last_entry.get("reply") or ""
            last_question = last_entry.get("question") or ""
            routing_query = (
                f"{user_prompt} "
                f"[You asked: {last_question}] "
                f"[User replied: {last_reply}]"
            )
        else:
            reply_context = state.get("reply_context")
            routing_query = (
                build_user_request_context(
                    user_prompt,
                    reply_context=reply_context,
                )
                if reply_context
                else user_prompt
            )
        # Pass active_domains so the selector can merge pinned domains on resumes.
        active_domains = state.get("active_domains") or []
        async_select_schemas = getattr(run_tool_selector, "async_select_schemas", None)
        if inspect.iscoroutinefunction(async_select_schemas):
            tool_schemas = await async_select_schemas(
                routing_query,
                run_registry,
                active_domains=active_domains or None,
            )
        else:
            tool_schemas = await bounded_to_thread(
                run_tool_selector.select_schemas,
                routing_query,
                run_registry,
                active_domains=active_domains or None,
            )
        selected_tool_names = _tool_schema_names(tool_schemas)

        # Persist the initial routing domains for context preservation across
        # HITL resumes. Only set on the first turn (no clarification history yet,
        # no prior active_domains); subsequent turns within the same run reuse it.
        selector_decision = getattr(run_tool_selector, "decision", None)
        if not clarification_history and not active_domains and selector_decision:
            active_domains = list(effective_router_domains(selector_decision))

        routed_domains = [
            {"google_calendar": "calendar"}.get(domain, domain)
            for domain in (active_domains or [])
            if domain in {"todoist", "google_calendar", "calendar", "gmail", "notion"}
        ]
        selected_specs = [run_registry.get(name) for name in selected_tool_names]
        intent = "mutation" if any(spec and spec.mutating for spec in selected_specs) else "read"
        if routed_domains:
            run_tracer.progress({
                "phase": "routing",
                "action": "completed",
                "domains": sorted(set(routed_domains)),
                "intent": intent,
            })

        run_tracer.event(
            "graph.tools.selected",
            "Selected tools for this turn.",
            available=len(run_registry.specs),
            selected=len(tool_schemas),
            tool_names=selected_tool_names,
        )
        # If a query router chose the tools, slim the system prompt to match — the
        # model should not read a Calendar block when only Todoist tools are on
        # offer. Rebuilds messages[0] only (history-safe); no-op without a decision.
        _apply_router_prompt_slimming(
            messages,
            run_tool_selector,
            state,
            run_tracer,
            selected_tool_names,
        )
        model_override = None
        effort_override = None
        timeout_override = None
        if run_model_router is not None and selector_decision is not None:
            selection = run_model_router.select(selector_decision)
            model_override = selection.model
            effort_override = selection.reasoning_effort
            timeout_override = selection.request_timeout_seconds
            run_tracer.event(
                "model_router.selected",
                "Model router selected model for this turn.",
                model=selection.model,
                reasoning_effort=selection.reasoning_effort,
                request_timeout_seconds=selection.request_timeout_seconds,
            )
        # User pins override the router (or the client default when no router ran).
        # Applied after the block so it covers every path uniformly; None leaves the
        # router/client value untouched (see create_message: model or self.model).
        if deps is not None and deps.forced_model is not None:
            model_override = deps.forced_model
        if deps is not None and deps.forced_reasoning_effort is not None:
            effort_override = deps.forced_reasoning_effort
        if deps is not None and (
            deps.forced_model is not None or deps.forced_reasoning_effort is not None
        ):
            run_tracer.event(
                "model_router.pinned",
                "Applied user model/reasoning override for this turn.",
                model=model_override,
                reasoning_effort=effort_override,
            )
        run_images = deps.images if deps is not None else ()
        run_prior_image_batches = (
            deps.prior_image_batches if deps is not None else None
        )
        run_image_context: ImageContext | None = (
            ImageContext(images=run_images, prior_batches=run_prior_image_batches)
            if run_images or run_prior_image_batches
            else None
        )
        try:
            if isinstance(run_agent_client, LLMAgentClient):
                if run_agent_client.async_client is not None:
                    assistant_message = await run_agent_client.async_create_message(
                        messages,
                        tool_schemas,
                        model=model_override,
                        reasoning_effort=effort_override,
                        request_timeout_seconds=timeout_override,
                        tracer=run_tracer,
                        usage_accumulator=run_usage_accumulator,
                        async_client=run_agent_client.async_client,
                        image_context=run_image_context,
                    )
                else:
                    assistant_message = await bounded_to_thread(
                        run_agent_client.create_message,
                        messages,
                        tool_schemas,
                        model=model_override,
                        reasoning_effort=effort_override,
                        request_timeout_seconds=timeout_override,
                        tracer=run_tracer,
                        usage_accumulator=run_usage_accumulator,
                        image_context=run_image_context,
                    )
            else:
                # Preserve duck-typed test/CLI clients while keeping their
                # legacy synchronous transports off the event loop.
                async_create_message = getattr(
                    run_agent_client,
                    "async_create_message",
                    None,
                )
                if inspect.iscoroutinefunction(async_create_message):
                    kwargs = {
                        "model": model_override,
                        "reasoning_effort": effort_override,
                        "request_timeout_seconds": timeout_override,
                    }
                    if run_image_context is not None:
                        kwargs["image_context"] = run_image_context
                    assistant_message = await async_create_message(
                        messages, tool_schemas, **kwargs
                    )
                else:
                    kwargs = {
                        "model": model_override,
                        "reasoning_effort": effort_override,
                        "request_timeout_seconds": timeout_override,
                    }
                    if run_image_context is not None:
                        kwargs["image_context"] = run_image_context
                    assistant_message = await bounded_to_thread(
                        run_agent_client.create_message,
                        messages,
                        tool_schemas,
                        **kwargs,
                    )
        except LLMAgentClientError as error:
            run_tracer.event(
                "graph.agent",
                "Stopping graph because the language-model provider failed.",
                provider=error.payload.get("provider") or error.payload.get("source"),
                error_type=error.payload.get("type"),
                attempts=error.payload.get("attempts"),
            )
            return {
                "error": json.dumps(error.payload, sort_keys=True),
                "final_response": LLM_FAILURE_MESSAGE,
                "next": "end",
            }
        messages.append(assistant_message)

        final_response = ""

        if not assistant_message.get("tool_calls"):
            content = assistant_message.get("content") or ""
            if _looks_like_question(content):
                synthetic_id = f"synth_{uuid4().hex[:8]}"
                assistant_message["tool_calls"] = [
                    {
                        "id": synthetic_id,
                        "type": "function",
                        "function": {
                            "name": "ask_user",
                            "arguments": json.dumps({"question": content}),
                        },
                    }
                ]
                run_tracer.event(
                    "agent.question_detected",
                    "Model asked in plain text; routing to HITL.",
                    synthetic_tool_call_id=synthetic_id,
                )
            else:
                final_response = content
                run_tracer.progress({"phase": "finalizing", "action": "started"})
                run_tracer.payload("agent.final", "content", final_response)
                run_log = getattr(run_tracer, "run_log", None)
                if run_log is not None:
                    run_log.write_messages_dump(
                        "final_turn_input (context sent to LLM on ANSWER turn)",
                        messages[:-1],
                    )

        run_tracer.event(
            "graph.route",
            "Agent node completed.",
            turn=turn_count + 1,
        )

        return {
            "messages": messages,
            "turn_count": turn_count + 1,
            "final_response": final_response,
            "selected_tool_names": selected_tool_names,
            "active_domains": active_domains,
            "router_outcome": (
                selector_decision.outcome.value if selector_decision is not None else None
            ),
        }

    return agent_node


# Temporary import compatibility for callers that still use the original names.
DeepSeekAgentClient = LLMAgentClient
DeepSeekAgentClientError = LLMAgentClientError


__all__ = [
    "DeepSeekAgentClient",
    "DeepSeekAgentClientError",
    "LLMAgentClient",
    "LLMAgentClientError",
    "LLM_FAILURE_MESSAGE",
    "UsageSummary",
    "_looks_like_question",
    "close_shared_agent_client",
    "close_shared_async_agent_client",
    "create_agent_node",
    "get_shared_agent_client",
    "get_shared_async_agent_client",
    "raw_message_from_openai",
    "_tool_schema_names",
    "usage_from_response",
]
