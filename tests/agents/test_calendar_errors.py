"""Stage 0: shared ClassifiedApiError base + Todoist subclassing.

These tests prove the dispatcher-decoupling refactor is behavior-neutral: the
Todoist error keeps its exact payload shape while now subclassing the shared
base the dispatcher catches.
"""

from agents.agent_api.app.tools.dispatcher import ToolDispatcher
from agents.agent_api.app.tools.errors import ClassifiedApiError
from agents.agent_api.app.tools.todoist.client import TodoistApiError


class TestClassifiedApiErrorBase:
    def test_todoist_error_subclasses_base(self):
        assert issubclass(TodoistApiError, ClassifiedApiError)

    def test_todoist_error_is_catchable_as_base(self):
        try:
            raise TodoistApiError(kind="auth", message="nope")
        except ClassifiedApiError as err:
            assert err.kind == "auth"
        else:  # pragma: no cover - defensive
            raise AssertionError("TodoistApiError was not caught as ClassifiedApiError")

    def test_todoist_payload_shape_unchanged(self):
        err = TodoistApiError(
            kind="auth",
            message="x",
            status_code=401,
            retryable=False,
            attempts=2,
        )
        payload = err.to_classifier_payload()
        assert payload["source"] == "todoist"
        assert payload["kind"] == "auth"
        assert payload["retryable"] is False
        assert payload["status_code"] == 401
        assert payload["attempts"] == 2

    def test_base_default_payload(self):
        class _Sample(ClassifiedApiError):
            source = "sample"

        err = _Sample()
        err.kind = "rate-limit"
        payload = err.to_classifier_payload()
        assert payload["source"] == "sample"
        assert payload["kind"] == "rate-limit"


class TestDispatcherCatchesBase:
    """A registered handler raising ANY ClassifiedApiError yields a classified result."""

    def test_non_todoist_classified_error_is_classified(self):
        from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec

        class _Calendarish(ClassifiedApiError):
            source = "google_calendar"

            def __init__(self):
                self.kind = "not-found"
                self.message = "event missing"
                self.retryable = False

        def _boom(_args):
            raise _Calendarish()

        registry = ToolRegistry()
        registry.register(
            [ToolSpec(name="probe", openai_schema={"type": "function", "function": {"name": "probe"}}, handler=_boom)]
        )
        dispatcher = ToolDispatcher(registry, allow_mutations=True)

        result = dispatcher.execute_tool("call_1", "probe", {})

        assert result["success"] is False
        assert result["error"] == "event missing"
        assert result["classified_error"]["kind"] == "not-found"
        assert result["classified_error"]["source"] == "google_calendar"
