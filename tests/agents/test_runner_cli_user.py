"""Unit tests for CLI Telegram-ID resolution in the local runner.

Covers the precedence chain that decides whether a `runner.py` invocation resolves
integrations from Supabase (a real Telegram id) or falls back to keyless offline mode
(`None`): explicit flag > --user-* alias env > JARVIS_CLI_DEFAULT_TELEGRAM_ID > None.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.agent_api.app.runner import (
    CLI_DEFAULT_TELEGRAM_ID_ENV,
    build_arg_parser,
    resolve_cli_telegram_user_id,
)


def _resolve(argv, env=None):
    """Resolve the CLI Telegram id for the given argv under a clean environment."""

    args = build_arg_parser().parse_args(argv)
    with patch.dict(os.environ, env or {}, clear=True):
        return resolve_cli_telegram_user_id(args)


class ResolveCliTelegramUserIdTest(unittest.TestCase):
    def test_explicit_flag_wins(self):
        # Even with a default set, an explicit --telegram-user-id takes precedence.
        result = _resolve(
            ["--telegram-user-id", "701122767", "hi"],
            env={CLI_DEFAULT_TELEGRAM_ID_ENV: "111111111"},
        )
        self.assertEqual(result, 701122767)

    def test_user_alias_resolves_from_env(self):
        result = _resolve(
            ["--user-1", "hi"],
            env={"JARVIS_CLI_USER_1_TELEGRAM_ID": "123456789"},
        )
        self.assertEqual(result, 123456789)

    def test_default_env_used_when_no_flag(self):
        # The fix: a plain run picks up the default identity so it resolves Supabase.
        result = _resolve(["hi"], env={CLI_DEFAULT_TELEGRAM_ID_ENV: "701122767"})
        self.assertEqual(result, 701122767)

    def test_none_when_no_flag_and_no_default(self):
        # No user anywhere -> keyless offline mode (last-resort fallback).
        self.assertIsNone(_resolve(["hi"], env={}))

    def test_malformed_default_env_raises(self):
        with self.assertRaises(ValueError):
            _resolve(["hi"], env={CLI_DEFAULT_TELEGRAM_ID_ENV: "not-a-number"})

    def test_non_positive_default_env_raises(self):
        with self.assertRaises(ValueError):
            _resolve(["hi"], env={CLI_DEFAULT_TELEGRAM_ID_ENV: "0"})

    def test_flag_and_alias_together_raises(self):
        with self.assertRaises(ValueError):
            _resolve(
                ["--telegram-user-id", "701122767", "--user-1", "hi"],
                env={"JARVIS_CLI_USER_1_TELEGRAM_ID": "123456789"},
            )

    def test_two_aliases_together_raises(self):
        with self.assertRaises(ValueError):
            _resolve(["--user-1", "--user-2", "hi"], env={})

    def test_selected_alias_with_unset_env_raises(self):
        # Choosing --user-1 but leaving its env unset is a hard error, not a fallback.
        with self.assertRaises(ValueError):
            _resolve(["--user-1", "hi"], env={})


if __name__ == "__main__":
    unittest.main()
