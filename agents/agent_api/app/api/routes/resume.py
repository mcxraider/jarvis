"""Route for resuming interrupted Jarvis runs."""

from typing import Any, Optional

from fastapi import APIRouter, Header, Request as FastAPIRequest
from fastapi.responses import StreamingResponse

from agents.agent_api.app.api.routes.invoke import (
    _call_runner,
    allow_mutations,
    run_agent_request,
    runtime_checkpointer,
    stream_agent_run,
    stream_final_response,
)
from agents.agent_api.app.api.schemas import AgentResponse, ResumeRequest
from agents.agent_api.app.middleware.request_gate import apply_request_gate_async
from agents.agent_api.app.service import (
    NULL_TRACE,
    run_jarvis_async as run_jarvis,
)
from agents.agent_api.app.tracing import UserProgressTracePrinter

router = APIRouter()


@router.post("/resume", response_model=AgentResponse)
async def resume(
    request: ResumeRequest,
    http_request: FastAPIRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> AgentResponse:
    ctx = await apply_request_gate_async(
        "resume",
        request,
        x_jarvis_agent_key,
        charges_new_thread_quota=False,
        require_thread_ownership=True,
        admit_run=True,
    )
    if ctx.cached_response is not None:
        return ctx.cached_response

    async def run_with_tracer(_tracer: UserProgressTracePrinter) -> Any:
        return await _call_runner(
            run_jarvis,
            user_prompt=request.message,
            user_id=request.user_id,
            request_source=ctx.request_source,
            allow_mutations=allow_mutations(request.allow_mutations),
            tracer=NULL_TRACE,
            thread_id=request.thread_id,
            identity=ctx.identity,
            clarification_reply=request.message,
            request_id=request.request_id,
            checkpointer=runtime_checkpointer(http_request),
        )

    return await run_agent_request(
        run_with_tracer,
        ctx.claim,
        ctx.run_slot,
        failure_thread_id=request.thread_id,
    )


@router.post("/resume/stream")
async def resume_stream(
    request: ResumeRequest,
    http_request: FastAPIRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> StreamingResponse:
    ctx = await apply_request_gate_async(
        "resume",
        request,
        x_jarvis_agent_key,
        charges_new_thread_quota=False,
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
            clarification_reply=request.message,
            request_id=request.request_id,
            checkpointer=runtime_checkpointer(http_request),
        )

    return await stream_agent_run(
        run_with_tracer,
        request_claim=ctx.claim,
        run_slot=ctx.run_slot,
        failure_thread_id=request.thread_id,
    )
