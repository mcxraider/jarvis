"""Synchronous DeepSeek-backed client for the query router.

This mirrors :class:`DeepSeekAgentClient` deliberately: the whole agent path is
synchronous (``select_schemas`` and the graph node are sync), so the router call
is sync too. It reuses the same OpenAI-compatible endpoint and structured-error
contract as the orchestrator, but with a tighter, non-critical budget (shorter
timeout, fewer Tenacity attempts, and no SDK-internal retries). The router is
**never a hard-failure path**: callers degrade to the static selector on any
:class:`RouterClientError`.

Two things differ from the orchestrator client:
1. Reasoning is OFF by default — the router is a fast classifier, so it
   explicitly disables DeepSeek ``thinking`` mode unless configured otherwise.
   We request DeepSeek's stable JSON Output mode. The provider guarantees JSON
   syntax; :class:`RouterDecision` remains the authoritative schema and is
   enforced locally with Pydantic.
2. The failure surface is wider: a non-retryable transport error AND an
   unparseable / schema-invalid completion both raise ``RouterClientError``. A
   malformed body is not worth retrying (the model will likely repeat it), so it
   is reported as a terminal, non-retryable failure and the caller falls back.
"""

import json
import os
import threading
import time
from contextlib import suppress
from typing import Any, Dict, List, Optional

from langsmith import traceable
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

from agents.agent_api.app.constants import (
    ROUTER_API_KEY,
    ROUTER_BASE_URL,
    ROUTER_MAX_RETRY_ATTEMPTS,
    ROUTER_MODEL,
    ROUTER_REASONING_EFFORT,
    ROUTER_REQUEST_TIMEOUT_SECONDS,
    ROUTER_RETRY_MAX_DELAY_SECONDS,
)
from agents.agent_api.app.graph.nodes.orchestrator import UsageSummary, usage_from_response
from agents.agent_api.app.router.prompt import RouterDecision, build_router_messages
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter
from agents.agent_api.app.user_context.runtime import RuntimeContextSnapshot

# Cap the classifier's output. It only ever returns a small JSON object
# (outcome, domains, uncertainty, complexity, and short reasoning), so a tight
# ceiling keeps the call fast and cheap without ever truncating a valid decision.
_ROUTER_MAX_TOKENS = 400
_ROUTER_SDK_MAX_RETRIES = 0
_THINKING_DISABLED = {"thinking": {"type": "disabled"}}
_THINKING_ENABLED = {"thinking": {"type": "enabled"}}


# SDK transports are the expensive, connection-pool-owning objects. The default
# router configuration shares one sync and one async transport process-wide;
# lightweight RouterClient wrappers keep any compatibility defaults scoped to a
# caller and pass request context explicitly on every classification.
_shared_router_openai_client: Optional[OpenAI] = None
_shared_router_openai_client_lock = threading.Lock()
_shared_async_router_openai_client: Optional[AsyncOpenAI] = None
_shared_async_router_openai_client_lock = threading.Lock()


def _default_api_key() -> Optional[str]:
    return ROUTER_API_KEY or os.getenv("DEEPSEEK_API_KEY")


def _build_router_openai_client(
    *,
    api_key: str,
    base_url: str,
    request_timeout_seconds: float,
) -> OpenAI:
    return wrap_openai(
        OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=request_timeout_seconds,
            max_retries=_ROUTER_SDK_MAX_RETRIES,
        )
    )


def _build_async_router_openai_client(
    *,
    api_key: str,
    base_url: str,
    request_timeout_seconds: float,
) -> AsyncOpenAI:
    return wrap_openai(
        AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=request_timeout_seconds,
            max_retries=_ROUTER_SDK_MAX_RETRIES,
        )
    )


def get_shared_router_openai_client() -> OpenAI:
    """Return the lazily-created default sync router SDK transport."""

    global _shared_router_openai_client
    client = _shared_router_openai_client
    if client is not None:
        return client
    with _shared_router_openai_client_lock:
        if _shared_router_openai_client is None:
            api_key = _default_api_key()
            if not api_key:
                raise RuntimeError(
                    "ROUTER_API_KEY (or DEEPSEEK_API_KEY) is required to run the router."
                )
            _shared_router_openai_client = _build_router_openai_client(
                api_key=api_key,
                base_url=ROUTER_BASE_URL,
                request_timeout_seconds=ROUTER_REQUEST_TIMEOUT_SECONDS,
            )
        return _shared_router_openai_client


