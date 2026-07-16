"""Invocation routes for starting Jarvis runs."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import weakref
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request as FastAPIRequest
from fastapi.responses import StreamingResponse

from agents.agent_api.app.async_offload import bounded_to_thread
from agents.agent_api.app.api.admission import (
    RunSlot,
    capacity_exceeded,
    try_acquire_run_slot_async,
)
from agents.agent_api.app.api.request_idempotency import RequestClaim
from agents.agent_api.app.api.schemas import (
    AgentResponse,
    BulkAgentResponse,
    BulkInvokeRequest,
    InvokeRequest,
)
from agents.agent_api.app.checkpointing import DEFAULT_CHECKPOINTER, get_async_checkpointer
from agents.agent_api.app.config import settings
from agents.agent_api.app.errors import require_api_key
from agents.agent_api.app.middleware import idempotency
import agents.agent_api.app.middleware.rate_limit as rate_limit
from agents.agent_api.app.middleware.request_gate import (
    abandon_claim_async,
    apply_request_gate_async,
)
from agents.agent_api.app.service import (
    ALLOW_MUTATIONS,
    MAX_AGENT_TURNS,
    NULL_TRACE,
    JarvisState,
    run_jarvis_async as run_jarvis,
)
from agents.agent_api.app.tracing import UserProgressTracePrinter

router = APIRouter()

STREAM_WORKER_DRAIN_TIMEOUT_SECONDS = 5.0
STREAM_QUEUE_MAX = 256

_active_stream_producers: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, set[asyncio.Task[Any]]
] = weakref.WeakKeyDictionary()
_active_stream_producers_lock = threading.Lock()


def _track_producer(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    """Track accepted graph work until it actually settles."""

    loop = task.get_loop()
    with _active_stream_producers_lock:
        _active_stream_producers.setdefault(loop, set()).add(task)

    def remove(completed: asyncio.Task[Any]) -> None:
        with _active_stream_producers_lock:
            tasks = _active_stream_producers.get(loop)
            if tasks is not None:
                tasks.discard(completed)
                if not tasks:
                    _active_stream_producers.pop(loop, None)
        if not completed.cancelled():
            # Retrieve an exception when a disconnected client no longer awaits
            # the producer. Other awaiters can still retrieve the same result.
            completed.exception()

    task.add_done_callback(remove)
    return task


async def drain_stream_workers(
    timeout: float = STREAM_WORKER_DRAIN_TIMEOUT_SECONDS,
) -> bool:
    """Wait for accepted graph producers owned by this event loop to settle."""

    loop = asyncio.get_running_loop()
    current_task = asyncio.current_task()
    deadline = loop.time() + max(0.0, timeout)
    while True:
        with _active_stream_producers_lock:
            tasks = {
                task
                for task in _active_stream_producers.get(loop, ())
                if task is not current_task and not task.done()
            }
        if not tasks:
            # Let completion callbacks and already-admitted routes publish any
            # successor work before declaring the registry stably empty.
            await asyncio.sleep(0)
            with _active_stream_producers_lock:
                if not any(
                    task is not current_task and not task.done()
                    for task in _active_stream_producers.get(loop, ())
                ):
                    return True
            continue
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        _done, pending = await asyncio.wait(tasks, timeout=remaining)
        if pending:
            return False


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
        user_response = str(
            result.get("final_response")
            or "Jarvis could not complete that request."
        )
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


def runtime_checkpointer(http_request: Optional[FastAPIRequest] = None) -> Any:
    """Use the request app's saver, with a memory-only direct-test fallback."""

    if http_request is not None:
        checkpointer = getattr(http_request.app.state, "async_checkpointer", None)
        if checkpointer is not None:
            return checkpointer

    try:
        return get_async_checkpointer()
    except RuntimeError:
        if settings.checkpoint_backend == "memory":
            return DEFAULT_CHECKPOINTER
        raise


