"""Invocation routes for starting Jarvis runs."""

import json
import logging
import queue
import threading
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from agents.agent_api.app.api.admission import (
    RunSlot,
    capacity_exceeded,
    try_acquire_run_slot,
)
from agents.agent_api.app.api.request_idempotency import RequestClaim
from agents.agent_api.app.api.schemas import AgentResponse, BulkAgentResponse, BulkInvokeRequest, InvokeRequest
from agents.agent_api.app.errors import require_api_key
from agents.agent_api.app.middleware import idempotency
import agents.agent_api.app.middleware.rate_limit as rate_limit
from agents.agent_api.app.middleware.request_gate import apply_request_gate
from agents.agent_api.app.service import ALLOW_MUTATIONS, MAX_AGENT_TURNS, NULL_TRACE, JarvisState, run_jarvis
from agents.agent_api.app.tracing import UserProgressTracePrinter

router = APIRouter()
logger = logging.getLogger(__name__)

STREAM_LIVENESS_TIMEOUT_SECONDS = 120.0
STREAM_WORKER_DRAIN_TIMEOUT_SECONDS = 5.0

_active_stream_workers: set[threading.Thread] = set()
_active_stream_workers_lock = threading.Lock()


def _start_registered_stream_worker(worker_thread: threading.Thread) -> None:
    """Publish and start a worker atomically with respect to shutdown drain."""

    with _active_stream_workers_lock:
        _active_stream_workers.add(worker_thread)
        try:
            worker_thread.start()
        except BaseException:
            _active_stream_workers.discard(worker_thread)
            raise


def _unregister_stream_worker(worker_thread: threading.Thread) -> None:
    with _active_stream_workers_lock:
        _active_stream_workers.discard(worker_thread)


def drain_stream_workers(
    timeout: float = STREAM_WORKER_DRAIN_TIMEOUT_SECONDS,
) -> bool:
    """Wait up to ``timeout`` seconds for active stream workers to finish."""

    deadline = time.monotonic() + max(0.0, timeout)
    current_thread = threading.current_thread()
    while True:
        with _active_stream_workers_lock:
            workers = tuple(
                worker
                for worker in _active_stream_workers
                if worker is not current_thread
            )
        if not workers:
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                break


def allow_mutations(request_value: Optional[bool]) -> bool:
    if request_value is not None:
        return request_value
    return ALLOW_MUTATIONS


def allow_bulk_mutations(request_value: Optional[bool]) -> bool:
    if request_value is not None:
        return request_value
    return ALLOW_MUTATIONS


request_source = idempotency.request_source


