"""Stage 3 tests: generate_friend_token.py standalone script."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "generate_friend_token.py"


class TestScriptSyntax:
    """Verify the script is valid Python and importable."""

    def test_script_exists(self):
        assert SCRIPT_PATH.exists()

    def test_script_compiles(self):
        code = SCRIPT_PATH.read_text()
        compile(code, str(SCRIPT_PATH), "exec")


class TestFindCredentials:
    """Test the credential file finder logic."""

    def test_finds_credentials_in_current_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text('{"installed": {}}')

        # Import the module functions
        sys.path.insert(0, str(SCRIPT_PATH.parent))
        try:
            import importlib
            spec = importlib.util.spec_from_file_location("generate_friend_token", SCRIPT_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            result = mod.find_credentials(None)
            assert result == Path("credentials.json")
        finally:
            sys.path.pop(0)

    def test_find_credentials_explicit_path(self, tmp_path):
        creds_file = tmp_path / "my_creds.json"
        creds_file.write_text('{"installed": {}}')

        sys.path.insert(0, str(SCRIPT_PATH.parent))
        try:
            import importlib
            spec = importlib.util.spec_from_file_location("generate_friend_token_2", SCRIPT_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            result = mod.find_credentials(str(creds_file))
            assert result == Path(str(creds_file))
        finally:
            sys.path.pop(0)

    def test_exits_when_no_credentials_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRETS_JSON", raising=False)

        sys.path.insert(0, str(SCRIPT_PATH.parent))
        try:
            import importlib
            spec = importlib.util.spec_from_file_location("generate_friend_token_3", SCRIPT_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            with pytest.raises(SystemExit) as exc_info:
                mod.find_credentials(None)
            assert exc_info.value.code == 1
        finally:
            sys.path.pop(0)


class TestCliHelp:
    """Test the script's CLI interface."""

    def test_help_flag(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "Generate a Google Calendar token" in result.stdout
        assert "--credentials" in result.stdout
        assert "--output" in result.stdout
