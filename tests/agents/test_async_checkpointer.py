"""Lifespan-owned asynchronous checkpointer tests."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.agent_api.app import checkpointing
from agents.agent_api.app.checkpointing.postgres import (
    create_async_postgres_checkpointer,
)


@pytest.fixture(autouse=True)
def _reset_async_checkpointer():
    checkpointing.reset_async_checkpointer()
    yield
    checkpointing.reset_async_checkpointer()


def test_async_postgres_factory_uses_exact_shared_pool() -> None:
    pool = MagicMock()
    saver = MagicMock()
    postgres_aio = ModuleType("langgraph.checkpoint.postgres.aio")
    postgres_aio.AsyncPostgresSaver = MagicMock(return_value=saver)

    with patch.dict(
        sys.modules,
        {"langgraph.checkpoint.postgres.aio": postgres_aio},
    ):
        result = create_async_postgres_checkpointer(pool)

    assert result is saver
    postgres_aio.AsyncPostgresSaver.assert_called_once_with(pool)


def test_sync_saver_compatibility_adapter_is_stable_and_offloads_methods() -> None:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    class SyncSaver(BaseCheckpointSaver):
        def __init__(self) -> None:
            super().__init__()
            self.writes = []

        def get_tuple(self, config):
            return ("tuple", config)

        def list(self, config, **_kwargs):
            yield ("listed", config)

        def put(self, config, checkpoint, metadata, new_versions):
            return {**config, "stored": checkpoint}

        def put_writes(self, config, writes, task_id, task_path=""):
            self.writes.append((config, writes, task_id, task_path))

        def delete_thread(self, thread_id):
            self.deleted = thread_id

    saver = SyncSaver()
    first = checkpointing.as_async_checkpointer(saver)
    second = checkpointing.as_async_checkpointer(saver)

    async def exercise() -> None:
        assert await first.aget_tuple({"thread": "one"}) == (
            "tuple",
            {"thread": "one"},
        )
        assert [item async for item in first.alist({"thread": "one"})] == [
            ("listed", {"thread": "one"})
        ]
        stored = await first.aput(
            {"thread": "one"},
            {"id": "checkpoint"},
            {},
            {},
        )
        assert stored["stored"] == {"id": "checkpoint"}
        await first.aput_writes({"thread": "one"}, [("key", "value")], "task")
        await first.adelete_thread("one")

    asyncio.run(exercise())

    assert first is second
    assert saver.writes[-1][2] == "task"
    assert saver.deleted == "one"


def test_async_postgres_factory_requires_pool_and_dependency() -> None:
    with pytest.raises(RuntimeError, match="open async Postgres pool"):
        create_async_postgres_checkpointer(None)

    with patch.dict(sys.modules, {"langgraph.checkpoint.postgres.aio": None}):
        with pytest.raises(RuntimeError, match="langgraph-checkpoint-postgres"):
            create_async_postgres_checkpointer(MagicMock())


def test_memory_runtime_reuses_default_checkpointer() -> None:
    memory = MagicMock()
    with patch.object(
        checkpointing,
        "settings",
        SimpleNamespace(checkpoint_backend="memory", run_checkpoint_setup=False),
    ), patch.object(checkpointing, "DEFAULT_CHECKPOINTER", memory):
        first = asyncio.run(checkpointing.initialize_async_checkpointer())
        second = asyncio.run(checkpointing.initialize_async_checkpointer())

    assert first is second is memory
    assert checkpointing.get_async_checkpointer() is memory


@pytest.mark.parametrize("run_setup", [False, True])
def test_postgres_runtime_optionally_awaits_setup(run_setup: bool) -> None:
    pool = MagicMock()
    saver = MagicMock()
    saver.setup = AsyncMock()
    with patch.object(
        checkpointing,
        "settings",
        SimpleNamespace(checkpoint_backend="postgres", run_checkpoint_setup=run_setup),
    ), patch.object(
        checkpointing,
        "_CHECKPOINT_SETUP_COMPLETE",
        False,
    ), patch.object(
        checkpointing,
        "create_async_postgres_checkpointer",
        return_value=saver,
    ) as create:
        result = asyncio.run(checkpointing.initialize_async_checkpointer(pool))

    assert result is saver
    create.assert_called_once_with(pool)
    if run_setup:
        saver.setup.assert_awaited_once_with()
    else:
        saver.setup.assert_not_awaited()


def test_sync_default_setup_runs_once_without_lifespan() -> None:
    saver = MagicMock()
    with patch.object(
        checkpointing,
        "settings",
        SimpleNamespace(checkpoint_backend="postgres", run_checkpoint_setup=True),
    ), patch.object(
        checkpointing,
        "DEFAULT_CHECKPOINTER",
        saver,
    ), patch.object(
        checkpointing,
        "_CHECKPOINT_SETUP_COMPLETE",
        False,
    ):
        checkpointing.ensure_default_checkpointer_setup()
        checkpointing.ensure_default_checkpointer_setup()

    saver.setup.assert_called_once_with()


def test_get_async_checkpointer_fails_before_lifespan_startup() -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        checkpointing.get_async_checkpointer()


def test_async_redis_backend_fails_explicitly() -> None:
    with patch.object(
        checkpointing,
        "settings",
        SimpleNamespace(checkpoint_backend="redis", run_checkpoint_setup=False),
    ):
        with pytest.raises(NotImplementedError, match="Async Redis"):
            asyncio.run(checkpointing.initialize_async_checkpointer())


def test_postgres_runtime_is_not_reused_by_another_event_loop() -> None:
    pool = MagicMock()
    saver = MagicMock()
    with patch.object(
        checkpointing,
        "settings",
        SimpleNamespace(checkpoint_backend="postgres", run_checkpoint_setup=False),
    ), patch.object(
        checkpointing,
        "create_async_postgres_checkpointer",
        return_value=saver,
    ):
        asyncio.run(checkpointing.initialize_async_checkpointer(pool))
        with pytest.raises(RuntimeError, match="different event loop"):
            asyncio.run(checkpointing.initialize_async_checkpointer(pool))


def test_concurrent_initialization_fails_closed_without_duplicate_setup() -> None:
    async def run() -> None:
        pool = MagicMock()
        saver = MagicMock()
        setup_entered = asyncio.Event()
        release_setup = asyncio.Event()

        async def setup() -> None:
            setup_entered.set()
            await release_setup.wait()

        saver.setup = AsyncMock(side_effect=setup)
        with patch.object(
            checkpointing,
            "settings",
            SimpleNamespace(checkpoint_backend="postgres", run_checkpoint_setup=True),
        ), patch.object(
            checkpointing,
            "_CHECKPOINT_SETUP_COMPLETE",
            False,
        ), patch.object(
            checkpointing,
            "create_async_postgres_checkpointer",
            return_value=saver,
        ) as create:
            first = asyncio.create_task(
                checkpointing.initialize_async_checkpointer(pool)
            )
            await setup_entered.wait()
            with pytest.raises(RuntimeError, match="already in progress"):
                await checkpointing.initialize_async_checkpointer(pool)
            release_setup.set()
            assert await first is saver

        create.assert_called_once_with(pool)
        saver.setup.assert_awaited_once_with()

    asyncio.run(run())
