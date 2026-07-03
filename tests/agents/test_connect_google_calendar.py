"""Stage 7: unit tests for the Google Calendar connect script.

The interactive consent flow (``run_consent_flow``) opens a browser and can't be
automated, so it's kept thin and injected. Everything around it — config
loading, token persistence, orchestration, and the CLI's exit codes — is pure
enough to test without network, browser, or a real Google client. No test writes
a token to the repo root; all writes go to ``tmp_path``.
"""

import json

import pytest

from scripts import connect_google_calendar as cg

_INSTALLED_CONFIG = {
    "installed": {
        "client_id": "abc.apps.googleusercontent.com",
        "client_secret": "shh",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


class _FakeCreds:
    """Stand-in for google.oauth2.credentials.Credentials — only to_json is used."""

    def __init__(self, payload='{"token": "fake", "refresh_token": "r"}'):
        self._payload = payload

    def to_json(self) -> str:
        return self._payload


# -- load_client_config -------------------------------------------------------


def test_load_client_config_from_raw_json():
    config = cg.load_client_config(json.dumps(_INSTALLED_CONFIG))
    assert config["installed"]["client_id"].endswith("googleusercontent.com")


def test_load_client_config_accepts_web_key():
    config = cg.load_client_config(json.dumps({"web": {"client_id": "x"}}))
    assert "web" in config


def test_load_client_config_from_file_path(tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps(_INSTALLED_CONFIG), encoding="utf-8")
    config = cg.load_client_config(str(path))
    assert config["installed"]["client_secret"] == "shh"


def test_load_client_config_reads_env_when_raw_omitted(monkeypatch):
    monkeypatch.setenv(cg.CLIENT_SECRETS_ENV, json.dumps(_INSTALLED_CONFIG))
    config = cg.load_client_config()
    assert "installed" in config


def test_load_client_config_missing_env_raises(monkeypatch):
    monkeypatch.delenv(cg.CLIENT_SECRETS_ENV, raising=False)
    with pytest.raises(cg.ConnectError) as exc:
        cg.load_client_config()
    assert cg.CLIENT_SECRETS_ENV in str(exc.value)


def test_load_client_config_blank_raises(monkeypatch):
    monkeypatch.delenv(cg.CLIENT_SECRETS_ENV, raising=False)
    with pytest.raises(cg.ConnectError):
        cg.load_client_config("   ")


def test_load_client_config_invalid_json_raises():
    with pytest.raises(cg.ConnectError) as exc:
        cg.load_client_config("{not json")
    assert "valid JSON" in str(exc.value)


def test_load_client_config_wrong_shape_raises():
    # Valid JSON, but not a client config (no 'installed'/'web').
    with pytest.raises(cg.ConnectError) as exc:
        cg.load_client_config(json.dumps({"nope": 1}))
    assert "installed" in str(exc.value)


# -- save_credentials ---------------------------------------------------------


def test_save_credentials_writes_to_json(tmp_path):
    token_path = tmp_path / "token.json"
    cg.save_credentials(_FakeCreds('{"token": "abc"}'), str(token_path))
    assert json.loads(token_path.read_text(encoding="utf-8")) == {"token": "abc"}


# -- connect (orchestration, injected flow) -----------------------------------


def test_connect_writes_token_and_returns_path(tmp_path):
    token_path = tmp_path / "token.json"
    seen = {}

    def fake_flow(config, scopes):
        seen["config"] = config
        seen["scopes"] = scopes
        return _FakeCreds('{"token": "live"}')

    written = cg.connect(
        client_secrets=json.dumps(_INSTALLED_CONFIG),
        token_path=str(token_path),
        scopes=["https://www.googleapis.com/auth/calendar"],
        flow_runner=fake_flow,
    )

    assert written == str(token_path)
    assert json.loads(token_path.read_text(encoding="utf-8")) == {"token": "live"}
    # The parsed client config and scopes reach the flow unchanged.
    assert seen["config"]["installed"]["client_id"].endswith("googleusercontent.com")
    assert seen["scopes"] == ["https://www.googleapis.com/auth/calendar"]


def test_connect_defaults_scopes_and_path_from_auth(monkeypatch, tmp_path):
    token_path = tmp_path / "token.json"
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))
    monkeypatch.setenv("GOOGLE_CALENDAR_SCOPES", "https://www.googleapis.com/auth/calendar.events")
    captured = {}

    def fake_flow(config, scopes):
        captured["scopes"] = scopes
        return _FakeCreds()

    written = cg.connect(
        client_secrets=json.dumps(_INSTALLED_CONFIG),
        flow_runner=fake_flow,
    )

    # Defaults are sourced from calendar.auth, proving the connect flow and the
    # runtime loader can't drift on scope/path.
    assert written == str(token_path)
    assert captured["scopes"] == ["https://www.googleapis.com/auth/calendar.events"]
    assert token_path.exists()


def test_connect_missing_secrets_raises_before_flow(monkeypatch):
    monkeypatch.delenv(cg.CLIENT_SECRETS_ENV, raising=False)
    called = []

    def fake_flow(config, scopes):
        called.append(True)
        return _FakeCreds()

    with pytest.raises(cg.ConnectError):
        cg.connect(flow_runner=fake_flow)
    assert called == []  # never reaches the browser flow


# -- main (CLI exit codes) ----------------------------------------------------


def test_main_success_writes_token_and_returns_zero(monkeypatch, tmp_path, capsys):
    token_path = tmp_path / "token.json"
    monkeypatch.setenv(cg.CLIENT_SECRETS_ENV, json.dumps(_INSTALLED_CONFIG))
    monkeypatch.setattr(cg, "run_consent_flow", lambda config, scopes: _FakeCreds('{"token": "ok"}'))

    code = cg.main(["--token-path", str(token_path)])

    assert code == 0
    assert token_path.exists()
    assert "Google Calendar connected" in capsys.readouterr().out


def test_main_missing_secrets_returns_one(monkeypatch, capsys):
    monkeypatch.delenv(cg.CLIENT_SECRETS_ENV, raising=False)
    # Even if the flow were reachable, it must not run without config.
    monkeypatch.setattr(cg, "run_consent_flow", lambda config, scopes: _FakeCreds())

    code = cg.main([])

    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_main_scopes_flag_is_split(monkeypatch, tmp_path):
    token_path = tmp_path / "token.json"
    monkeypatch.setenv(cg.CLIENT_SECRETS_ENV, json.dumps(_INSTALLED_CONFIG))
    seen = {}

    def fake_flow(config, scopes):
        seen["scopes"] = scopes
        return _FakeCreds()

    monkeypatch.setattr(cg, "run_consent_flow", fake_flow)

    code = cg.main(["--token-path", str(token_path), "--scopes", "scope_a scope_b"])

    assert code == 0
    assert seen["scopes"] == ["scope_a", "scope_b"]
