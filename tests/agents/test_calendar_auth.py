"""Stage 1: local token load / refresh / write-back + configured check.

All Google internals are stubbed via a FakeCreds so these tests exercise OUR
branching (missing token, valid token, expired-with-refresh, refresh failure)
without any network or browser.
"""

import json

import pytest

from agents.agent_api.app.tools.calendar.auth import (
    GoogleCalendarApiError,
    is_calendar_configured,
    is_calendar_enabled,
    load_credentials,
)

CREDS_FACTORY = "google.oauth2.credentials.Credentials.from_authorized_user_file"


class FakeCreds:
    def __init__(self, *, valid, expired, refresh_token, token="tok"):
        self._valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.token = token
        self.refreshed = False

    @property
    def valid(self):
        return self._valid

    def refresh(self, _request):
        self.refreshed = True
        self._valid = True
        self.expired = False
        self.token = "new-token"

    def to_json(self):
        return json.dumps({"token": self.token, "refresh_token": self.refresh_token})


def test_missing_token_raises_reconnect(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "absent.json"))
    with pytest.raises(GoogleCalendarApiError) as excinfo:
        load_credentials()
    assert excinfo.value.kind == "auth"
    assert excinfo.value.reconnect is True


def test_valid_token_no_refresh_file_unchanged(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    token.write_text('{"orig": true}')
    fake = FakeCreds(valid=True, expired=False, refresh_token="r")
    monkeypatch.setattr(CREDS_FACTORY, lambda path, scopes: fake)
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token))

    creds = load_credentials()

    assert creds is fake
    assert fake.refreshed is False
    assert token.read_text() == '{"orig": true}'


def test_expired_token_refreshes_and_persists(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    token.write_text('{"orig": true}')
    fake = FakeCreds(valid=False, expired=True, refresh_token="r")
    monkeypatch.setattr(CREDS_FACTORY, lambda path, scopes: fake)
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token))

    load_credentials()

    assert fake.refreshed is True
    persisted = json.loads(token.read_text())
    assert persisted["token"] == "new-token"  # refreshed token written back

    # Second load: the credential is now valid, so no re-refresh occurs.
    fake.refreshed = False
    load_credentials()
    assert fake.refreshed is False


def test_refresh_failure_raises_reconnect(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    fake = FakeCreds(valid=False, expired=True, refresh_token="r")

    def _boom(_request):
        raise RuntimeError("refresh token revoked")

    fake.refresh = _boom
    monkeypatch.setattr(CREDS_FACTORY, lambda path, scopes: fake)
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token))

    with pytest.raises(GoogleCalendarApiError) as excinfo:
        load_credentials()
    assert excinfo.value.kind == "auth"
    assert excinfo.value.reconnect is True


def test_expired_without_refresh_token_raises_reconnect(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    fake = FakeCreds(valid=False, expired=True, refresh_token=None)
    monkeypatch.setattr(CREDS_FACTORY, lambda path, scopes: fake)
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token))

    with pytest.raises(GoogleCalendarApiError) as excinfo:
        load_credentials()
    assert excinfo.value.reconnect is True


def test_is_calendar_configured_tracks_token_file(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token))
    monkeypatch.delenv("GOOGLE_CALENDAR_ENABLED", raising=False)  # default on
    assert is_calendar_configured() is False
    token.write_text("{}")
    assert is_calendar_configured() is True


def test_is_calendar_enabled_toggle(monkeypatch):
    monkeypatch.delenv("GOOGLE_CALENDAR_ENABLED", raising=False)
    assert is_calendar_enabled() is True  # default on
    for off in ("false", "0", "no", "off", "FALSE", "Off"):
        monkeypatch.setenv("GOOGLE_CALENDAR_ENABLED", off)
        assert is_calendar_enabled() is False, off
    for on in ("true", "1", "yes", "on", ""):
        monkeypatch.setenv("GOOGLE_CALENDAR_ENABLED", on)
        assert is_calendar_enabled() is True, on


def test_toggle_off_disables_even_with_token(monkeypatch, tmp_path):
    # The token stays on disk; the toggle alone disables the domain.
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token))
    monkeypatch.setenv("GOOGLE_CALENDAR_ENABLED", "false")
    assert is_calendar_configured() is False
    monkeypatch.setenv("GOOGLE_CALENDAR_ENABLED", "true")
    assert is_calendar_configured() is True  # token untouched; flips right back on
