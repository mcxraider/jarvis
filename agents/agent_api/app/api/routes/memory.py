"""Authenticated durable thread-memory operations."""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from agents.agent_api.app.api.schemas import MemoryResetRequest, MemoryResetResponse
from agents.agent_api.app.errors import require_api_key
from agents.agent_api.app.thread_memory import reset_thread_memory_async


router = APIRouter()


@router.post("/memory/reset", response_model=MemoryResetResponse)
async def reset_memory(
    request: MemoryResetRequest,
    x_jarvis_agent_key: Optional[str] = Header(default=None),
) -> MemoryResetResponse:
    require_api_key(x_jarvis_agent_key)
    identity = request.resolved_telegram_identity()
    assert identity is not None
    try:
        lineage_id = await reset_thread_memory_async(
            identity=identity,
            conversation_key=request.conversation_key,
        )
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="Thread memory could not be reset. Please retry.",
        ) from error
    return MemoryResetResponse(lineage_id=lineage_id)


__all__ = ["reset_memory", "router"]
