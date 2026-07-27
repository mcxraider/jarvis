"""Validation and snapshot coverage for per-domain execution comments."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agents.agent_api.app.user_context.preferences import AssistantPreferencesV1
from agents.agent_api.app.user_context.runtime import RuntimeContextSnapshot
from tests.agents.runtime_helpers import make_preferences, make_snapshot


TODOIST_COMMENT = (
    "When adding Todoist items, apply the `task` or `event` label according "
    "to the item type."
)


def _payload() -> dict:
    return make_preferences(
        todoist_comments=[TODOIST_COMMENT],
        google_calendar_comments=[],
    ).model_dump(mode="json")


def test_comments_accept_omitted_empty_and_populated_lists():
    omitted = make_preferences()
    assert omitted.domains.todoist.user_domain_specific_comments == []
    assert omitted.domains.google_calendar.user_domain_specific_comments == []

    populated = AssistantPreferencesV1.model_validate(_payload())
    assert populated.domains.todoist.user_domain_specific_comments == [
        TODOIST_COMMENT
    ]
    assert populated.domains.google_calendar.user_domain_specific_comments == []


@pytest.mark.parametrize(
    ("domain", "value"),
    [
        ("todoist", "not-an-array"),
        ("google_calendar", {"comment": "not-an-array"}),
        ("todoist", [1]),
        ("google_calendar", [" \n\t "]),
        ("todoist", ["x" * 201]),
        ("google_calendar", [str(index) for index in range(11)]),
    ],
)
def test_comments_reject_invalid_shapes_and_bounds(domain, value):
    payload = _payload()
    payload["domains"][domain]["user_domain_specific_comments"] = value

    with pytest.raises(ValidationError):
        AssistantPreferencesV1.model_validate(payload)


def test_comments_reject_unknown_fields():
    payload = _payload()
    payload["domains"]["todoist"]["unexpected"] = True

    with pytest.raises(ValidationError):
        AssistantPreferencesV1.model_validate(payload)


def test_runtime_snapshot_serializes_and_rehydrates_comments():
    snapshot = make_snapshot(
        preferences=make_preferences(
            todoist_comments=[TODOIST_COMMENT],
            google_calendar_comments=[],
        )
    )

    serialized = snapshot.model_dump_json()
    restored = RuntimeContextSnapshot.model_validate(json.loads(serialized))

    assert restored == snapshot
    assert (
        restored.preferences.domains.todoist.user_domain_specific_comments
        == [TODOIST_COMMENT]
    )


def test_migration_extends_both_domain_paths_without_bumping_schema_version():
    migration = (
        Path(__file__).parents[2]
        / "supabase"
        / "migrations"
        / "20260727122414_add_user_domain_specific_comments.sql"
    ).read_text(encoding="utf-8")

    assert "create or replace function private.is_valid_user_preferences_v1" in migration
    assert (
        "{domains,todoist,user_domain_specific_comments}', 10, 200"
        in migration
    )
    assert (
        "{domains,google_calendar,user_domain_specific_comments}'" in migration
    )
    assert "\n      10,\n      200\n" in migration
    assert "schema_version = 2" not in migration