def get_shared_async_router_openai_client() -> AsyncOpenAI:
    """Return the lazily-created default async router SDK transport."""

    global _shared_async_router_openai_client
    client = _shared_async_router_openai_client
    if client is not None:
        return client
    with _shared_async_router_openai_client_lock:
        if _shared_async_router_openai_client is None:
            api_key = _default_api_key()
            if not api_key:
                raise RuntimeError(
                    "ROUTER_API_KEY (or DEEPSEEK_API_KEY) is required to run the router."
                )
            _shared_async_router_openai_client = _build_async_router_openai_client(
                api_key=api_key,
                base_url=ROUTER_BASE_URL,
                request_timeout_seconds=ROUTER_REQUEST_TIMEOUT_SECONDS,
            )
        return _shared_async_router_openai_client


def close_shared_router_openai_client() -> None:
    """Close and clear the default sync router SDK transport."""

    global _shared_router_openai_client
    with _shared_router_openai_client_lock:
        client = _shared_router_openai_client
        _shared_router_openai_client = None
    if client is not None:
        client.close()


async def close_shared_async_router_openai_client() -> None:
    """Close and clear the default async router SDK transport."""

    global _shared_async_router_openai_client
    with _shared_async_router_openai_client_lock:
        client = _shared_async_router_openai_client
        _shared_async_router_openai_client = None
    if client is not None:
        await client.close()


class RouterClientError(RuntimeError):
    """Terminal router failure with graph-safe structured metadata.

    Mirrors ``DeepSeekAgentClientError`` so callers can treat both uniformly. The
    payload always carries ``source="router"`` so a failure is attributable even
    when both clients share a trace.
    """

    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        super().__init__(json.dumps(payload, sort_keys=True))


