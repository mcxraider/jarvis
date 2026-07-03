"""Stage 1 tests: per-user Google Calendar token routing."""

import os
from unittest.mock import patch

import pytest

from agents.agent_api.app.tools.calendar.auth import (
    GOOGLE_TOKEN_MAP_ENV,
    _parse_google_token_map,
    get_token_path_for_user,
    is_calendar_configured_for_user,
)


class TestParseGoogleTokenMap:
    """Tests for _parse_google_token_map."""

    def test_none_returns_empty(self):
        assert _parse_google_token_map(None) == {}

    def test_empty_string_returns_empty(self):
        assert _parse_google_token_map("") == {}

    def test_whitespace_only_returns_empty(self):
        assert _parse_google_token_map("   ") == {}

    def test_single_entry(self):
        result = _parse_google_token_map("701122767:tokens/jerry_token.json")
        assert result == {"701122767": "tokens/jerry_token.json"}

    def test_multiple_entries(self):
        raw = "701122767:tokens/jerry_token.json,387244560:tokens/zachary_token.json"
        result = _parse_google_token_map(raw)
        assert result == {
            "701122767": "tokens/jerry_token.json",
            "387244560": "tokens/zachary_token.json",
        }

    def test_strips_whitespace(self):
        raw = " 701122767 : tokens/jerry.json , 387244560 : tokens/zac.json "
        result = _parse_google_token_map(raw)
        assert result == {
            "701122767": "tokens/jerry.json",
            "387244560": "tokens/zac.json",
        }

    def test_trailing_comma_ignored(self):
        raw = "701122767:tokens/jerry.json,"
        result = _parse_google_token_map(raw)
        assert result == {"701122767": "tokens/jerry.json"}

    def test_malformed_no_colon_raises(self):
        with pytest.raises(ValueError, match=GOOGLE_TOKEN_MAP_ENV):
            _parse_google_token_map("701122767tokens/jerry.json")

    def test_malformed_empty_id_raises(self):
        with pytest.raises(ValueError, match=GOOGLE_TOKEN_MAP_ENV):
            _parse_google_token_map(":tokens/jerry.json")

    def test_malformed_empty_path_raises(self):
        with pytest.raises(ValueError, match=GOOGLE_TOKEN_MAP_ENV):
            _parse_google_token_map("701122767:")


class TestGetTokenPathForUser:
    """Tests for get_token_path_for_user."""

    def test_none_user_returns_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(GOOGLE_TOKEN_MAP_ENV, None)
            os.environ.pop("GOOGLE_TOKEN_PATH", None)
            assert get_token_path_for_user(None) == "token.json"

    def test_user_not_in_map_returns_default(self):
        env = {GOOGLE_TOKEN_MAP_ENV: "999:tokens/other.json"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("GOOGLE_TOKEN_PATH", None)
            assert get_token_path_for_user(701122767) == "token.json"

    def test_user_in_map_returns_mapped_path(self):
        env = {GOOGLE_TOKEN_MAP_ENV: "701122767:tokens/jerry.json"}
        with patch.dict(os.environ, env, clear=False):
            assert get_token_path_for_user(701122767) == "tokens/jerry.json"

    def test_fallback_to_google_token_path_env(self):
        env = {"GOOGLE_TOKEN_PATH": "custom/path.json"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop(GOOGLE_TOKEN_MAP_ENV, None)
            assert get_token_path_for_user(701122767) == "custom/path.json"

    def test_map_takes_priority_over_google_token_path(self):
        env = {
            GOOGLE_TOKEN_MAP_ENV: "701122767:tokens/jerry.json",
            "GOOGLE_TOKEN_PATH": "custom/path.json",
        }
        with patch.dict(os.environ, env, clear=False):
            assert get_token_path_for_user(701122767) == "tokens/jerry.json"


class TestIsCalendarConfiguredForUser:
    """Tests for is_calendar_configured_for_user."""

    def test_returns_false_when_calendar_disabled(self, tmp_path):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}")
        env = {
            GOOGLE_TOKEN_MAP_ENV: f"701122767:{token_file}",
            "GOOGLE_CALENDAR_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=False):
            assert is_calendar_configured_for_user(701122767) is False

    def test_returns_false_when_token_missing(self):
        env = {
            GOOGLE_TOKEN_MAP_ENV: "701122767:nonexistent/path.json",
            "GOOGLE_CALENDAR_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            assert is_calendar_configured_for_user(701122767) is False

    def test_returns_true_when_enabled_and_token_exists(self, tmp_path):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}")
        env = {
            GOOGLE_TOKEN_MAP_ENV: f"701122767:{token_file}",
            "GOOGLE_CALENDAR_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            assert is_calendar_configured_for_user(701122767) is True

    def test_user_without_token_returns_false_while_other_user_has_token(self, tmp_path):
        token_file = tmp_path / "jerry.json"
        token_file.write_text("{}")
        env = {
            GOOGLE_TOKEN_MAP_ENV: f"701122767:{token_file}",
            "GOOGLE_CALENDAR_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            # Jerry has a token
            assert is_calendar_configured_for_user(701122767) is True
            # Zachary does not
            assert is_calendar_configured_for_user(387244560) is False
