"""Pure availability classification: adapters × connections × secrets → domains.

No database or network: ``classify_domains`` takes plain rows and an injected
secret resolver, so every availability branch is a table-driven assertion.
"""

from types import SimpleNamespace

from agents.agent_api.app.user_context.domains import ConnectionRow, classify_domains


def _adapters():
    return {
        "todoist": SimpleNamespace(capabilities=["tasks"]),
        "google_calendar": SimpleNamespace(capabilities=["calendar_events"]),
    }


def _by_provider(domains):
    return {domain.provider: domain for domain in domains}


def test_active_when_connected_enabled_and_secret_resolves():
    connections = {"todoist": ConnectionRow("c1", "todoist", "connected", True)}
    domains, credentials = classify_domains(
        _adapters(), connections, lambda provider: "secret" if provider == "todoist" else None
    )

    todoist = _by_provider(domains)["todoist"]
    assert todoist.status == "active"
    assert todoist.reason is None
    assert todoist.capabilities == ["tasks"]
    assert credentials["todoist"].secret == "secret"
    assert credentials["todoist"].connection_id == "c1"


def test_no_connection_is_not_connected():
    domains, credentials = classify_domains(_adapters(), {}, lambda provider: "secret")
    calendar = _by_provider(domains)["google_calendar"]
    assert calendar.status == "unavailable"
    assert calendar.reason == "not_connected"
    assert calendar.connection_id is None
    assert credentials == {}


def test_disabled_connection_is_disabled():
    connections = {"todoist": ConnectionRow("c1", "todoist", "connected", False)}
    domains, credentials = classify_domains(_adapters(), connections, lambda provider: "secret")
    todoist = _by_provider(domains)["todoist"]
    assert (todoist.status, todoist.reason) == ("unavailable", "disabled")
    assert "todoist" not in credentials


def test_non_connected_status_is_passed_through_as_reason():
    connections = {"todoist": ConnectionRow("c1", "todoist", "needs_reauth", True)}
    domains, _ = classify_domains(_adapters(), connections, lambda provider: "secret")
    todoist = _by_provider(domains)["todoist"]
    assert (todoist.status, todoist.reason) == ("unavailable", "needs_reauth")


def test_credential_unavailable_when_secret_missing():
    connections = {"todoist": ConnectionRow("c1", "todoist", "connected", True)}
    domains, credentials = classify_domains(_adapters(), connections, lambda provider: None)
    todoist = _by_provider(domains)["todoist"]
    assert (todoist.status, todoist.reason) == ("unavailable", "credential_unavailable")
    assert "todoist" not in credentials


def test_connection_without_adapter_is_unsupported():
    connections = {"notion": ConnectionRow("c9", "notion", "connected", True)}
    domains, credentials = classify_domains(_adapters(), connections, lambda provider: "secret")
    notion = _by_provider(domains)["notion"]
    assert (notion.status, notion.reason) == ("unsupported", "no_adapter")
    assert "notion" not in credentials


def test_adapters_are_ordered_before_unsupported_connections():
    connections = {
        "notion": ConnectionRow("c9", "notion", "connected", True),
        "todoist": ConnectionRow("c1", "todoist", "connected", True),
    }
    domains, _ = classify_domains(_adapters(), connections, lambda provider: "secret")
    providers = [domain.provider for domain in domains]
    assert providers.index("todoist") < providers.index("notion")
    assert providers.index("google_calendar") < providers.index("notion")
