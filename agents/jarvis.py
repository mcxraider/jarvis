"""Compatibility facade for the modular Jarvis agent API package.

New code should import from `agents.agent_api.app.*`. This module intentionally
keeps the historical CLI and test import surface available.
"""

import sys

from agents.agent_api.app.service import *  # noqa: F401,F403
from agents.agent_api.app.service import main


if __name__ == "__main__":
    sys.exit(main())
