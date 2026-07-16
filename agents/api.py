"""Compatibility shim for the Jarvis FastAPI app.

Prefer `agents.agent_api.app.main:app` for new deployments.
"""

from agents.agent_api.app.api.routes.invoke import to_response
from agents.agent_api.app.main import app, create_app
from agents.agent_api.app.service import (
    ALLOW_MUTATIONS,
    MAX_AGENT_TURNS,
    NULL_TRACE,
    JarvisState,
    run_jarvis,
    run_jarvis_async,
)

__all__ = [
    "ALLOW_MUTATIONS",
    "MAX_AGENT_TURNS",
    "NULL_TRACE",
    "JarvisState",
    "app",
    "create_app",
    "run_jarvis",
    "run_jarvis_async",
    "to_response",
]
