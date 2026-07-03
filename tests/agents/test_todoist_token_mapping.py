"""Tests for Todoist per-user token routing.

Comprehensive edge case coverage mirroring test_calendar_multi_user.py.
"""

import os
from unittest.mock import patch

import pytest

from agents.agent_api.app.tools.todoist.client import (
    TODOIST_KEY_FILE_MAP_ENV,
    TODOIST_TOKEN_MAP_ENV,
    TodoistApiClient,
    _parse_todoist_key_file_map,
    _parse_todoist_token_map,
    _read_todoist_key_file,
    todoist_api_key_for_telegram_user,
)


class TestParseTodoistTokenMap:
    """Tests for _parse_todoist_token_map."""

    def test_none_returns_empty(self):
        assert _parse_todoist_token_map(None) == {}

    def test_empty_string_returns_empty(self):
        assert _parse_todoist_token_map("") == {}

    def test_whitespace_only_returns_empty(self):
        assert _parse_todoist_token_map("   ") == {}

    def test_single_entry(self):
        result = _parse_todoist_token_map("701122767:abc123token")
        assert result == {"701122767": "abc123token"}

    def test_multiple_entries(self):
        raw = "701122767:token_jerry,387244560:token_zachary"
        result = _parse_todoist_token_map(raw)
        assert result == {
            "701122767": "token_jerry",
            "387244560": "token_zachary",
        }

    def test_strips_whitespace(self):
        raw = " 701122767 : token_jerry , 387244560 : token_zachary "
        result = _parse_todoist_token_map(raw)
        assert result == {
            "701122767": "token_jerry",
            "387244560": "token_zachary",
        }

    def test_trailing_comma_ignored(self):
        raw = "701122767:token_jerry,"
        result = _parse_todoist_token_map(raw)
        assert result == {"701122767": "token_jerry"}

    def test_malformed_no_colon_raises(self):
        with pytest.raises(ValueError, match=TODOIST_TOKEN_MAP_ENV):
            _parse_todoist_token_map("701122767token_jerry")

    def test_malformed_empty_id_raises(self):
        with pytest.raises(ValueError, match=TODOIST_TOKEN_MAP_ENV):
            _parse_todoist_token_map(":token_jerry")

    def test_malformed_empty_token_raises(self):
        with pytest.raises(ValueError, match=TODOIST_TOKEN_MAP_ENV):
            _parse_todoist_token_map("701122767:")


class TestTodoistApiKeyForTelegramUser:
    """Tests for todoist_api_key_for_telegram_user resolution chain."""

    def test_user_in_map_returns_mapped_key(self, monkeypatch):
        monkeypatch.setenv(TODOIST_TOKEN_MAP_ENV, "701122767:mapped_key")
        monkeypatch.setenv("TODOIST_API_KEY", "fallback_key")
        # Bypass DB lookup
        monkeypatch.setattr(
            "agents.agent_api.app.tools.todoist.client.todoist_api_key_for_telegram_user.__wrapped__",
            None,
            raising=False,
        )
        with patch("agents.agent_api.app.credentials.get_credential", return_value=None):
            assert todoist_api_key_for_telegram_user(701122767) == "mapped_key"

    def test_user_not_in_map_returns_none_when_map_configured(self, monkeypatch):
        monkeypatch.setenv(TODOIST_TOKEN_MAP_ENV, "701122767:mapped_key")
        monkeypatch.setenv("TODOIST_API_KEY", "fallback_key")
        with patch("agents.agent_api.app.credentials.get_credential", return_value=None):
            assert todoist_api_key_for_telegram_user(999999999) is None

    def test_none_telegram_user_id_returns_fallback(self, monkeypatch):
        monkeypatch.delenv(TODOIST_TOKEN_MAP_ENV, raising=False)
        monkeypatch.setenv("TODOIST_API_KEY", "fallback_key")
        with patch("agents.agent_api.app.credentials.get_credential", return_value=None):
            assert todoist_api_key_for_telegram_user(None) == "fallback_key"

    def test_no_map_no_fallback_returns_none(self, monkeypatch):
        monkeypatch.delenv(TODOIST_TOKEN_MAP_ENV, raising=False)
        monkeypatch.delenv("TODOIST_API_KEY", raising=False)
        with patch("agents.agent_api.app.credentials.get_credential", return_value=None):
            assert todoist_api_key_for_telegram_user(701122767) is None

    def test_db_credential_takes_priority(self, monkeypatch):
        monkeypatch.setenv(TODOIST_TOKEN_MAP_ENV, "701122767:env_key")
        monkeypatch.setenv("TODOIST_API_KEY", "fallback_key")
        with patch("agents.agent_api.app.credentials.get_credential", return_value="db_key"):
            assert todoist_api_key_for_telegram_user(701122767) == "db_key"

    def test_no_map_uses_single_fallback(self, monkeypatch):
        monkeypatch.delenv(TODOIST_TOKEN_MAP_ENV, raising=False)
        monkeypatch.setenv("TODOIST_API_KEY", "fallback_key")
        with patch("agents.agent_api.app.credentials.get_credential", return_value=None):
            assert todoist_api_key_for_telegram_user(701122767) == "fallback_key"


