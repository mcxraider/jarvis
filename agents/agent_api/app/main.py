"""FastAPI entrypoint for the Jarvis LangGraph agent service."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from agents.agent_api.app.api.routes.health import router as health_router
from agents.agent_api.app.api.routes.invoke import router as invoke_router
from agents.agent_api.app.api.routes.resume import router as resume_router
from agents.agent_api.app.config import settings
from agents.agent_api.app.idempotency import DEFAULT_IDEMPOTENCY_STORE, IdempotencyStore

logger = logging.getLogger(__name__)


async def run_idempotency_cleanup_loop(
    store: IdempotencyStore,
    interval_seconds: int,
) -> None:
    """Remove expired claims without delaying API startup."""

    while True:
        try:
            deleted = await asyncio.to_thread(store.cleanup_expired)
            logger.info(
                "Idempotency cleanup completed.",
                extra={
                    "idempotency_action": "cleanup",
                    "idempotency_layer": "all",
                    "deleted_count": deleted,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Idempotency cleanup failed open.",
                extra={"error_type": type(error).__name__},
            )
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cleanup_task = asyncio.create_task(
        run_idempotency_cleanup_loop(
            DEFAULT_IDEMPOTENCY_STORE,
            settings.idempotency_cleanup_interval_seconds,
        )
    )
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        from agents.agent_api.app.db import close_pool

        close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.api_title, lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(invoke_router)
    app.include_router(resume_router)
    return app


app = create_app()
