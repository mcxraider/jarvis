"""Stage 1: local token load / refresh / write-back + configured check.

All Google internals are stubbed via a FakeCreds so these tests exercise OUR
branching (missing token, valid token, expired-with-refresh, refresh failure)
without any network or browser.
"""

import json

import pytest

from agents.agent_api.app.tools.google_calendar.auth import (
    GoogleCalendarApiError,
    load_credentials,
)

CREDS_FACTORY = "google.oauth2.credentials.Credentials.from_authorized_user_file"
INFO_CREDS_FACTORY = "google.oauth2.credentials.Credentials.from_authorized_user_info"


class FakeCreds:
    def __init__(self, *, valid, expired, refresh_token, token="tok"):
        self._valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.token = token
        self.expiry = None
        self.refreshed = False
        self.refresh_request = None

    @property
    def valid(self):
        return self._valid

    def refresh(self, request):
        self.refresh_request = request
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


def test_oauth_refresh_transport_is_bounded_to_30_seconds(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    token.write_text('{"orig": true}')
    fake = FakeCreds(valid=False, expired=True, refresh_token="r")
    observed = []

    def raw_request(*args, **kwargs):
        observed.append((args, kwargs))
        return object()

    monkeypatch.setattr(CREDS_FACTORY, lambda path, scopes: fake)
    monkeypatch.setattr(
        "google.auth.transport.requests.Request",
        lambda: raw_request,
    )
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token))

    load_credentials()

    assert fake.refresh_request is not None
    fake.refresh_request(method="POST", url="https://oauth2.googleapis.com/token")
    assert observed[0][1]["timeout"] == 30.0


def test_valid_vault_json_does_not_touch_files(monkeypatch):
    fake = FakeCreds(valid=True, expired=False, refresh_token="r")
    monkeypatch.setattr(INFO_CREDS_FACTORY, lambda info, scopes: fake)

    creds = load_credentials(credential_json='{"refresh_token": "redacted"}')

    assert creds is fake
    assert fake.refreshed is False


def test_expired_vault_json_refreshes_through_callback(monkeypatch):
    fake = FakeCreds(valid=False, expired=True, refresh_token="r")
    monkeypatch.setattr(INFO_CREDS_FACTORY, lambda info, scopes: fake)
    persisted = []

    load_credentials(
        credential_json='{"refresh_token": "redacted"}',
        persist_callback=lambda payload, expiry: persisted.append((payload, expiry)),
    )

    assert fake.refreshed is True
    assert len(persisted) == 1
    assert json.loads(persisted[0][0])["token"] == "new-token"


def test_expired_vault_json_without_callback_fails_closed(monkeypatch):
    fake = FakeCreds(valid=False, expired=True, refresh_token="r")
    monkeypatch.setattr(INFO_CREDS_FACTORY, lambda info, scopes: fake)

    with pytest.raises(GoogleCalendarApiError):
        load_credentials(credential_json='{"refresh_token": "redacted"}')


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
