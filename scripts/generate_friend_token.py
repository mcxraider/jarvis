"""Generate a Google Calendar token for Jarvis (friend onboarding).

This is a STANDALONE script — no project dependencies needed.
Send this file + credentials.json to a friend. They run it, authorize in
their browser, and send back the generated my_token.json.

Setup (one time):
    pip install google-auth google-auth-oauthlib google-api-python-client

Usage:
    python generate_friend_token.py

    (Optional) specify output filename:
    python generate_friend_token.py --output zachary_token.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


SCOPES = ["https://www.googleapis.com/auth/calendar"]
DEFAULT_CREDENTIALS_FILE = "credentials.json"
DEFAULT_OUTPUT_FILE = "my_token.json"


def find_credentials(path: str | None = None) -> Path:
    """Locate the OAuth client credentials file."""
    candidates = [
        path,
        os.getenv("GOOGLE_OAUTH_CLIENT_SECRETS_JSON"),
        DEFAULT_CREDENTIALS_FILE,
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return Path(candidate)
    print(
        "ERROR: Could not find credentials.json\n\n"
        "Make sure credentials.json is in the same folder as this script,\n"
        "or pass --credentials /path/to/credentials.json\n\n"
        "If you don't have credentials.json, ask Jerry for it.",
        file=sys.stderr,
    )
    sys.exit(1)


def run_oauth_flow(credentials_path: Path) -> object:
    """Run the interactive OAuth flow and return credentials."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "ERROR: Missing dependencies. Run this first:\n\n"
            "    pip install google-auth google-auth-oauthlib google-api-python-client\n",
            file=sys.stderr,
        )
        sys.exit(1)

    creds_text = credentials_path.read_text(encoding="utf-8")
    config = json.loads(creds_text)
    if not isinstance(config, dict) or not ({"installed", "web"} & set(config)):
        print(
            "ERROR: credentials.json doesn't look like a Google OAuth client config.\n"
            "It should have an 'installed' or 'web' key.\n"
            "Download the correct file from Google Cloud Console > APIs & Services > Credentials.",
            file=sys.stderr,
        )
        sys.exit(1)

    flow = InstalledAppFlow.from_client_config(config, SCOPES)
    print("\nOpening your browser for Google Calendar authorization...")
    print("(If it doesn't open automatically, check your terminal for a URL)\n")
    creds = flow.run_local_server(port=0)
    return creds


def save_token(creds: object, output_path: str) -> None:
    """Save the authorized token to a file."""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a Google Calendar token for Jarvis.",
        epilog="After running, send the output file back to Jerry!",
    )
    parser.add_argument(
        "--credentials",
        default=None,
        help=f"Path to OAuth client credentials (default: ./{DEFAULT_CREDENTIALS_FILE})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_FILE,
        help=f"Where to save the token (default: {DEFAULT_OUTPUT_FILE})",
    )
    args = parser.parse_args()

    credentials_path = find_credentials(args.credentials)
    print(f"Using credentials from: {credentials_path}")

    creds = run_oauth_flow(credentials_path)
    save_token(creds, args.output)

    print(f"\nDone! Token saved to: {args.output}")
    print(f"\nNext step: Send '{args.output}' back to Jerry (Signal, AirDrop, etc.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
