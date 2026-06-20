"""Invocation routes for starting Jarvis runs."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException

from agents.agent_api.app.api.schemas import AgentResponse, BulkAgentResponse, BulkInvokeRequest, InvokeRequest
from agents.agent_api.app.errors import require_api_key
from agents.agent_api.app.service import ALLOW_MUTATIONS, MAX_AGENT_TURNS, NULL_TRACE, JarvisState, run_jarvis

router = APIRouter()


def allow_mutations(request_value: Optional[bool]) -> bool:
    if request_value is not None:
        return request_value
    return ALLOW_MUTATIONS


def allow_bulk_mutations(request_value: Optional[bool]) -> bool:
    if request_value is not None:
        return request_value
    return ALLOW_MUTATIONS


def request_source(source: Optional[str], telegram_user_id: Optional[int]) -> str:
    if source:
        return source
    return "telegram" if telegram_user_id is not None else "api"


def to_response(result: JarvisState) -> AgentResponse:
    thread_id = str(result.get("thread_id") or "")
    tool_results = result.get("tool_results", [])

    if result.get("interrupted"):
        interrupt = result.get("interrupt_payload", {})
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
        return AgentResponse(
            status="failed",
            thread_id=thread_id,
            response="Jarvis could not complete that request.",
            tool_results=tool_results,
            error=error,
        )

    return AgentResponse(
        status="completed",
        thread_id=thread_id,
        response=str(result.get("final_response") or ""),
        tool_results=tool_results,
    )


@router.post("/invoke", response_model=AgentResponse)
def invoke(
    request: InvokeRequest,
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
        )
        return to_response(result)
    except Exception as error:
        return AgentResponse(
            status="failed",
            thread_id=request.thread_id or "",
            response="Jarvis is temporarily unavailable. Please try again in a moment.",
            error=str(error),
        )


@router.post("/invoke-bulk", response_model=BulkAgentResponse)
def invoke_bulk(
    request: BulkInvokeRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> BulkAgentResponse:
    require_api_key(x_jarvis_agent_key)
    messages = [message.strip() for message in request.messages if message.strip()]
    if not messages:
        raise HTTPException(status_code=422, detail="At least one non-empty message is required.")

    results = []
    for message in messages:
        try:
            result = run_jarvis(
                user_prompt=message,
                user_id=request.user_id,
                request_source=request_source(request.source, request.telegram_user_id),
                allow_mutations=allow_bulk_mutations(request.allow_mutations),
                max_agent_turns=request.max_agent_turns or MAX_AGENT_TURNS,
                tracer=NULL_TRACE,
            )
            results.append(to_response(result))
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
