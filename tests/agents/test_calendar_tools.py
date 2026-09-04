"""Stage 3: schema/spec layer consistency + handler binding.

The single most valuable structural test: the two layers must name the exact
same set of tools. Drift there means the model calls a tool that dispatches to
nothing.
"""

from unittest.mock import AsyncMock, MagicMock

from agents.agent_api.app.tools.google_calendar import (
    MUTATING_CALENDAR_TOOLS,
    get_calendar_tool_schemas,
    get_calendar_tool_specs,
)

EXPECTED = {
    "list_calendars",
    "list_calendar_events",
    "get_calendar_event",
    "create_calendar_event",
    "update_calendar_event",
    "delete_calendar_event",
    "get_freebusy",
}


def _schema(name):
    return next(
        schema
        for schema in get_calendar_tool_schemas()
        if schema["function"]["name"] == name
    )


def test_layer_consistency():
    schema_names = {s["function"]["name"] for s in get_calendar_tool_schemas()}
    spec_names = {s.name for s in get_calendar_tool_specs(MagicMock())}
    assert schema_names == EXPECTED
    assert spec_names == EXPECTED


def test_collection_schemas_publish_defaults_and_page_tokens():
    for name in ("list_calendars", "list_calendar_events"):
        properties = _schema(name)["function"]["parameters"]["properties"]
        assert properties["max_results"]["default"] == 50
        assert properties["max_results"]["maximum"] == 250
        assert "page_token" in properties


def test_mutating_flags():
    specs = {s.name: s for s in get_calendar_tool_specs(MagicMock())}
    assert MUTATING_CALENDAR_TOOLS == {
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
    }
    for name, spec in specs.items():
        assert spec.mutating is (name in MUTATING_CALENDAR_TOOLS)


def test_spec_handlers_bound_to_client():
    client = MagicMock()
    specs = {s.name: s for s in get_calendar_tool_specs(client)}
    specs["list_calendars"].handler({})
    client.list_calendars.assert_called_once()
    specs["delete_calendar_event"].handler({"event_id": "e1"})
    client.delete_calendar_event.assert_called_once_with({"event_id": "e1"})


def test_spec_async_handlers_bound_to_client():
    client = MagicMock()
    for name in EXPECTED:
        setattr(client, f"async_{name}", AsyncMock())
    specs = {s.name: s for s in get_calendar_tool_specs(client)}

    assert specs["list_calendars"].async_handler is client.async_list_calendars
    assert (
        specs["list_calendar_events"].async_handler
        is client.async_list_calendar_events
    )
    assert specs["get_calendar_event"].async_handler is client.async_get_calendar_event
    assert (
        specs["create_calendar_event"].async_handler
        is client.async_create_calendar_event
    )
    assert (
        specs["update_calendar_event"].async_handler
        is client.async_update_calendar_event
    )
    assert (
        specs["delete_calendar_event"].async_handler
        is client.async_delete_calendar_event
    )
    assert specs["get_freebusy"].async_handler is client.async_get_freebusy
