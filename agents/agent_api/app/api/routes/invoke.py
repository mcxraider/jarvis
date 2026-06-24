"""Invocation routes for starting Jarvis runs."""

import json
import queue
import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from agents.agent_api.app.api.schemas import AgentResponse, BulkAgentResponse, BulkInvokeRequest, InvokeRequest
from agents.agent_api.app.errors import require_api_key
from agents.agent_api.app.service import ALLOW_MUTATIONS, MAX_AGENT_TURNS, NULL_TRACE, JarvisState, run_jarvis
from agents.agent_api.app.tracing import UserProgressTracePrinter

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
        )

    return AgentResponse(
        status="completed",
        thread_id=thread_id,
        response=str(result.get("final_response") or ""),
        tool_results=tool_results,
    )


def response_payload(response: AgentResponse) -> Dict[str, Any]:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return response.dict()


def stream_agent_run(run_callable: Any):
    events: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
    sequence = 0

    def emit_progress(progress: Dict[str, Any]) -> None:
        nonlocal sequence
        sequence += 1
        events.put(
            {
                "type": "progress",
                "sequence": sequence,
                "stage": progress.get("stage", "progress"),
                "message": progress.get("message", "Jarvis is working"),
            }
        )

    def worker() -> None:
        try:
            result = run_callable(UserProgressTracePrinter(emit_progress, enabled=False))
            events.put({"type": "final", "response": response_payload(to_response(result))})
        except Exception as error:
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
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def iterator():
        while True:
            event = events.get()
            if event is None:
                break
            yield json.dumps(event, default=str) + "\n"

    return StreamingResponse(iterator(), media_type="application/x-ndjson")


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
            request_id=request.request_id,
        )
        return to_response(result)
    except Exception as error:
        return AgentResponse(
            status="failed",
            thread_id=request.thread_id or "",
            response="Jarvis is temporarily unavailable. Please try again in a moment.",
            error=str(error),
        )


@router.post("/invoke/stream")
def invoke_stream(
    request: InvokeRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> StreamingResponse:
    require_api_key(x_jarvis_agent_key)

    def run_with_tracer(tracer: UserProgressTracePrinter) -> JarvisState:
        return run_jarvis(
            user_prompt=request.message,
            user_id=request.user_id,
            request_source=request_source(request.source, request.telegram_user_id),
            allow_mutations=allow_mutations(request.allow_mutations),
            tracer=tracer,
            thread_id=request.thread_id,
            request_id=request.request_id,
        )

    return stream_agent_run(run_with_tracer)


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
                request_id=request.request_id,
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
