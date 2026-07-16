"""Real-task coverage for the active-run registry."""

import asyncio
import threading

import pytest

from agents.agent_api.app.api.active_runs import ActiveRunRegistry
from agents.agent_api.app.config import load_settings
from agents.agent_api.app.graph.run_control import CancelOutcome, RunControl


def test_run_deadline_defaults_to_120_seconds(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_RUN_DEADLINE_SECONDS", raising=False)

    assert load_settings().run_deadline_seconds == 120.0


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_run_deadline_rejects_non_positive_or_non_finite_values(
    monkeypatch,
    value: str,
) -> None:
    monkeypatch.setenv("JARVIS_RUN_DEADLINE_SECONDS", value)

    with pytest.raises(ValueError, match="JARVIS_RUN_DEADLINE_SECONDS"):
        load_settings()


def test_duplicate_registration_rejected_and_cleanup_is_identity_aware() -> None:
    async def scenario() -> None:
        registry = ActiveRunRegistry()
        release_first = asyncio.Event()
        release_second = asyncio.Event()
        first = asyncio.create_task(release_first.wait())
        first_run = registry.register(
            user_id="user",
            request_id="request",
            thread_id="thread-1",
            deadline=asyncio.get_running_loop().time() + 60,
            task=first,
            control=RunControl(),
        )

        duplicate = asyncio.create_task(asyncio.sleep(0))
        with pytest.raises(RuntimeError, match="already owns"):
            registry.register(
                user_id="user",
                request_id="request",
                thread_id="thread-duplicate",
                deadline=asyncio.get_running_loop().time() + 60,
                task=duplicate,
                control=RunControl(),
            )
        await duplicate

        release_first.set()
        await first
        await asyncio.sleep(0)

        second = asyncio.create_task(release_second.wait())
        second_run = registry.register(
            user_id="user",
            request_id="request",
            thread_id="thread-2",
            deadline=asyncio.get_running_loop().time() + 60,
            task=second,
            control=RunControl(),
        )
        assert registry.finish(first_run) is False
        assert registry.get("user", "request") is second_run

        release_second.set()
        await second
        await asyncio.sleep(0)
        assert registry.count == 0
        registry.reset()

    asyncio.run(scenario())


def test_cancel_is_loop_safe_idempotent_and_user_scoped() -> None:
    async def scenario() -> None:
        registry = ActiveRunRegistry()
        started = asyncio.Event()

        async def worker() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(worker())
        registry.register(
            user_id="user-a",
            request_id="same-id",
            thread_id=None,
            deadline=asyncio.get_running_loop().time() + 60,
            task=task,
            control=RunControl(),
        )
        await started.wait()

        assert registry.cancel("user-b", "same-id") is CancelOutcome.NOT_FOUND
        outcomes: list[CancelOutcome] = []
        thread = threading.Thread(
            target=lambda: outcomes.append(registry.cancel("user-a", "same-id"))
        )
        thread.start()
        thread.join(timeout=1)
        assert outcomes == [CancelOutcome.CANCELLED]

        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert registry.count == 0
        assert registry.cancel("user-a", "same-id") is CancelOutcome.CANCELLED
        registry.reset()

    asyncio.run(scenario())


def test_cancel_before_registration_is_applied_to_late_producer() -> None:
    async def scenario() -> None:
        registry = ActiveRunRegistry()
        control = RunControl()

        assert registry.cancel("user", "late-register") is CancelOutcome.NOT_FOUND

        task = asyncio.create_task(asyncio.Event().wait())
        registry.register(
            user_id="user",
            request_id="late-register",
            thread_id=None,
            deadline=asyncio.get_running_loop().time() + 60,
            task=task,
            control=control,
        )

        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

        assert control.cancel_reason == "cancelled"
        assert registry.count == 0
        assert registry.cancel("user", "late-register") is CancelOutcome.CANCELLED
        registry.reset()

    asyncio.run(scenario())


def test_pre_registration_cancel_is_user_scoped() -> None:
    async def scenario() -> None:
        registry = ActiveRunRegistry()
        assert registry.cancel("other-user", "shared-id") is CancelOutcome.NOT_FOUND

        task = asyncio.create_task(asyncio.sleep(0))
        control = RunControl()
        registry.register(
            user_id="user",
            request_id="shared-id",
            thread_id=None,
            deadline=asyncio.get_running_loop().time() + 60,
            task=task,
            control=control,
        )
        await task
        await asyncio.sleep(0)

        assert control.cancel_reason is None
        assert registry.cancel("user", "shared-id") is CancelOutcome.ALREADY_FINISHED
        registry.reset()

    asyncio.run(scenario())


def test_foreign_thread_finish_marshals_deadline_cancellation_to_owner_loop() -> None:
    async def scenario() -> None:
        registry = ActiveRunRegistry()
        loop = asyncio.get_running_loop()
        previous_debug = loop.get_debug()
        loop.set_debug(True)
        producer = asyncio.create_task(asyncio.Event().wait())
        run = registry.register(
            user_id="user",
            request_id="foreign-finish",
            thread_id=None,
            deadline=loop.time() + 60,
            task=producer,
            control=RunControl(),
        )
        errors: list[BaseException] = []

        def finish_from_thread() -> None:
            try:
                registry.finish(run)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=finish_from_thread)
        try:
            thread.start()
            thread.join(timeout=1)
            assert not thread.is_alive()
            assert errors == []
            assert run.deadline_task is not None
            with pytest.raises(asyncio.CancelledError):
                await run.deadline_task
            assert run.deadline_task.cancelled()
        finally:
            producer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await producer
            loop.set_debug(previous_debug)

    asyncio.run(scenario())


