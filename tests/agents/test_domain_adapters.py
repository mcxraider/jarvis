"""The domain adapter registry is the single registration point for a domain."""

import pytest

from agents.agent_api.app.credentials import IntegrationCredential
from agents.agent_api.app.tools.domain_adapters import (
    DOMAIN_ADAPTERS,
    CredentialValidationError,
)


def _credential(provider: str, secret: str) -> IntegrationCredential:
    return IntegrationCredential(connection_id="c1", provider=provider, secret=secret)


def test_every_adapter_is_fully_populated():
    for key, adapter in DOMAIN_ADAPTERS.items():
        assert adapter.key == key
        assert adapter.capabilities, f"{key} has no capabilities"
        assert adapter.prompt_fragment.strip(), f"{key} has no prompt fragment"
        assert adapter.grounding_note.strip(), f"{key} has no grounding note"
        assert callable(adapter.build_client)
        assert callable(adapter.get_tool_specs)
        assert adapter.credential_validator is None or callable(adapter.credential_validator)


def test_calendar_fragment_is_self_contained():
    # Each fragment must read correctly even when the other domain is unavailable.
    assert "delete_todoist_task" not in DOMAIN_ADAPTERS["google_calendar"].prompt_fragment


def test_todoist_validator_rejects_empty_key():
    validate = DOMAIN_ADAPTERS["todoist"].credential_validator
    validate(_credential("todoist", "a-real-key"))  # does not raise
    with pytest.raises(CredentialValidationError):
        validate(_credential("todoist", "   "))


def test_calendar_validator_requires_refresh_token():
    validate = DOMAIN_ADAPTERS["google_calendar"].credential_validator
    validate(_credential("google_calendar", '{"refresh_token": "r", "token": "t"}'))
    with pytest.raises(CredentialValidationError):
        validate(_credential("google_calendar", '{"token": "t"}'))
    with pytest.raises(CredentialValidationError):
        validate(_credential("google_calendar", "not json"))