async def _call_maybe_async(
    run_callable: Callable[[UserProgressTracePrinter], Any],
    tracer: UserProgressTracePrinter,
) -> JarvisState:
    if inspect.iscoroutinefunction(run_callable):
        return await run_callable(tracer)
    result = await bounded_to_thread(run_callable, tracer)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _call_runner(runner: Callable[..., Any], **kwargs: Any) -> JarvisState:
    """Call the production async runner or a patched legacy sync fake safely."""

    if inspect.iscoroutinefunction(runner):
        return await runner(**kwargs)
    result = await bounded_to_thread(runner, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def _failed_response(error: BaseException, thread_id: str = "") -> AgentResponse:
    return AgentResponse(
        status="failed",
        thread_id=thread_id,
        response="Jarvis is temporarily unavailable. Please try again in a moment.",
        error=str(error),
    )


async def _settle_agent_run(
    run_callable: Callable[[UserProgressTracePrinter], Any],
    tracer: UserProgressTracePrinter,
    request_claim: Optional[RequestClaim],
    run_slot: Optional[RunSlot],
    failure_thread_id: str = "",
) -> AgentResponse:
    """Run and finalize one accepted request before returning its capacity."""

    try:
        result = await _call_maybe_async(run_callable, tracer)
        response = to_response(result)
        if request_claim is not None:
            await bounded_to_thread(
                finish_idempotent_request,
                request_claim,
                response,
            )
        return response
    except asyncio.CancelledError:
        if request_claim is not None:
            await abandon_claim_async(request_claim)
        raise
    except Exception as error:
        if request_claim is not None:
            await abandon_claim_async(request_claim)
        return _failed_response(error, failure_thread_id)
    finally:
        if run_slot is not None:
            run_slot.release()


async def _start_agent_run(
    run_callable: Callable[[UserProgressTracePrinter], Any],
    tracer: UserProgressTracePrinter,
    request_claim: Optional[RequestClaim],
    run_slot: Optional[RunSlot],
    failure_thread_id: str = "",
) -> asyncio.Task[AgentResponse]:
    """Create a tracked producer, cleaning route-owned resources on failure."""

    producer = _settle_agent_run(
        run_callable,
        tracer,
        request_claim,
        run_slot,
        failure_thread_id,
    )
    try:
        task = asyncio.create_task(producer)
    except BaseException:
        producer.close()
        try:
            if request_claim is not None:
                await abandon_claim_async(request_claim)
        finally:
            if run_slot is not None:
                run_slot.release()
        raise
    try:
        return _track_producer(task)
    except BaseException:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        try:
            if request_claim is not None:
                await abandon_claim_async(request_claim)
        finally:
            if run_slot is not None:
                run_slot.release()
        raise


async def _await_accepted_task(task: asyncio.Task[Any]) -> Any:
    """Do not let request cancellation duplicate accepted mutation work."""

    cancellation_requested = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancellation_requested = True
            continue
        if cancellation_requested:
            raise asyncio.CancelledError
        return result


async def run_agent_request(
    run_callable: Callable[[UserProgressTracePrinter], Any],
    request_claim: Optional[RequestClaim],
    run_slot: Optional[RunSlot],
    failure_thread_id: str = "",
) -> AgentResponse:
    """Run a non-streaming request through the tracked producer lifecycle."""

    task = await _start_agent_run(
        run_callable,
        UserProgressTracePrinter(lambda _progress: None, enabled=False),
        request_claim,
        run_slot,
        failure_thread_id,
    )
    return await _await_accepted_task(task)


def stream_final_response(response: AgentResponse) -> StreamingResponse:
    async def iterator():
        event = {"type": "final", "response": response_payload(response)}
        yield json.dumps(event, default=str) + "\n"

    return StreamingResponse(iterator(), media_type="application/x-ndjson")


async def stream_agent_run(
    run_callable: Callable[[UserProgressTracePrinter], Any],
    request_claim: Optional[RequestClaim] = None,
    run_slot: Optional[RunSlot] = None,
    failure_thread_id: str = "",
) -> StreamingResponse:
    """Start one native async producer and stream bounded NDJSON progress."""

    loop = asyncio.get_running_loop()
    loop_thread_id = threading.get_ident()
    try:
        events: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(
            maxsize=STREAM_QUEUE_MAX
        )
        # Progress callbacks may originate from bounded compatibility threads.
        consumer_closed = threading.Event()
        pending_callbacks = threading.BoundedSemaphore(STREAM_QUEUE_MAX)
    except BaseException:
        try:
            if request_claim is not None:
                await abandon_claim_async(request_claim)
        finally:
            if run_slot is not None:
                run_slot.release()
        raise
    sequence = 0

    def enqueue_progress(progress: Dict[str, Any]) -> None:
        nonlocal sequence
        try:
            if consumer_closed.is_set() or events.full():
                return
            sequence += 1
            event: Dict[str, Any] = {
                "type": "progress",
                "sequence": sequence,
                # Preserve legacy fields for clients that have not adopted facts.
                "stage": progress.get("stage", "progress"),
                "message": progress.get("message", "Jarvis is working"),
            }
            if isinstance(progress.get("fact"), dict):
                event["fact"] = progress["fact"]
            events.put_nowait(event)
        finally:
            pending_callbacks.release()

    def emit_progress(progress: Dict[str, Any]) -> None:
        if consumer_closed.is_set() or not pending_callbacks.acquire(blocking=False):
            return
        progress_copy = dict(progress)
        if threading.get_ident() == loop_thread_id:
            enqueue_progress(progress_copy)
            return
        try:
            loop.call_soon_threadsafe(enqueue_progress, progress_copy)
        except RuntimeError:
            pending_callbacks.release()

    tracer = UserProgressTracePrinter(emit_progress, enabled=False)
    task = await _start_agent_run(
        run_callable,
        tracer,
        request_claim,
        run_slot,
        failure_thread_id,
    )

    def publish_final(completed: asyncio.Task[AgentResponse]) -> None:
        if completed.cancelled() or consumer_closed.is_set():
            return
        try:
            response = completed.result()
        except BaseException as error:
            response = _failed_response(error)
        # Final delivery has priority over progress under backpressure. Keeping
        # this loop-owned and synchronous avoids an orphan terminal enqueue task.
        while events.full():
            try:
                events.get_nowait()
            except asyncio.QueueEmpty:
                break
        events.put_nowait({"type": "final", "response": response_payload(response)})

    task.add_done_callback(publish_final)

    async def iterator():
        try:
            while True:
                event = await events.get()
                yield json.dumps(event, default=str) + "\n"
                if event.get("type") == "final":
                    break
        finally:
            # Disconnect never cancels accepted graph/mutation work. The producer
            # remains tracked and owns its claim/slot until durable finalization.
            consumer_closed.set()

    return StreamingResponse(iterator(), media_type="application/x-ndjson")


@router.post("/invoke", response_model=AgentResponse)
async def invoke(
    request: InvokeRequest,
    http_request: FastAPIRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> AgentResponse:
    ctx = await apply_request_gate_async(
        "invoke",
        request,
        x_jarvis_agent_key,
        charges_new_thread_quota=True,
        require_thread_ownership=True,
        admit_run=True,
    )
    if ctx.cached_response is not None:
        return ctx.cached_response

    async def run_with_tracer(tracer: UserProgressTracePrinter) -> Any:
        return await _call_runner(
            run_jarvis,
            user_prompt=request.message,
            user_id=request.user_id,
            request_source=ctx.request_source,
            allow_mutations=allow_mutations(request.allow_mutations),
            tracer=tracer,
            thread_id=request.thread_id,
            identity=ctx.identity,
            request_id=request.request_id,
            checkpointer=runtime_checkpointer(http_request),
        )

    return await run_agent_request(
        run_with_tracer,
        ctx.claim,
        ctx.run_slot,
        failure_thread_id=request.thread_id or "",
    )


@router.post("/invoke/stream")
async def invoke_stream(
    request: InvokeRequest,
    http_request: FastAPIRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> StreamingResponse:
    ctx = await apply_request_gate_async(
        "invoke",
        request,
        x_jarvis_agent_key,
        charges_new_thread_quota=True,
        require_thread_ownership=True,
        admit_run=True,
    )
    if ctx.cached_response is not None:
        return stream_final_response(ctx.cached_response)

    async def run_with_tracer(tracer: UserProgressTracePrinter) -> Any:
        return await _call_runner(
            run_jarvis,
            user_prompt=request.message,
            user_id=request.user_id,
            request_source=ctx.request_source,
            allow_mutations=allow_mutations(request.allow_mutations),
            tracer=tracer,
            thread_id=request.thread_id,
            identity=ctx.identity,
            request_id=request.request_id,
            checkpointer=runtime_checkpointer(http_request),
        )

    return await stream_agent_run(
        run_with_tracer,
        request_claim=ctx.claim,
        run_slot=ctx.run_slot,
        failure_thread_id=request.thread_id or "",
    )


@router.post("/invoke-bulk", response_model=BulkAgentResponse)
async def invoke_bulk(
    request: BulkInvokeRequest,
    http_request: FastAPIRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> BulkAgentResponse:
    require_api_key(x_jarvis_agent_key)
    identity = request.resolved_telegram_identity()
    messages = [message.strip() for message in request.messages if message.strip()]
    if not messages:
        raise HTTPException(
            status_code=422,
            detail="At least one non-empty message is required.",
        )

    run_slot = await try_acquire_run_slot_async()
    if run_slot is None:
        raise capacity_exceeded()

    async def run_batch() -> BulkAgentResponse:
        results = []
        try:
            for index, message in enumerate(messages):
                try:
                    await bounded_to_thread(
                        rate_limit.consume_new_thread_quota,
                        identity,
                    )
                    result = await _call_runner(
                        run_jarvis,
                        user_prompt=message,
                        user_id=request.user_id,
                        request_source=request_source(request.source, identity),
                        allow_mutations=allow_bulk_mutations(request.allow_mutations),
                        max_agent_turns=request.max_agent_turns or MAX_AGENT_TURNS,
                        tracer=NULL_TRACE,
                        identity=identity,
                        request_id=request.request_id,
                        checkpointer=runtime_checkpointer(http_request),
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
                    results.append(_failed_response(error))

            return BulkAgentResponse(results=results)
        finally:
            run_slot.release()

    producer = run_batch()
    try:
        task = asyncio.create_task(producer)
    except BaseException:
        producer.close()
        run_slot.release()
        raise
    try:
        _track_producer(task)
    except BaseException:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        finally:
            run_slot.release()
        raise
    return await _await_accepted_task(task)
