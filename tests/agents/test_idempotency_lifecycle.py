"""Stage 4 tests for background idempotency cleanup."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agents.agent_api.app import main, run_logging


@pytest.fixture(autouse=True)
def isolate_run_log_writer():
    run_logging.reset_log_writer()
    yield
    run_logging.reset_log_writer()


def test_lifespan_runs_cleanup_without_blocking_health():
    cleanup_started = threading.Event()
    store = MagicMock()

    def cleanup():
        cleanup_started.set()
        return 2

    store.cleanup_expired.side_effect = cleanup
    with (
        patch.object(main, "DEFAULT_IDEMPOTENCY_STORE", store),
        patch.object(
            main,
            "settings",
            SimpleNamespace(
                api_title="Test API",
                idempotency_cleanup_interval_seconds=3600,
            ),
        ),
    ):
        app = main.create_app()
        with TestClient(app) as client:
            response = client.get("/health")
            assert cleanup_started.wait(timeout=1)

    assert response.status_code == 200
    store.cleanup_expired.assert_called()


def test_cleanup_failure_does_not_prevent_startup():
    store = MagicMock()
    store.cleanup_expired.side_effect = RuntimeError("database unavailable")

    with (
        patch.object(main, "DEFAULT_IDEMPOTENCY_STORE", store),
        patch.object(
            main,
            "settings",
            SimpleNamespace(
                api_title="Test API",
                idempotency_cleanup_interval_seconds=3600,
            ),
        ),
    ):
        app = main.create_app()
        with TestClient(app) as client:
            response = client.get("/health")

    assert response.status_code == 200