def parse_error_details(error: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(error)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def to_response(result: JarvisState) -> AgentResponse:
    thread_id = str(result.get("thread_id") or "")
    tool_results = result.get("tool_results", [])

    if result.get("interrupted"):
        interrupt = result.get("interrupt_payload", {})
        if interrupt.get("type") == "confirm":
            question = f"⚠️ Please Confirm: {interrupt.get('summary', 'Action requires approval.')}"
        else:
            question = str(
                interrupt.get("question")
                or "Jarvis needs more information before continuing."
            )
        return AgentResponse(
            status="interrupted",
            thread_id=thread_id,
            response=question,
            interrupt=interrupt,
            tool_results=tool_results,
        )

    if result.get("error"):
        error = str(result.get("error"))
        user_response = str(result.get("final_response") or "Jarvis could not complete that request.")
        return AgentResponse(
            status="failed",
            thread_id=thread_id,
            response=user_response,
            tool_results=tool_results,
            error=error,
            error_details=parse_error_details(error),
        )

    return AgentResponse(
        status="completed",
        thread_id=thread_id,
        response=str(result.get("final_response") or ""),
        tool_results=tool_results,
    )


def response_payload(response: AgentResponse) -> Dict[str, Any]:
    if hasattr(response, "model_dump"):
        return response.model_dump(exclude_none=True)
    return response.dict(exclude_none=True)


begin_idempotent_request = idempotency.begin_idempotent_request
finish_idempotent_request = idempotency.finish_idempotent_request


def stream_final_response(response: AgentResponse) -> StreamingResponse:
    event = {"type": "final", "response": response_payload(response)}
    return StreamingResponse(
        iter([json.dumps(event, default=str) + "\n"]),
        media_type="application/x-ndjson",
    )


def stream_agent_run(
    run_callable: Any,
    request_claim: Optional[RequestClaim] = None,
    run_slot: Optional[RunSlot] = None,
):
    def cleanup_failed_start() -> None:
        """Release route-owned resources when worker ownership never begins."""

        try:
            if request_claim is not None:
                idempotency.abandon_idempotent_request(request_claim)
        except Exception:
            # Claim cleanup is best-effort; capacity must still be returned and
            # the original stream-start failure should reach the caller.
            pass
        finally:
            if run_slot is not None:
                run_slot.release()

    try:
        events: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
    except BaseException:
        cleanup_failed_start()
        raise
    sequence = 0

    def emit_progress(progress: Dict[str, Any]) -> None:
        nonlocal sequence
        sequence += 1
        event = {
            "type": "progress",
            "sequence": sequence,
            # Preserve legacy fields for clients that have not adopted facts.
            "stage": progress.get("stage", "progress"),
            "message": progress.get("message", "Jarvis is working"),
        }
        if isinstance(progress.get("fact"), dict):
            event["fact"] = progress["fact"]
        events.put(event)

    def worker() -> None:
        try:
            result = run_callable(UserProgressTracePrinter(emit_progress, enabled=False))
            response = to_response(result)
            if request_claim is not None:
                finish_idempotent_request(request_claim, response)
            events.put({"type": "final", "response": response_payload(response)})
        except Exception as error:
            logger.exception(
                "Jarvis streaming invocation failed before producing a result.",
                extra={"error": str(error)},
            )
            if request_claim is not None:
                idempotency.abandon_idempotent_request(request_claim)
            events.put(
                {
                    "type": "final",
                    "response": response_payload(
                        AgentResponse(
                            status="failed",
                            thread_id="",
                            response="Jarvis is temporarily unavailable. Please try again in a moment.",
                            error=str(error),
                        )
                    ),
                }
            )
        finally:
            try:
                if run_slot is not None:
                    run_slot.release()
            finally:
                try:
                    events.put(None)
                finally:
                    _unregister_stream_worker(threading.current_thread())

    worker_thread: Optional[threading.Thread] = None
    try:
        worker_thread = threading.Thread(target=worker, daemon=True)
        _start_registered_stream_worker(worker_thread)
    except BaseException:
        cleanup_failed_start()
        raise

    def iterator():
        while True:
            try:
                event = events.get(timeout=STREAM_LIVENESS_TIMEOUT_SECONDS)
            except queue.Empty:
                if not worker_thread.is_alive():
                    logger.error("Stream worker thread died without sending sentinel.")
                    yield json.dumps({
                        "type": "final",
                        "response": {
                            "status": "failed",
                            "thread_id": "",
                            "response": "Jarvis encountered an internal error. Please try again.",
                            "error": "Worker thread terminated unexpectedly.",
                        },
                    }, default=str) + "\n"
                    break
                continue
            if event is None:
                break
            yield json.dumps(event, default=str) + "\n"

    return StreamingResponse(iterator(), media_type="application/x-ndjson")


@router.post("/invoke", response_model=AgentResponse)
def invoke(
    request: InvokeRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> AgentResponse:
    ctx = apply_request_gate(
        "invoke",
        request,
        x_jarvis_agent_key,
        charges_new_thread_quota=True,
        require_thread_ownership=True,
        admit_run=True,
    )
    try:
        if ctx.cached_response is not None:
            return ctx.cached_response
        try:
            result = run_jarvis(
                user_prompt=request.message,
                user_id=request.user_id,
                request_source=ctx.request_source,
                allow_mutations=allow_mutations(request.allow_mutations),
                tracer=NULL_TRACE,
                thread_id=request.thread_id,
                identity=ctx.identity,
                request_id=request.request_id,
            )
            response = to_response(result)
            finish_idempotent_request(ctx.claim, response)
            return response
        except Exception as error:
            idempotency.abandon_idempotent_request(ctx.claim)
            return AgentResponse(
                status="failed",
                thread_id=request.thread_id or "",
                response="Jarvis is temporarily unavailable. Please try again in a moment.",
                error=str(error),
            )
    finally:
        if ctx.run_slot is not None:
            ctx.run_slot.release()


@router.post("/invoke/stream")
def invoke_stream(
    request: InvokeRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> StreamingResponse:
    ctx = apply_request_gate(
        "invoke",
        request,
        x_jarvis_agent_key,
        charges_new_thread_quota=True,
        require_thread_ownership=True,
        admit_run=True,
    )
    if ctx.cached_response is not None:
        if ctx.run_slot is not None:
            ctx.run_slot.release()
        return stream_final_response(ctx.cached_response)

    def run_with_tracer(tracer: UserProgressTracePrinter) -> JarvisState:
        return run_jarvis(
            user_prompt=request.message,
            user_id=request.user_id,
            request_source=ctx.request_source,
            allow_mutations=allow_mutations(request.allow_mutations),
            tracer=tracer,
            thread_id=request.thread_id,
            identity=ctx.identity,
            request_id=request.request_id,
        )

    return stream_agent_run(
        run_with_tracer,
        request_claim=ctx.claim,
        run_slot=ctx.run_slot,
    )


@router.post("/invoke-bulk", response_model=BulkAgentResponse)
def invoke_bulk(
    request: BulkInvokeRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> BulkAgentResponse:
    require_api_key(x_jarvis_agent_key)
    identity = request.resolved_telegram_identity()
    messages = [message.strip() for message in request.messages if message.strip()]
    if not messages:
        raise HTTPException(status_code=422, detail="At least one non-empty message is required.")

    run_slot = try_acquire_run_slot()
    if run_slot is None:
        raise capacity_exceeded()

    try:
        results = []
        for index, message in enumerate(messages):
            try:
                rate_limit.consume_new_thread_quota(identity)
                result = run_jarvis(
                    user_prompt=message,
                    user_id=request.user_id,
                    request_source=request_source(request.source, identity),
                    allow_mutations=allow_bulk_mutations(request.allow_mutations),
                    max_agent_turns=request.max_agent_turns or MAX_AGENT_TURNS,
                    tracer=NULL_TRACE,
                    identity=identity,
                    request_id=request.request_id,
                )
                results.append(to_response(result))
            except HTTPException as error:
                if error.status_code != 429:
                    raise
                detail = str(error.detail)
                failed_response = AgentResponse(
                    status="failed",
                    thread_id="",
                    response=detail,
                    error=f"HTTP 429: {detail}",
                )
                results.append(failed_response)
                remaining = len(messages) - index - 1
                results.extend(
                    AgentResponse(
                        status="failed",
                        thread_id="",
                        response=detail,
                        error=f"HTTP 429: {detail}",
                    )
                    for _ in range(remaining)
                )
                break
            except Exception as error:
                results.append(
                    AgentResponse(
                        status="failed",
                        thread_id="",
                        response="Jarvis is temporarily unavailable. Please try again in a moment.",
                        error=str(error),
                    )
                )

        return BulkAgentResponse(results=results)
    finally:
        run_slot.release()
