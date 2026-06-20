"""Shared API error helpers."""

from typing import Optional

from fastapi import HTTPException

from agents.agent_api.app.config import settings


class JarvisApiError(Exception):
    """Base exception for expected Jarvis API failures."""


def require_api_key(x_jarvis_agent_key: Optional[str]) -> None:
    if settings.api_key and x_jarvis_agent_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

