"""Route for resuming interrupted Jarvis runs."""

from typing import Optional

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from agents.agent_api.app.api import request_idempotency
from agents.agent_api.app.api.routes.invoke import (
    allow_mutations,
    begin_idempotent_request,
    finish_idempotent_request,
    request_source,
    stream_agent_run,
    stream_final_response,
    to_response,
)
from agents.agent_api.app.api.schemas import AgentResponse, ResumeRequest
from agents.agent_api.app.errors import require_api_key
from agents.agent_api.app.service import NULL_TRACE, run_jarvis
from agents.agent_api.app.tracing import UserProgressTracePrinter

router = APIRouter()


@router.post("/resume", response_model=AgentResponse)
def resume(
    request: ResumeRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> AgentResponse:
    require_api_key(x_jarvis_agent_key)
    request_claim, cached_response = begin_idempotent_request("resume", request)
    if cached_response is not None:
        return cached_response
    try:
        result = run_jarvis(
            user_prompt=request.message,
            user_id=request.user_id,
            request_source=request_source(request.source, request.telegram_user_id),
            allow_mutations=allow_mutations(request.allow_mutations),
            tracer=NULL_TRACE,
            thread_id=request.thread_id,
            telegram_user_id=request.telegram_user_id,
            telegram_username=request.telegram_username,
            telegram_first_name=request.telegram_first_name,
            clarification_reply=request.message,
            request_id=request.request_id,
        )
        response = to_response(result)
        finish_idempotent_request(request_claim, response)
        return response
    except Exception as error:
        request_idempotency.DEFAULT_REQUEST_IDEMPOTENCY_COORDINATOR.abandon(
            request_claim
        )
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
    require_api_key(x_jarvis_agent_key)
    request_claim, cached_response = begin_idempotent_request("resume", request)
    if cached_response is not None:
        return stream_final_response(cached_response)

    def run_with_tracer(tracer: UserProgressTracePrinter):
        return run_jarvis(
            user_prompt=request.message,
            user_id=request.user_id,
            request_source=request_source(request.source, request.telegram_user_id),
            allow_mutations=allow_mutations(request.allow_mutations),
            tracer=tracer,
            thread_id=request.thread_id,
            telegram_user_id=request.telegram_user_id,
            telegram_username=request.telegram_username,
            telegram_first_name=request.telegram_first_name,
            clarification_reply=request.message,
            request_id=request.request_id,
        )

    return stream_agent_run(run_with_tracer, request_claim=request_claim)