def test_mutation_in_flight_is_not_task_cancelled() -> None:
    async def scenario() -> None:
        registry = ActiveRunRegistry()
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        control = RunControl()
        assert control.begin_mutation() is True
        registry.register(
            user_id="user",
            request_id="mutation",
            thread_id="thread",
            deadline=asyncio.get_running_loop().time() + 60,
            task=task,
            control=control,
        )

        assert (
            registry.cancel("user", "mutation")
            is CancelOutcome.MUTATION_IN_FLIGHT
        )
        await asyncio.sleep(0)
        assert not task.done()
        assert control.finish_mutation() is True

        release.set()
        await task
        await asyncio.sleep(0)
        assert registry.cancel("user", "mutation") is CancelOutcome.CANCELLED
        registry.reset()

    asyncio.run(scenario())


def test_deadline_actually_cancels_the_registered_task() -> None:
    async def scenario() -> None:
        registry = ActiveRunRegistry()
        started = asyncio.Event()

        async def worker() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(worker())
        registry.register(
            user_id="user",
            request_id="deadline",
            thread_id=None,
            deadline=asyncio.get_running_loop().time() + 0.01,
            task=task,
            control=RunControl(),
        )
        await started.wait()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        await asyncio.sleep(0)
        assert registry.count == 0
        assert (
            registry.cancel("user", "deadline")
            is CancelOutcome.ALREADY_FINISHED
        )
        registry.reset()

    asyncio.run(scenario())


def test_normal_completion_retains_bounded_finished_outcome() -> None:
    async def scenario() -> None:
        registry = ActiveRunRegistry()
        task = asyncio.create_task(asyncio.sleep(0))
        registry.register(
            user_id="user",
            request_id="finished",
            thread_id=None,
            deadline=asyncio.get_running_loop().time() + 60,
            task=task,
            control=RunControl(),
        )
        await task
        await asyncio.sleep(0)
        assert (
            registry.cancel("user", "finished")
            is CancelOutcome.ALREADY_FINISHED
        )
        registry.reset()

    asyncio.run(scenario())