class TestTodoistApiClientPerUser:
    """Tests for TodoistApiClient constructor with per-user routing."""

    def test_client_uses_mapped_key(self, monkeypatch):
        monkeypatch.setenv(TODOIST_TOKEN_MAP_ENV, "111:token_one,222:token_two")
        monkeypatch.setenv("TODOIST_API_KEY", "fallback")
        with patch("agents.agent_api.app.credentials.get_credential", return_value=None):
            client = TodoistApiClient(telegram_user_id=222)
            assert client.api_key == "token_two"

    def test_client_with_explicit_key_ignores_map(self, monkeypatch):
        monkeypatch.setenv(TODOIST_TOKEN_MAP_ENV, "111:token_one")
        client = TodoistApiClient(api_key="explicit_key", telegram_user_id=111)
        assert client.api_key == "explicit_key"

    def test_client_without_user_id_uses_fallback(self, monkeypatch):
        monkeypatch.delenv(TODOIST_TOKEN_MAP_ENV, raising=False)
        monkeypatch.setenv("TODOIST_API_KEY", "fallback")
        with patch("agents.agent_api.app.credentials.get_credential", return_value=None):
            client = TodoistApiClient(telegram_user_id=None)
            assert client.api_key == "fallback"


class TestParseTodoistKeyFileMap:
    """Tests for _parse_todoist_key_file_map."""

    def test_none_returns_empty(self):
        assert _parse_todoist_key_file_map(None) == {}

    def test_empty_string_returns_empty(self):
        assert _parse_todoist_key_file_map("") == {}

    def test_single_entry(self):
        result = _parse_todoist_key_file_map("701122767:tokens/jerry_todoist.key")
        assert result == {"701122767": "tokens/jerry_todoist.key"}

    def test_multiple_entries(self):
        raw = "701122767:tokens/jerry.key,387244560:tokens/zac.key"
        result = _parse_todoist_key_file_map(raw)
        assert result == {
            "701122767": "tokens/jerry.key",
            "387244560": "tokens/zac.key",
        }

    def test_malformed_raises(self):
        with pytest.raises(ValueError, match=TODOIST_KEY_FILE_MAP_ENV):
            _parse_todoist_key_file_map("701122767")


class TestReadTodoistKeyFile:
    """Tests for _read_todoist_key_file."""

    def test_reads_key_from_file(self, tmp_path):
        key_file = tmp_path / "test.key"
        key_file.write_text("my_api_key_123")
        assert _read_todoist_key_file(str(key_file)) == "my_api_key_123"

    def test_strips_whitespace_and_newlines(self, tmp_path):
        key_file = tmp_path / "test.key"
        key_file.write_text("  my_api_key_123  \n")
        assert _read_todoist_key_file(str(key_file)) == "my_api_key_123"

    def test_nonexistent_file_returns_none(self):
        assert _read_todoist_key_file("/nonexistent/path.key") is None

    def test_empty_file_returns_none(self, tmp_path):
        key_file = tmp_path / "empty.key"
        key_file.write_text("")
        assert _read_todoist_key_file(str(key_file)) is None


class TestFileBasedKeyResolution:
    """Tests for file-based key resolution in todoist_api_key_for_telegram_user."""

    def test_file_key_used_when_inline_map_not_configured(self, tmp_path, monkeypatch):
        key_file = tmp_path / "jerry.key"
        key_file.write_text("file_based_key")
        monkeypatch.delenv(TODOIST_TOKEN_MAP_ENV, raising=False)
        monkeypatch.setenv(TODOIST_KEY_FILE_MAP_ENV, f"701122767:{key_file}")
        monkeypatch.setenv("TODOIST_API_KEY", "fallback")
        with patch("agents.agent_api.app.credentials.get_credential", return_value=None):
            assert todoist_api_key_for_telegram_user(701122767) == "file_based_key"

    def test_inline_map_takes_priority_over_file_map(self, tmp_path, monkeypatch):
        key_file = tmp_path / "jerry.key"
        key_file.write_text("file_key")
        monkeypatch.setenv(TODOIST_TOKEN_MAP_ENV, "701122767:inline_key")
        monkeypatch.setenv(TODOIST_KEY_FILE_MAP_ENV, f"701122767:{key_file}")
        with patch("agents.agent_api.app.credentials.get_credential", return_value=None):
            assert todoist_api_key_for_telegram_user(701122767) == "inline_key"

    def test_missing_file_falls_through_to_fallback(self, monkeypatch):
        monkeypatch.delenv(TODOIST_TOKEN_MAP_ENV, raising=False)
        monkeypatch.setenv(TODOIST_KEY_FILE_MAP_ENV, "701122767:/nonexistent/file.key")
        monkeypatch.setenv("TODOIST_API_KEY", "fallback")
        with patch("agents.agent_api.app.credentials.get_credential", return_value=None):
            assert todoist_api_key_for_telegram_user(701122767) is None

    def test_user_not_in_file_map_falls_through(self, tmp_path, monkeypatch):
        key_file = tmp_path / "jerry.key"
        key_file.write_text("jerry_key")
        monkeypatch.delenv(TODOIST_TOKEN_MAP_ENV, raising=False)
        monkeypatch.setenv(TODOIST_KEY_FILE_MAP_ENV, f"701122767:{key_file}")
        monkeypatch.setenv("TODOIST_API_KEY", "fallback")
        with patch("agents.agent_api.app.credentials.get_credential", return_value=None):
            assert todoist_api_key_for_telegram_user(999999) == "fallback"
