"""Route for resuming interrupted Jarvis runs."""

from typing import Optional

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from agents.agent_api.app.api.routes.invoke import allow_mutations, request_source, stream_agent_run, to_response
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
    try:
        result = run_jarvis(
            user_prompt=request.message,
            user_id=request.user_id,
            request_source=request_source(request.source, request.telegram_user_id),
            allow_mutations=allow_mutations(request.allow_mutations),
            tracer=NULL_TRACE,
            thread_id=request.thread_id,
            telegram_user_id=request.telegram_user_id,
            clarification_reply=request.message,
            request_id=request.request_id,
        )
        return to_response(result)
    except Exception as error:
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

    def run_with_tracer(tracer: UserProgressTracePrinter):
        return run_jarvis(
            user_prompt=request.message,
            user_id=request.user_id,
            request_source=request_source(request.source, request.telegram_user_id),
            allow_mutations=allow_mutations(request.allow_mutations),
            tracer=tracer,
            thread_id=request.thread_id,
            telegram_user_id=request.telegram_user_id,
            clarification_reply=request.message,
            request_id=request.request_id,
        )

    return stream_agent_run(run_with_tracer)
