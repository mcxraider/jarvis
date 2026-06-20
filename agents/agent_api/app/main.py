"""FastAPI entrypoint for the Jarvis LangGraph agent service."""

from fastapi import FastAPI

from agents.agent_api.app.api.routes.health import router as health_router
from agents.agent_api.app.api.routes.invoke import router as invoke_router
from agents.agent_api.app.api.routes.resume import router as resume_router
from agents.agent_api.app.config import settings


def create_app() -> FastAPI:
    app = FastAPI(title=settings.api_title)
    app.include_router(health_router)
    app.include_router(invoke_router)
    app.include_router(resume_router)
    return app


app = create_app()

