"""Atomic cancellation versus mutation phase tests."""

import threading

from agents.agent_api.app.graph.run_control import (
    CancelOutcome,
    RunControl,
    RunPhase,
)


def test_cancel_before_mutation_prevents_dispatch() -> None:
    control = RunControl()

    decision = control.request_cancel()

    assert decision.outcome is CancelOutcome.CANCELLED
    assert decision.cancel_task is True
    assert control.begin_mutation() is False
    assert control.phase is RunPhase.CANCELLED


def test_cancel_during_mutation_is_deferred_until_settlement() -> None:
    control = RunControl()
    assert control.begin_mutation() is True

    decision = control.request_cancel()

    assert decision.outcome is CancelOutcome.MUTATION_IN_FLIGHT
    assert decision.cancel_task is False
    assert control.phase is RunPhase.MUTATION_IN_FLIGHT
    assert control.finish_mutation() is True
    assert control.phase is RunPhase.CANCELLED


def test_cancel_and_mutation_start_race_has_one_atomic_winner() -> None:
    for _ in range(100):
        control = RunControl()
        barrier = threading.Barrier(3)
        results: dict[str, object] = {}

        def cancel() -> None:
            barrier.wait()
            results["cancel"] = control.request_cancel()

        def mutate() -> None:
            barrier.wait()
            results["mutation"] = control.begin_mutation()

        cancel_thread = threading.Thread(target=cancel)
        mutation_thread = threading.Thread(target=mutate)
        cancel_thread.start()
        mutation_thread.start()
        barrier.wait()
        cancel_thread.join(timeout=1)
        mutation_thread.join(timeout=1)

        decision = results["cancel"]
        mutation_started = results["mutation"]
        if mutation_started:
            assert decision.outcome is CancelOutcome.MUTATION_IN_FLIGHT
            assert control.finish_mutation() is True
        else:
            assert decision.outcome is CancelOutcome.CANCELLED
        assert control.phase is RunPhase.CANCELLED


def test_finished_run_cannot_be_cancelled_or_reopened() -> None:
    control = RunControl()
    assert control.try_mark_finished() is True

    decision = control.request_cancel()

    assert decision.outcome is CancelOutcome.ALREADY_FINISHED
    assert decision.cancel_task is False
    assert control.begin_mutation() is False


def test_cancelled_run_cannot_be_overwritten_by_normal_completion() -> None:
    control = RunControl()
    assert control.request_cancel().outcome is CancelOutcome.CANCELLED

    assert control.try_mark_finished() is False
    assert control.phase is RunPhase.CANCELLED

    control.mark_cancelled_finished()
    assert control.phase is RunPhase.FINISHED
    assert control.cancel_reason == "cancelled"
