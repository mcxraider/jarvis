"""Run-bound Jev → LLM classification over shared, stateless transports."""

import threading
import time
from typing import Any, Callable, Optional, Protocol

from agents.agent_api.app.config import settings
from agents.agent_api.app.router.client import RouterClientError
from agents.agent_api.app.router.prompt import RouterDecision
from agents.agent_api.app.router.typesafe import TypeSafeRouterClient
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter
from agents.agent_api.app.user_context.runtime import RuntimeContextSnapshotLike


class DomainClassifier(Protocol):
    def classify(
        self, query: str, snapshot: RuntimeContextSnapshotLike, **kwargs: Any
    ) -> RouterDecision: ...

    async def async_classify(
        self, query: str, snapshot: RuntimeContextSnapshotLike, **kwargs: Any
    ) -> RouterDecision: ...


class RoutingChain:
    """A run-local facade: only final decisions enter the selector's cache."""

    def __init__(
        self,
        primary: Optional[DomainClassifier],
        fallback: Callable[[], DomainClassifier],
        *,
        tracer: TracePrinter = NULL_TRACE,
        usage_accumulator: Any = None,
    ):
        self.primary = primary
        self._fallback = fallback
        self._tracer = tracer
        self._usage = usage_accumulator

    def _fallback_event(self, reason: str, tracer: TracePrinter) -> None:
        tracer.event(
            "router.classifier_fallback",
            "Using LLM domain classifier.",
            reason=reason,
            provider="llm",
        )

    def _failure(self) -> RouterClientError:
        return RouterClientError(
            {
                "source": "router",
                "type": "classifiers_failed",
                "retryable": False,
                "message": "Domain classifiers unavailable",
            }
        )

    def classify(
        self, query: str, snapshot: RuntimeContextSnapshotLike, **kwargs: Any
    ) -> RouterDecision:
        context = {"tracer": self._tracer, "usage_accumulator": self._usage, **kwargs}
        started = time.perf_counter()
        decision = None
        reason = "unavailable"
        if self.primary is not None:
            try:
                decision = self.primary.classify(query, snapshot, **context)
            except RouterClientError:
                reason = "error"
            else:
                if not decision.uncertain:
                    return self._selected(
                        decision, "typesafe", started, context["tracer"]
                    )
                reason = "uncertain"
        self._fallback_event(reason, context["tracer"])
        try:
            result = self._fallback().classify(query, snapshot, **context)
        except Exception as error:
            # Construction errors count as unavailable classifiers, too.
            if decision is not None:
                return self._selected(
                    decision, "typesafe_candidates", started, context["tracer"]
                )
            raise self._failure() from error
        return self._selected(result, "llm", started, context["tracer"])

    async def async_classify(
        self, query: str, snapshot: RuntimeContextSnapshotLike, **kwargs: Any
    ) -> RouterDecision:
        context = {"tracer": self._tracer, "usage_accumulator": self._usage, **kwargs}
        started = time.perf_counter()
        decision = None
        reason = "unavailable"
        if self.primary is not None:
            try:
                decision = await self.primary.async_classify(query, snapshot, **context)
            except RouterClientError:
                reason = "error"
            else:
                if not decision.uncertain:
                    return self._selected(
                        decision, "typesafe", started, context["tracer"]
                    )
                reason = "uncertain"
        self._fallback_event(reason, context["tracer"])
        try:
            result = await self._fallback().async_classify(query, snapshot, **context)
        except Exception as error:
            # CancelledError is a BaseException and deliberately propagates.
            if decision is not None:
                return self._selected(
                    decision, "typesafe_candidates", started, context["tracer"]
                )
            raise self._failure() from error
        return self._selected(result, "llm", started, context["tracer"])

    def _selected(
        self,
        decision: RouterDecision,
        source: str,
        started: float,
        tracer: TracePrinter,
    ) -> RouterDecision:
        tracer.event(
            "router.classifier_selected",
            "Domain classification complete.",
            classifier=source,
            outcome=decision.outcome.value,
            domains=[domain.value for domain in decision.domains],
            uncertain=decision.uncertain,
            total_elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return decision


_client: Optional[TypeSafeRouterClient] = None
_lock = threading.Lock()


def get_shared_typesafe_router() -> TypeSafeRouterClient:
    global _client
    with _lock:
        if _client is None:
            _client = TypeSafeRouterClient(
                settings.typesafe_api_key or "",
                model=settings.typesafe_model,
                base_url=settings.typesafe_base_url,
                request_timeout_seconds=settings.typesafe_router_request_timeout_seconds,
                max_retry_attempts=settings.typesafe_router_max_retry_attempts,
                retry_max_delay_seconds=settings.typesafe_router_retry_max_delay_seconds,
            )
        return _client


def build_runtime_router(
    fallback: Callable[[], DomainClassifier],
    *,
    tracer: TracePrinter,
    usage_accumulator: Any = None,
) -> RoutingChain:
    primary = None
    if settings.router_engine == "typesafe" and settings.typesafe_api_key:
        try:
            primary = get_shared_typesafe_router()
        except Exception:
            tracer.event(
                "router.primary_unavailable", "TypeSafe transport unavailable."
            )
    return RoutingChain(
        primary, fallback, tracer=tracer, usage_accumulator=usage_accumulator
    )


def close_shared_typesafe_router() -> None:
    # Keep the wrapper until async cleanup closes its separately owned pool.
    with _lock:
        if _client is not None:
            _client.close()


async def close_shared_async_typesafe_router() -> None:
    global _client
    with _lock:
        client, _client = _client, None
    if client is not None:
        await client.aclose()
