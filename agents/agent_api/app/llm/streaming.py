"""OpenAI Responses streaming with bounded reasoning-summary accumulation."""

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from openai.lib.streaming.responses import AsyncResponseStream, ResponseStream
from openai.types.responses import (
    Response,
    ResponseReasoningSummaryPartAddedEvent,
    ResponseReasoningSummaryPartDoneEvent,
    ResponseReasoningSummaryTextDeltaEvent,
)

_MAX_DISPLAY_CHARS = 3800
_MIN_EMIT_INTERVAL_S = 0.25


@dataclass
class SummaryAccumulator:
    """Bounded reasoning-summary accumulator for one model call."""

    _parts: list[str] = field(default_factory=list)
    _current_index: int = -1
    _dirty: bool = False
    _last_emit: float = 0.0

    def append_delta(self, event: ResponseReasoningSummaryTextDeltaEvent) -> None:
        if event.summary_index != self._current_index:
            if self._parts:
                self._parts.append("\n")
            self._current_index = event.summary_index
        self._parts.append(event.delta)
        self._dirty = True

    def snapshot(self) -> str:
        text = "".join(self._parts)
        if len(text) > _MAX_DISPLAY_CHARS:
            text = "…\n" + text[-_MAX_DISPLAY_CHARS:]
        return text

    def should_emit(self) -> bool:
        if not self._dirty:
            return False
        now = time.monotonic()
        if self._last_emit == 0.0:
            return True
        return (now - self._last_emit) >= _MIN_EMIT_INTERVAL_S

    def mark_emitted(self) -> None:
        self._dirty = False
        self._last_emit = time.monotonic()

    def has_content(self) -> bool:
        return bool(self._parts)


SummaryCallback = Callable[[str], None]


def consume_response_stream(
    stream: ResponseStream,
    on_summary: SummaryCallback | None = None,
) -> Response:
    """Iterate a sync ResponseStream, accumulate reasoning summaries, return final response."""
    acc = SummaryAccumulator()

    for event in stream:
        if isinstance(event, ResponseReasoningSummaryTextDeltaEvent):
            acc.append_delta(event)
            if on_summary and acc.should_emit():
                snap = acc.snapshot()
                if snap.strip():
                    on_summary(snap)
                    acc.mark_emitted()
        elif isinstance(
            event,
            (ResponseReasoningSummaryPartDoneEvent, ResponseReasoningSummaryPartAddedEvent),
        ):
            pass

    if on_summary and acc.has_content() and acc._dirty:
        snap = acc.snapshot()
        if snap.strip():
            on_summary(snap)
            acc.mark_emitted()

    return stream.get_final_response()


async def consume_async_response_stream(
    stream: AsyncResponseStream,
    on_summary: SummaryCallback | None = None,
) -> Response:
    """Iterate an async ResponseStream, accumulate reasoning summaries, return final response."""
    acc = SummaryAccumulator()

    async for event in stream:
        if isinstance(event, ResponseReasoningSummaryTextDeltaEvent):
            acc.append_delta(event)
            if on_summary and acc.should_emit():
                snap = acc.snapshot()
                if snap.strip():
                    on_summary(snap)
                    acc.mark_emitted()
        elif isinstance(
            event,
            (ResponseReasoningSummaryPartDoneEvent, ResponseReasoningSummaryPartAddedEvent),
        ):
            pass

    if on_summary and acc.has_content() and acc._dirty:
        snap = acc.snapshot()
        if snap.strip():
            on_summary(snap)
            acc.mark_emitted()

    return stream.get_final_response()


def is_summary_rejection(error: Any) -> bool:
    """Return True if an OpenAI error specifically rejects the reasoning.summary parameter."""
    status_code = getattr(error, "status_code", None)
    if status_code not in (400, 403):
        return False
    body = getattr(error, "body", None) or {}
    message = ""
    if isinstance(body, dict):
        message = str(body.get("message", ""))
    else:
        message = str(body)
    error_text = message.lower()
    keywords = ("reasoning.summary", "reasoning summary", "summarizer", "summary")
    return any(kw in error_text for kw in keywords)


def strip_summary_from_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of params with reasoning.summary removed."""
    params = dict(params)
    reasoning = params.get("reasoning")
    if isinstance(reasoning, dict):
        reasoning = {k: v for k, v in reasoning.items() if k != "summary"}
        params["reasoning"] = reasoning
    return params
