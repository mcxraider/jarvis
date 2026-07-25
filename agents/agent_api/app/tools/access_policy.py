"""Request-scoped resource restrictions for provider tool calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

from agents.agent_api.app.tools.errors import ClassifiedApiError
from agents.agent_api.app.tracing import TracePrinter

if TYPE_CHECKING:
    from agents.agent_api.app.user_context.preferences import AccessPreferences

_DROP = object()


@dataclass
class AccessDeniedError(ClassifiedApiError):
    """Sanitized policy failure routed through the standard tool envelope."""

    source: str = "access_policy"
    kind: str = "access_denied"
    message: str = "This resource is unavailable under the user's access policy."
    retryable: bool = False
    attempts: int = 1


class _ProviderTrace(TracePrinter):
    """Delegate metadata events while suppressing provider payload bodies."""

    _SAFE_EVENT_FIELDS = {
        "attempt",
        "endpoint",
        "has_body",
        "has_payload",
        "kind",
        "max_attempts",
        "method",
        "operation",
        "retryable",
        "status",
    }

    def __init__(self, delegate: TracePrinter):
        self._delegate = delegate
        super().__init__(
            enabled=getattr(delegate, "enabled", False),
            show_payloads=False,
        )

    def section(self, title: str) -> None:
        self._delegate.section(title)

    def event(self, stage: str, message: str, **fields: Any) -> None:
        self._delegate.event(
            stage,
            message,
            **{
                key: value
                for key, value in fields.items()
                if key in self._SAFE_EVENT_FIELDS
            },
        )

    def progress(self, fact: Dict[str, Any]) -> None:
        self._delegate.progress(fact)

    def narration(self, text: str) -> None:
        self._delegate.narration(text)

    def payload(self, stage: str, label: str, value: Any, limit: int = 900) -> None:
        return


class ResourceAccessPolicy:
    """Guard targeted calls and sanitize mixed provider results."""

    def __init__(self, preferences: Optional["AccessPreferences"] = None):
        todoist_resources = (
            preferences.restricted_todoist_projects if preferences is not None else ()
        )
        calendar_resources = (
            preferences.restricted_google_calendars if preferences is not None else ()
        )
        self._todoist_projects = {
            resource.id for resource in todoist_resources
        }
        self._calendar_ids = {
            resource.id for resource in calendar_resources
        }
        self._calendar_primary_restricted = any(
            resource.is_primary for resource in calendar_resources
        )
        self._restricted_task_ids: set[str] = set()

    @classmethod
    def from_preferences(
        cls,
        preferences: "AccessPreferences",
    ) -> "ResourceAccessPolicy":
        return cls(preferences)

    @property
    def has_restrictions(self) -> bool:
        return bool(
            self._todoist_projects
            or self._calendar_ids
            or self._calendar_primary_restricted
        )

    def provider_tracer(self, tracer: TracePrinter) -> TracePrinter:
        """Prevent mixed raw provider responses from entering debug/run logs."""

        return _ProviderTrace(tracer) if self.has_restrictions else tracer

    def trace_arguments(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Remove resource identifiers from request/run-log argument payloads."""

        if not self.has_restrictions:
            return arguments
        redacted = dict(arguments)
        for field_name in (
            "calendar_id",
            "calendar_ids",
            "comment_id",
            "project_id",
            "task_id",
        ):
            if field_name in redacted:
                redacted[field_name] = "[restricted-resource-id]"
        return redacted

    def guard(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        """Reject explicit references before argument payloads are traced."""

        project_id = arguments.get("project_id")
        if project_id is not None and str(project_id) in self._todoist_projects:
            raise AccessDeniedError()

        task_id = arguments.get("task_id")
        if task_id is not None and str(task_id) in self._restricted_task_ids:
            raise AccessDeniedError()

        if (
            self._todoist_projects
            and tool_name == "get_comments"
            and arguments.get("comment_id") is not None
        ):
            # A standalone comment id carries no project scope before retrieval.
            raise AccessDeniedError()

        calendar_id = arguments.get("calendar_id")
        if calendar_id is not None and self._calendar_restricted(str(calendar_id)):
            raise AccessDeniedError()
        if (
            calendar_id is None
            and tool_name
            in {
                "list_calendar_events",
                "get_calendar_event",
                "create_calendar_event",
                "update_calendar_event",
                "delete_calendar_event",
            }
            and self._calendar_primary_restricted
        ):
            raise AccessDeniedError()

        calendar_ids = arguments.get("calendar_ids")
        if calendar_ids and any(
            self._calendar_restricted(str(value)) for value in calendar_ids
        ):
            raise AccessDeniedError()
        if (
            not calendar_ids
            and tool_name == "get_freebusy"
            and self._calendar_primary_restricted
        ):
            raise AccessDeniedError()

    def filter_result(self, tool_name: str, result: Any) -> Any:
        """Remove restricted records before tracing or model exposure."""

        if not self.has_restrictions:
            return result

        if tool_name == "list_calendars" and isinstance(result, dict):
            filtered = dict(result)
            filtered["calendars"] = [
                calendar
                for calendar in result.get("calendars", [])
                if isinstance(calendar, dict)
                and not self._calendar_restricted(
                    str(calendar.get("calendar_id", ""))
                )
                and not (
                    calendar.get("primary") and self._calendar_primary_restricted
                )
            ]
            return filtered

        if tool_name == "get_freebusy" and isinstance(result, dict):
            filtered = dict(result)
            filtered["calendars"] = {
                calendar_id: value
                for calendar_id, value in result.get("calendars", {}).items()
                if not self._calendar_restricted(str(calendar_id))
            }
            return filtered

        if tool_name in {
            "get_todoist_task",
            "get_tasks",
            "get_tasks_by_filter",
            "get_completed_todoist_tasks_by_completion_date",
            "get_comments",
            "get_projects",
        }:
            sanitized = self._filter_todoist(
                result,
                projects=tool_name == "get_projects",
            )
            if sanitized is _DROP:
                raise AccessDeniedError()
            return sanitized

        return result

    def filter_todoist_provider_response(self, url: str, result: Any) -> Any:
        """Sanitize a Todoist HTTP response before provider tracing sees it."""

        if not self._todoist_projects:
            return result
        path = url.split("?", 1)[0].rstrip("/")
        sanitized = self._filter_todoist(
            result,
            projects=path.endswith("/projects"),
        )
        if sanitized is _DROP:
            raise AccessDeniedError()
        return sanitized

    def _calendar_restricted(self, calendar_id: str) -> bool:
        return (
            calendar_id in self._calendar_ids
            or (calendar_id == "primary" and self._calendar_primary_restricted)
        )

    def _filter_todoist(self, value: Any, *, projects: bool = False) -> Any:
        if isinstance(value, list):
            filtered = [
                item
                for item in (
                    self._filter_todoist(entry, projects=projects)
                    for entry in value
                )
                if item is not _DROP
            ]
            return filtered

        if not isinstance(value, dict):
            return value

        project_id = value.get("project_id")
        if project_id is not None and str(project_id) in self._todoist_projects:
            task_id = value.get("id") or value.get("task_id")
            if task_id is not None:
                self._restricted_task_ids.add(str(task_id))
            return _DROP

        if projects and value.get("id") is not None:
            if str(value["id"]) in self._todoist_projects:
                return _DROP

        task_id = value.get("task_id")
        if task_id is not None and str(task_id) in self._restricted_task_ids:
            return _DROP

        filtered: Dict[str, Any] = {}
        for key, item in value.items():
            child_projects = projects and key in {"results", "items", "projects"}
            sanitized = self._filter_todoist(item, projects=child_projects)
            if sanitized is not _DROP:
                filtered[key] = sanitized
        return filtered


__all__ = [
    "AccessDeniedError",
    "ResourceAccessPolicy",
]
