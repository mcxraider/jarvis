"""Route for resuming interrupted Jarvis runs."""

from typing import Optional

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from agents.agent_api.app.api.routes.invoke import (
    allow_mutations,
    finish_idempotent_request,
    stream_agent_run,
    stream_final_response,
    to_response,
)
from agents.agent_api.app.api.schemas import AgentResponse, ResumeRequest
from agents.agent_api.app.middleware import idempotency
from agents.agent_api.app.middleware.request_gate import apply_request_gate
from agents.agent_api.app.service import NULL_TRACE, run_jarvis
from agents.agent_api.app.tracing import UserProgressTracePrinter

router = APIRouter()


@router.post("/resume", response_model=AgentResponse)
def resume(
    request: ResumeRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> AgentResponse:
    ctx = apply_request_gate(
        "resume",
        request,
        x_jarvis_agent_key,
        charges_new_thread_quota=False,
        require_thread_ownership=True,
    )
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
            clarification_reply=request.message,
            request_id=request.request_id,
        )
        response = to_response(result)
        finish_idempotent_request(ctx.claim, response)
        return response
    except Exception as error:
        idempotency.abandon_idempotent_request(ctx.claim)
        return AgentResponse(
            status="failed",
            thread_id=request.thread_id,
            response="Jarvis is temporarily unavailable. Please try again in a moment.",
            error=str(error),
        )


@router.post("/resume/stream")
def resume_stream(
    request: ResumeRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> StreamingResponse:
    ctx = apply_request_gate(
        "resume",
        request,
        x_jarvis_agent_key,
        charges_new_thread_quota=False,
        require_thread_ownership=True,
    )
    if ctx.cached_response is not None:
        return stream_final_response(ctx.cached_response)

    def run_with_tracer(tracer: UserProgressTracePrinter):
        return run_jarvis(
            user_prompt=request.message,
            user_id=request.user_id,
            request_source=ctx.request_source,
            allow_mutations=allow_mutations(request.allow_mutations),
            tracer=tracer,
            thread_id=request.thread_id,
            identity=ctx.identity,
            clarification_reply=request.message,
            request_id=request.request_id,
        )

    return stream_agent_run(run_with_tracer, request_claim=ctx.claim)
