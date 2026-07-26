"""Stage 3: schema/spec/langchain-tool layer consistency + dispatch routing.

The single most valuable structural test: the three layers must name the exact
same set of tools. Drift there means the model calls a tool that dispatches to
nothing.
"""

from unittest.mock import AsyncMock, MagicMock

from agents.agent_api.app.tools.google_calendar import (
    MUTATING_CALENDAR_TOOLS,
    build_calendar_langchain_tools,
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
    lc_names = {t.name for t in build_calendar_langchain_tools(lambda *a, **k: None)}
    assert schema_names == EXPECTED
    assert spec_names == EXPECTED
    assert lc_names == EXPECTED


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


def test_langchain_tool_routes_through_dispatch():
    calls = []

    def _dispatch(tool_call_id, name, args):
        calls.append((name, args))
        return {"ok": True}

    tools = {t.name: t for t in build_calendar_langchain_tools(_dispatch)}
    # Tools carry an InjectedToolCallId, so they must be invoked with a full
    # ToolCall envelope (the shape ToolNode passes at runtime), not flat args.
    tools["create_calendar_event"].invoke(
        {
            "name": "create_calendar_event",
            "type": "tool_call",
            "id": "call_1",
            "args": {
                "summary": "Standup",
                "start_datetime": "2026-07-02T14:00:00+08:00",
                "end_datetime": "2026-07-02T14:30:00+08:00",
            },
        }
    )
    assert calls[0][0] == "create_calendar_event"
    assert calls[0][1]["summary"] == "Standup"
    assert calls[0][1]["start_datetime"] == "2026-07-02T14:00:00+08:00"


def test_list_langchain_tools_forward_limit_and_page_token():
    calls = []

    def _dispatch(tool_call_id, name, args):
        calls.append((name, args))
        return {"ok": True}

    tools = {tool.name: tool for tool in build_calendar_langchain_tools(_dispatch)}
    tools["list_calendars"].invoke(
        {
            "name": "list_calendars",
            "type": "tool_call",
            "id": "call-calendars",
            "args": {"max_results": 25, "page_token": "cal-token"},
        }
    )
    tools["list_calendar_events"].invoke(
        {
            "name": "list_calendar_events",
            "type": "tool_call",
            "id": "call-events",
            "args": {
                "time_min": "2026-07-02T00:00:00+08:00",
                "time_max": "2026-07-03T00:00:00+08:00",
                "max_results": 30,
                "page_token": "event-token",
            },
        }
    )

    assert calls[0] == (
        "list_calendars",
        {"max_results": 25, "page_token": "cal-token"},
    )
    assert calls[1][0] == "list_calendar_events"
    assert calls[1][1]["max_results"] == 30
    assert calls[1][1]["page_token"] == "event-token"
