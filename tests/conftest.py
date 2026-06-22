"""Shared pytest configuration for the Jarvis test suite.

Native LangSmith tracing is governed by the LANGSMITH_TRACING env var. A local
``.env`` may enable it for real runs, but the test suite must never emit
telemetry or make network calls to LangSmith, so disable tracing for the whole
session. This mirrors how per-run file logs are auto-disabled under pytest.
"""

import os

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
