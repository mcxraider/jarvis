"""FastAPI entrypoint for the Jarvis LangGraph agent service."""

import asyncio
import logging
from contextlib import asynccontextmanager

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
    from agents.agent_api.app.db import verify_database_runtime
    from agents.agent_api.app.run_logging import cleanup_old_logs, LOG_DIR

    await asyncio.to_thread(verify_database_runtime)
    await asyncio.to_thread(cleanup_old_logs, LOG_DIR)
    cleanup_task = asyncio.create_task(
        run_idempotency_cleanup_loop(
            DEFAULT_IDEMPOTENCY_STORE,
            settings.idempotency_cleanup_interval_seconds,
        )
    )
    try:
        yield
    finally:
        cleanup_errors: list[BaseException] = []
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        except BaseException as error:
            cleanup_errors.append(error)
        from agents.agent_api.app.db import close_pool
        from agents.agent_api.app.api.routes.invoke import (
            STREAM_WORKER_DRAIN_TIMEOUT_SECONDS,
            drain_stream_workers,
        )
        from agents.agent_api.app.graph.nodes.orchestrator import (
            close_shared_agent_client,
            close_shared_async_agent_client,
        )
        from agents.agent_api.app.graph.nodes.summarize import (
            close_shared_async_summarizer_client,
            close_shared_summarizer_client,
        )
        from agents.agent_api.app.router.client import (
            close_shared_async_router_openai_client,
            close_shared_router_client,
        )
        from agents.agent_api.app.run_logging import shutdown_run_logs
        from agents.agent_api.app.tools.todoist.client import (
            close_todoist_async_http_client,
            close_todoist_http_client,
        )

        # Workers can outlive disconnected streaming responses. Drain them before
        # closing shared resources, then attempt every cleanup without allowing a
        # later failure to hide an earlier one.
        try:
            workers_drained = await asyncio.to_thread(
                drain_stream_workers,
                timeout=STREAM_WORKER_DRAIN_TIMEOUT_SECONDS,
            )
            if not workers_drained:
                cleanup_errors.append(
                    TimeoutError(
                        "Active streaming workers did not drain before shutdown."
                    )
                )
        except BaseException as error:
            cleanup_errors.append(error)
        for close_resource in (
            close_shared_agent_client,
            close_shared_router_client,
            close_shared_summarizer_client,
        ):
            try:
                await asyncio.to_thread(close_resource)
            except BaseException as error:
                cleanup_errors.append(error)
        for close_resource in (
            close_shared_async_agent_client,
            close_shared_async_router_openai_client,
            close_shared_async_summarizer_client,
        ):
            try:
                await close_resource()
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            await asyncio.to_thread(shutdown_run_logs, timeout=5.0)
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            await asyncio.to_thread(close_todoist_http_client)
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            await close_todoist_async_http_client()
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            close_pool()
        except BaseException as error:
            cleanup_errors.append(error)

        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        if cleanup_errors:
            raise BaseExceptionGroup(
                "Jarvis shutdown encountered multiple cleanup failures.",
                cleanup_errors,
            )


def create_app() -> FastAPI:
    app = FastAPI(title=settings.api_title, lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(invoke_router)
    app.include_router(resume_router)
    return app


app = create_app()
