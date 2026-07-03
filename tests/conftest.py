"""Shared pytest configuration for the Jarvis test suite.

Native LangSmith tracing is governed by the LANGSMITH_TRACING env var. A local
``.env`` may enable it for real runs, but the test suite must never emit
telemetry or open production checkpoint connections. Disable tracing and force
in-memory checkpointing for the whole session. This mirrors how per-run file
logs are auto-disabled under pytest.
"""

import os
import tempfile

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["JARVIS_CHECKPOINT_BACKEND"] = "memory"
os.environ["JARVIS_POSTGRES_DSN"] = ""
os.environ["DATABASE_URL"] = ""

# Google Calendar registers only when a local token file exists
# (``is_calendar_configured``). Pin GOOGLE_TOKEN_PATH to a guaranteed-absent
# path so a developer's real ``token.json`` in the repo root can't leak into
# full-graph tests and register calendar tools non-deterministically. Tests that
# exercise the calendar path monkeypatch ``is_calendar_configured`` directly,
# which overrides this default.
os.environ["GOOGLE_TOKEN_PATH"] = os.path.join(
    tempfile.gettempdir(), "jarvis-tests-no-such-dir", "absent-token.json"
)
