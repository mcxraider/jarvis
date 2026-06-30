"""Pydantic schemas for the Jarvis FastAPI contract."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class InvokeRequest(BaseModel):
    message: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    source: Optional[str] = None
    telegram_user_id: Optional[int] = None
    telegram_username: Optional[str] = None
    telegram_first_name: Optional[str] = None
    request_id: Optional[str] = None
    thread_id: Optional[str] = None
    allow_mutations: Optional[bool] = None


class ResumeRequest(BaseModel):
    thread_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    source: Optional[str] = None
    telegram_user_id: Optional[int] = None
    telegram_username: Optional[str] = None
    telegram_first_name: Optional[str] = None
    request_id: Optional[str] = None
    allow_mutations: Optional[bool] = None


class BulkInvokeRequest(BaseModel):
    messages: List[str] = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    source: Optional[str] = None
    telegram_user_id: Optional[int] = None
    telegram_username: Optional[str] = None
    telegram_first_name: Optional[str] = None
    request_id: Optional[str] = None
    allow_mutations: Optional[bool] = None
    max_agent_turns: Optional[int] = None


class AgentResponse(BaseModel):
    status: Literal["completed", "interrupted", "failed"]
    thread_id: str
    response: str
    interrupt: Optional[Dict[str, Any]] = None
    tool_results: List[Dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None


class BulkAgentResponse(BaseModel):
    results: List[AgentResponse]