class RouterClient:
    """Sync wrapper around the router's OpenAI-compatible chat API.

    Non-critical by contract: every failure raises :class:`RouterClientError`,
    and the selector that owns this client degrades to the static (all-tools)
    selector rather than failing the run.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = ROUTER_MODEL,
        base_url: str = ROUTER_BASE_URL,
        reasoning_effort: str = ROUTER_REASONING_EFFORT,
        tracer: Optional[TracePrinter] = None,
        request_timeout_seconds: float = ROUTER_REQUEST_TIMEOUT_SECONDS,
        max_retry_attempts: int = ROUTER_MAX_RETRY_ATTEMPTS,
        retry_max_delay_seconds: float = ROUTER_RETRY_MAX_DELAY_SECONDS,
        retry_sleep: Optional[Any] = None,
        client: Optional[OpenAI] = None,
        async_client: Optional[AsyncOpenAI] = None,
    ):
        # ROUTER_API_KEY already falls back to DEEPSEEK_API_KEY at settings load;
        # keep the env fallback here too so a directly-constructed client works.
        self.api_key = api_key or _default_api_key()
        self._default_model = model
        self.request_timeout_seconds = request_timeout_seconds
        # Reasoning stays off for the classifier: an effort of "off"/"none"/""
        # means we explicitly disable DeepSeek thinking mode. DeepSeek defaults
        # thinking to enabled, so omission is not enough for the fast router.
        self._default_reasoning_effort = reasoning_effort
        self.base_url = base_url
        self._default_tracer = tracer or NULL_TRACE
        # Compatibility accumulator for direct callers. Production passes an
        # explicit RunDeps-owned accumulator, so the shared transport never owns
        # mutable request usage.
        self.usage = UsageSummary()
        self.max_retry_attempts = max(1, max_retry_attempts)
        self.retry_max_delay_seconds = retry_max_delay_seconds
        self.retry_sleep = retry_sleep
        if not self.api_key and client is None:
            raise RuntimeError("ROUTER_API_KEY (or DEEPSEEK_API_KEY) is required to run the router.")

        self._uses_default_transport = (
            client is None
            and api_key is None
            and base_url == ROUTER_BASE_URL
            and request_timeout_seconds == ROUTER_REQUEST_TIMEOUT_SECONDS
        )
        self._owns_sync_client = False
        if client is not None:
            self.client = client
        elif self._uses_default_transport:
            self.client = get_shared_router_openai_client()
        else:
            if not self.api_key:
                raise RuntimeError(
                    "An API key is required when no sync router client is injected."
                )
            self.client = _build_router_openai_client(
                api_key=self.api_key,
                base_url=base_url,
                request_timeout_seconds=request_timeout_seconds,
            )
            self._owns_sync_client = True
        self._async_client = async_client
        self._owns_async_client = False
        self._async_client_lock = threading.Lock()

    @property
    def model(self) -> str:
        """Compatibility view of the immutable default model."""

        return self._default_model

    @property
    def reasoning_effort(self) -> str:
        """Compatibility view of the immutable default reasoning setting."""

        return self._default_reasoning_effort

    @property
    def tracer(self) -> TracePrinter:
        """Compatibility view of the wrapper's default tracer binding."""

        return self._default_tracer

    def with_tracer(self, tracer: Optional[TracePrinter]) -> "RouterClient":
        """Return a lightweight binding that reuses the same SDK transports."""

        bound = RouterClient(
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            reasoning_effort=self.reasoning_effort,
            tracer=tracer,
            request_timeout_seconds=self.request_timeout_seconds,
            max_retry_attempts=self.max_retry_attempts,
            retry_max_delay_seconds=self.retry_max_delay_seconds,
            retry_sleep=self.retry_sleep,
            client=self.client,
            async_client=self._async_client,
        )
        bound._uses_default_transport = self._uses_default_transport
        return bound

    def _get_async_client(self) -> AsyncOpenAI:
        client = self._async_client
        if client is not None:
            return client
        with self._async_client_lock:
            if self._async_client is None:
                if self._uses_default_transport:
                    self._async_client = get_shared_async_router_openai_client()
                else:
                    if not self.api_key:
                        raise RuntimeError(
                            "An async router client must be injected when no API key is configured."
                        )
                    self._async_client = _build_async_router_openai_client(
                        api_key=self.api_key,
                        base_url=self.base_url,
                        request_timeout_seconds=self.request_timeout_seconds,
                    )
                    self._owns_async_client = True
            return self._async_client

    @staticmethod
    def _thinking_enabled(reasoning_effort: str) -> bool:
        return reasoning_effort.strip().lower() not in {"", "off", "none", "disabled"}

    @traceable(
        name="router_classify",
        run_type="llm",
    )
    def classify(
        self,
        query: str,
        snapshot: RuntimeContextSnapshot,
        *,
        tracer: Optional[TracePrinter] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        usage_accumulator: Optional[UsageSummary] = None,
    ) -> RouterDecision:
        """Classify a query without storing request state on the client.

        The return type intentionally remains ``RouterDecision`` for synchronous
        selectors and test fakes. Token usage is written only to the explicitly
        supplied per-run accumulator.
        """

        call_tracer = tracer or self.tracer
        call_model = model or self.model
        call_reasoning = (
            self.reasoning_effort if reasoning_effort is None else reasoning_effort
        )
        messages = build_router_messages(query, snapshot)
        self._trace_payload(
            call_tracer,
            "router.prompt",
            "system_prompt",
            messages[0]["content"],
            limit=len(messages[0]["content"]) + 1,
        )
        self._trace_payload(
            call_tracer,
            "router.prompt",
            "user_prompt",
            messages[1]["content"],
            limit=len(messages[1]["content"]) + 1,
        )
        classify_started = time.monotonic()
        call_tracer.event(
            "router.request",
            "Calling router classifier.",
            model=call_model,
            reasoning=call_reasoning,
            messages=len(messages),
            request_timeout_seconds=self.request_timeout_seconds,
            sdk_max_retries=_ROUTER_SDK_MAX_RETRIES,
            max_retry_attempts=self.max_retry_attempts,
            retry_max_delay_seconds=self.retry_max_delay_seconds,
            base_url=self.base_url,
            response_format="json_object",
            thinking_enabled=self._thinking_enabled(call_reasoning),
        )
        attempts = 0

        def create_completion() -> Any:
            nonlocal attempts
            attempts += 1
            attempt_started = time.monotonic()
            call_tracer.event(
                "router.attempt.start",
                "Starting router classification attempt.",
                attempt=attempts,
                request_timeout_seconds=self.request_timeout_seconds,
            )
            kwargs: Dict[str, Any] = {
                "model": call_model,
                "messages": messages,
                "max_tokens": _ROUTER_MAX_TOKENS,
                "response_format": {"type": "json_object"},
            }
            if self._thinking_enabled(call_reasoning):
                kwargs["reasoning_effort"] = call_reasoning
                kwargs["extra_body"] = _THINKING_ENABLED
            else:
                kwargs["extra_body"] = _THINKING_DISABLED
            try:
                response = self.client.chat.completions.create(**kwargs)
            except Exception as error:
                error_details = self._error_details(error)
                call_tracer.event(
                    "router.attempt.error",
                    "Router classification attempt failed.",
                    attempt=attempts,
                    error_type=self._error_type(error),
                    status_code=self._status_code(error),
                    elapsed_ms=round((time.monotonic() - attempt_started) * 1000, 1),
                    **error_details,
                )
                raise
            call_tracer.event(
                "router.attempt.done",
                "Router classification attempt completed.",
                attempt=attempts,
                elapsed_ms=round((time.monotonic() - attempt_started) * 1000, 1),
            )
            return response

        try:
            response = self._retrying(call_tracer)(create_completion)
        except Exception as error:
            total_elapsed_ms = round((time.monotonic() - classify_started) * 1000, 1)
            payload = self._failure_payload(error, attempts)
            payload["total_elapsed_ms"] = total_elapsed_ms
            call_tracer.event(
                "router.error",
                "Router classification failed.",
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
            raise RouterClientError(payload) from error

        # Read usage before parsing so a downstream parse failure still records tokens.
        turn_usage = usage_from_response(response)
        call_usage = usage_accumulator if usage_accumulator is not None else self.usage
        call_usage.add(turn_usage)

        decision = self._parse_decision(response, attempts, call_tracer)
        call_tracer.event(
            "router.response",
            "Received router decision.",
            domains=len(decision.domains),
            outcome=decision.outcome.value,
            complexity=decision.complexity.value,
            prompt_tokens=turn_usage.prompt_tokens or None,
            completion_tokens=turn_usage.completion_tokens or None,
            total_elapsed_ms=round((time.monotonic() - classify_started) * 1000, 1),
        )
        return decision

    @traceable(name="router_classify_async", run_type="llm")
    async def async_classify(
        self,
        query: str,
        snapshot: RuntimeContextSnapshot,
        *,
        tracer: Optional[TracePrinter] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        usage_accumulator: Optional[UsageSummary] = None,
        async_client: Optional[AsyncOpenAI] = None,
    ) -> RouterDecision:
        """Async classification using the shared async SDK transport by default."""

        call_tracer = tracer or self.tracer
        call_model = model or self.model
        call_reasoning = (
            self.reasoning_effort if reasoning_effort is None else reasoning_effort
        )
        client = async_client or self._get_async_client()
        messages = build_router_messages(query, snapshot)
        self._trace_payload(
            call_tracer,
            "router.prompt",
            "system_prompt",
            messages[0]["content"],
            limit=len(messages[0]["content"]) + 1,
        )
        self._trace_payload(
            call_tracer,
            "router.prompt",
            "user_prompt",
            messages[1]["content"],
            limit=len(messages[1]["content"]) + 1,
        )
        classify_started = time.monotonic()
        call_tracer.event(
            "router.request",
            "Calling router classifier.",
            model=call_model,
            reasoning=call_reasoning,
            messages=len(messages),
            request_timeout_seconds=self.request_timeout_seconds,
            sdk_max_retries=_ROUTER_SDK_MAX_RETRIES,
            max_retry_attempts=self.max_retry_attempts,
            retry_max_delay_seconds=self.retry_max_delay_seconds,
            base_url=self.base_url,
            response_format="json_object",
            thinking_enabled=self._thinking_enabled(call_reasoning),
            async_request=True,
        )
        attempts = 0

        async def create_completion() -> Any:
            nonlocal attempts
            attempts += 1
            attempt_started = time.monotonic()
            call_tracer.event(
                "router.attempt.start",
                "Starting router classification attempt.",
                attempt=attempts,
                request_timeout_seconds=self.request_timeout_seconds,
                async_request=True,
            )
            kwargs: Dict[str, Any] = {
                "model": call_model,
                "messages": messages,
                "max_tokens": _ROUTER_MAX_TOKENS,
                "response_format": {"type": "json_object"},
            }
            if self._thinking_enabled(call_reasoning):
                kwargs["reasoning_effort"] = call_reasoning
                kwargs["extra_body"] = _THINKING_ENABLED
            else:
                kwargs["extra_body"] = _THINKING_DISABLED
            try:
                response = await client.chat.completions.create(**kwargs)
            except Exception as error:
                error_details = self._error_details(error)
                call_tracer.event(
                    "router.attempt.error",
                    "Router classification attempt failed.",
                    attempt=attempts,
                    error_type=self._error_type(error),
                    status_code=self._status_code(error),
                    elapsed_ms=round((time.monotonic() - attempt_started) * 1000, 1),
                    async_request=True,
                    **error_details,
                )
                raise
            call_tracer.event(
                "router.attempt.done",
                "Router classification attempt completed.",
                attempt=attempts,
                elapsed_ms=round((time.monotonic() - attempt_started) * 1000, 1),
                async_request=True,
            )
            return response

        try:
            async for attempt in self._async_retrying(call_tracer):
                with attempt:
                    response = await create_completion()
        except Exception as error:
            total_elapsed_ms = round((time.monotonic() - classify_started) * 1000, 1)
            payload = self._failure_payload(error, attempts)
            payload["total_elapsed_ms"] = total_elapsed_ms
            call_tracer.event(
                "router.error",
                "Router classification failed.",
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
                async_request=True,
            )
            raise RouterClientError(payload) from error

        turn_usage = usage_from_response(response)
        call_usage = usage_accumulator if usage_accumulator is not None else self.usage
        call_usage.add(turn_usage)
        decision = self._parse_decision(response, attempts, call_tracer)
        call_tracer.event(
            "router.response",
            "Received router decision.",
            domains=len(decision.domains),
            outcome=decision.outcome.value,
            complexity=decision.complexity.value,
            prompt_tokens=turn_usage.prompt_tokens or None,
            completion_tokens=turn_usage.completion_tokens or None,
            total_elapsed_ms=round((time.monotonic() - classify_started) * 1000, 1),
            async_request=True,
        )
        return decision

    def _trace_payload(
        self,
        tracer: TracePrinter,
        stage: str,
        label: str,
        value: Any,
        limit: int,
    ) -> None:
        payload = getattr(tracer, "payload", None)
        if callable(payload):
            payload(stage, label, value, limit=limit)

    def _parse_decision(
        self,
        response: Any,
        attempts: int,
        tracer: TracePrinter,
    ) -> RouterDecision:
        """Parse + validate the completion body into a RouterDecision.

        A malformed body (not JSON, or JSON that violates the schema) is a
        terminal, non-retryable failure — retrying rarely helps and the caller
        falls back to the static selector.
        """

        content = self._response_content(response)
        if not content:
            raise self._invalid_response(
                "router returned an empty response",
                attempts,
                tracer,
                content_length=0,
            )
        try:
            data = json.loads(content)
        except (TypeError, ValueError) as error:
            raise self._invalid_response(
                "router response was not valid JSON",
                attempts,
                tracer,
                content_length=len(content),
                response_kind=type(content).__name__,
            ) from error
        try:
            return RouterDecision.model_validate(data)
        except ValidationError as error:
            raise self._invalid_response(
                "router response did not match the decision schema",
                attempts,
                tracer,
                content_length=len(content),
                validation_error_count=len(error.errors()),
            ) from error

    @staticmethod
    def _response_content(response: Any) -> str:
        try:
            message = response.choices[0].message
        except (AttributeError, IndexError, TypeError):
            return ""
        content = getattr(message, "content", None)
        return content if isinstance(content, str) else ""

    def _invalid_response(
        self,
        message: str,
        attempts: int,
        tracer: TracePrinter,
        **details: Any,
    ) -> RouterClientError:
        payload = {
            "source": "router",
            "type": "invalid_response",
            "retryable": False,
            "attempts": attempts,
            "message": message,
            **details,
        }
        tracer.event(
            "router.error",
            "Router returned an unparseable response.",
            error_type=payload["type"],
            retryable=payload["retryable"],
            attempts=payload["attempts"],
            content_length=payload.get("content_length"),
            validation_error_count=payload.get("validation_error_count"),
            response_kind=payload.get("response_kind"),
        )
        return RouterClientError(payload)

    def _retrying(self, tracer: TracePrinter) -> Retrying:
        sleep = self.retry_sleep if self.retry_sleep is not None else tenacity_sleep
        return Retrying(
            retry=retry_if_exception(self._is_retryable_error),
            wait=wait_random_exponential(multiplier=1, max=self.retry_max_delay_seconds),
            stop=stop_after_attempt(self.max_retry_attempts),
            reraise=True,
            sleep=sleep,
            before_sleep=lambda retry_state: self._trace_retry(retry_state, tracer),
        )

    def _async_retrying(self, tracer: TracePrinter) -> AsyncRetrying:
        return AsyncRetrying(
            retry=retry_if_exception(self._is_retryable_error),
            wait=wait_random_exponential(multiplier=1, max=self.retry_max_delay_seconds),
            stop=stop_after_attempt(self.max_retry_attempts),
            reraise=True,
            before_sleep=lambda retry_state: self._trace_retry(retry_state, tracer),
        )

    def _trace_retry(self, retry_state: Any, tracer: TracePrinter) -> None:
        error = retry_state.outcome.exception() if retry_state.outcome else None
        tracer.event(
            "router.retry",
            "Retrying router classification.",
            attempt=retry_state.attempt_number,
            error_type=self._error_type(error),
            status_code=self._status_code(error),
            provider_request_id=self._provider_request_id(error),
            exception_type=type(error).__name__ if error is not None else None,
            timeout_kind=self._timeout_kind(error),
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
                "retry": {"target": "router", "reason": "temporary_connection"},
            })

    def _is_retryable_error(self, error: BaseException) -> bool:
        if isinstance(error, (APITimeoutError, APIConnectionError)):
            return True

        status_code = self._status_code(error)
        if status_code == 429:
            return True
        if isinstance(error, APIStatusError) and status_code is not None:
            return status_code >= 500

        return isinstance(error, RateLimitError)

    def _failure_payload(self, error: BaseException, attempts: int) -> Dict[str, Any]:
        status_code = self._status_code(error)
        payload: Dict[str, Any] = {
            "source": "router",
            "type": self._error_type(error),
            "retryable": self._is_retryable_error(error),
            "attempts": attempts,
            "message": str(error),
            "error_message": str(error),
            "base_url": self.base_url,
            "request_timeout_seconds": self.request_timeout_seconds,
            "sdk_max_retries": _ROUTER_SDK_MAX_RETRIES,
            "retry_max_delay_seconds": self.retry_max_delay_seconds,
            "max_retry_attempts": self.max_retry_attempts,
            "total_elapsed_ms": 0.0,
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
        current = error
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
            if name == "timeout":
                return "timeout"
            current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        if isinstance(error, APITimeoutError):
            return "timeout"
        return None

    def _error_type(self, error: Optional[BaseException]) -> str:
        if error is None:
            return "unexpected"
        if isinstance(error, APITimeoutError):
            return "timeout"
        if isinstance(error, APIConnectionError):
            return "connection_error"

        status_code = self._status_code(error)
        if status_code == 429 or isinstance(error, RateLimitError):
            return "rate_limit"
        if status_code is not None and status_code >= 500:
            return "server_error"
        if status_code is not None and 400 <= status_code < 500:
            return "client_error"

        return "unexpected"

    @staticmethod
    def _status_code(error: Optional[BaseException]) -> Optional[int]:
        status_code = getattr(error, "status_code", None)
        return status_code if isinstance(status_code, int) else None

    def close(self) -> None:
        """Close only a transport owned by this compatibility wrapper."""

        if self._owns_sync_client:
            with suppress(Exception):
                self.client.close()

    async def aclose(self) -> None:
        """Close only an async transport owned by this compatibility wrapper."""

        if self._owns_async_client and self._async_client is not None:
            with suppress(Exception):
                await self._async_client.close()


_shared_router_client: Optional[RouterClient] = None
_shared_router_client_lock = threading.Lock()


def get_shared_router_client() -> RouterClient:
    """Return a request-stateless wrapper over the shared SDK transports."""

    global _shared_router_client
    client = _shared_router_client
    if client is not None:
        return client
    with _shared_router_client_lock:
        if _shared_router_client is None:
            _shared_router_client = RouterClient()
        return _shared_router_client


def close_shared_router_client() -> None:
    """Clear the shared wrapper and close its sync SDK connection pool."""

    global _shared_router_client
    with _shared_router_client_lock:
        _shared_router_client = None
    close_shared_router_openai_client()


# Source-branch spellings retained while callers migrate to the explicit
# ``*_client`` names.
get_shared_async_router_openai = get_shared_async_router_openai_client
close_shared_async_router_openai = close_shared_async_router_openai_client


__all__ = [
    "RouterClient",
    "RouterClientError",
    "close_shared_async_router_openai",
    "close_shared_async_router_openai_client",
    "close_shared_router_client",
    "close_shared_router_openai_client",
    "get_shared_async_router_openai",
    "get_shared_async_router_openai_client",
    "get_shared_router_client",
    "get_shared_router_openai_client",
]
