"""FastAPI entrypoint for the Jarvis LangGraph agent service."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agents.agent_api.app.api.routes.cancel import router as cancel_router
from agents.agent_api.app.api.routes.health import router as health_router
from agents.agent_api.app.api.routes.invoke import router as invoke_router
from agents.agent_api.app.api.routes.resume import router as resume_router
from agents.agent_api.app.config import settings
from agents.agent_api.app.idempotency import DEFAULT_IDEMPOTENCY_STORE, IdempotencyStore

logger = logging.getLogger(__name__)


def _raise_lifespan_errors(
    primary_error: BaseException | None,
    cleanup_errors: list[BaseException],
) -> None:
    if primary_error is not None and cleanup_errors:
        raise BaseExceptionGroup(
            "Jarvis lifespan and cleanup encountered multiple failures.",
            [primary_error, *cleanup_errors],
        )
    if primary_error is not None:
        raise primary_error.with_traceback(primary_error.__traceback__)
    if len(cleanup_errors) == 1:
        raise cleanup_errors[0]
    if cleanup_errors:
        raise BaseExceptionGroup(
            "Jarvis shutdown encountered multiple cleanup failures.",
            cleanup_errors,
        )


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
    from agents.agent_api.app.async_offload import drain_offloads, reset_offload_limiters
    from agents.agent_api.app.api.active_runs import reset_active_run_registry
    from agents.agent_api.app.checkpointing import (
        initialize_async_checkpointer,
        reset_async_checkpointer,
    )
    from agents.agent_api.app.db import (
        close_async_pool,
        open_async_pool,
        verify_database_runtime,
    )
    from agents.agent_api.app.graph.builder import (
        get_or_compile_graph,
        reset_compiled_graphs,
    )
    from agents.agent_api.app.run_logging import cleanup_old_logs, LOG_DIR
    from agents.agent_api.app.post_run import shutdown_post_run_jobs

    cleanup_task: asyncio.Task[None] | None = None
    primary_error: BaseException | None = None
    async_pool_started = False
    async_checkpointer = None
    try:
        # Build loop-owned resources before accepting requests. Memory mode reuses
        # the existing saver; Postgres mode binds AsyncPostgresSaver to this loop.
        async_pool = await open_async_pool()
        async_pool_started = True
        # Validate the least-privilege runtime before optional checkpoint setup can
        # issue schema DDL through the same configured database.
        await asyncio.to_thread(verify_database_runtime)
        async_checkpointer = await initialize_async_checkpointer(async_pool)
        _app.state.async_checkpointer = async_checkpointer
        get_or_compile_graph(async_checkpointer)
        await asyncio.to_thread(cleanup_old_logs, LOG_DIR)
        cleanup_task = asyncio.create_task(
            run_idempotency_cleanup_loop(
                DEFAULT_IDEMPOTENCY_STORE,
                settings.idempotency_cleanup_interval_seconds,
            )
        )
        yield
    except BaseException as error:
        primary_error = error
    finally:
        cleanup_errors: list[BaseException] = []
        if cleanup_task is not None:
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
            reset_summarizer_limiters,
        )
        from agents.agent_api.app.router.client import (
            close_shared_async_router_openai_client,
            close_shared_router_client,
        )
        from agents.agent_api.app.router.cache import reset_router_cache
        from agents.agent_api.app.run_logging import shutdown_run_logs
        from agents.agent_api.app.tools.todoist.client import (
            close_todoist_async_http_client,
            close_todoist_http_client,
        )
        from agents.agent_api.app.tools.google_calendar.client import (
            close_calendar_async_http_client,
        )

        # Producers can outlive disconnected streaming responses. Drain them before
        # closing shared resources, then attempt every cleanup without allowing a
        # later failure to hide an earlier one.
        workers_drained = True
        try:
            workers_drained = await drain_stream_workers(
                timeout=STREAM_WORKER_DRAIN_TIMEOUT_SECONDS,
            )
            if not workers_drained:
                cleanup_errors.append(
                    TimeoutError(
                        "Active streaming workers did not drain before shutdown."
                    )
                )
        except BaseException as error:
            workers_drained = False
            cleanup_errors.append(error)
        post_run_drained = True
        if workers_drained:
            try:
                post_run_drained = await shutdown_post_run_jobs(
                    STREAM_WORKER_DRAIN_TIMEOUT_SECONDS
                )
                if not post_run_drained:
                    cleanup_errors.append(
                        TimeoutError(
                            "Post-run metadata did not drain before shutdown."
                        )
                    )
            except BaseException as error:
                post_run_drained = False
                cleanup_errors.append(error)
        else:
            # A producer may still enqueue post-run work, so the worker cannot be
            # stopped safely yet. The producer error below retains all resources.
            post_run_drained = False
        offloads_drained = True
        try:
            offloads_drained = await drain_offloads(
                STREAM_WORKER_DRAIN_TIMEOUT_SECONDS
            )
            if not offloads_drained:
                cleanup_errors.append(
                    TimeoutError(
                        "Blocking async compatibility work did not drain before shutdown."
                    )
                )
        except BaseException as error:
            offloads_drained = False
            cleanup_errors.append(error)
        if not workers_drained or not post_run_drained or not offloads_drained:
            # Neither native producers nor ``to_thread`` work can be force-closed
            # safely once a mutation may be in flight. Still close connection pools
            # so connections are not orphaned during worker recycling.
            if hasattr(DEFAULT_IDEMPOTENCY_STORE, "close"):
                try:
                    DEFAULT_IDEMPOTENCY_STORE.close()
                except BaseException as error:
                    cleanup_errors.append(error)
            try:
                close_pool()
            except BaseException as error:
                cleanup_errors.append(error)
            _raise_lifespan_errors(primary_error, cleanup_errors)
        try:
            reset_active_run_registry()
        except BaseException as error:
            cleanup_errors.append(error)
            _raise_lifespan_errors(primary_error, cleanup_errors)
        for close_resource in (
            close_shared_agent_client,
            close_shared_router_client,
            close_shared_summarizer_client,
        ):
            try:
                await asyncio.to_thread(close_resource)
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            reset_summarizer_limiters()
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
            await close_calendar_async_http_client()
        except BaseException as error:
            cleanup_errors.append(error)
        if hasattr(DEFAULT_IDEMPOTENCY_STORE, "close"):
            try:
                DEFAULT_IDEMPOTENCY_STORE.close()
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            close_pool()
        except BaseException as error:
            cleanup_errors.append(error)
        # Compiled graphs retain their checkpointer by identity, so forget them
        # before closing a lifespan-owned Postgres pool.
        try:
            reset_compiled_graphs()
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            reset_router_cache()
        except BaseException as error:
            cleanup_errors.append(error)
        if async_checkpointer is not None:
            try:
                reset_async_checkpointer(async_checkpointer)
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                _app.state.async_checkpointer = None
            except BaseException as error:
                cleanup_errors.append(error)
        # The async pool is closed last because future async checkpoint writes may
        # remain in flight until every request worker and provider has drained.
        if async_pool_started:
            try:
                await close_async_pool()
            except BaseException as error:
                cleanup_errors.append(error)
        reset_offload_limiters()

        _raise_lifespan_errors(primary_error, cleanup_errors)


def create_app() -> FastAPI:
    app = FastAPI(title=settings.api_title, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # Strip ``input`` when it's large or contains data-URI blobs.
        # Small inputs are preserved for debugging normal validation failures.
        detail = []
        for item in error.errors():
            raw_input = item.get("input")
            if raw_input is not None:
                s = str(raw_input)
                if len(s) > 256 or "data:" in s:
                    item = {k: v for k, v in item.items() if k != "input"}
            detail.append(item)
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(detail)})

    app.include_router(health_router)
    app.include_router(invoke_router)
    app.include_router(resume_router)
    app.include_router(cancel_router)
    return app


app = create_app()
