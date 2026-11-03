import pytest
from agents.agent_api.app.pool_config import pool_sizes


def test_independent_production_pool_budgets(monkeypatch):
    monkeypatch.setenv("JARVIS_DB_SYNC_MIN", "1")
    monkeypatch.setenv("JARVIS_DB_SYNC_MAX", "4")
    assert pool_sizes("SYNC", 2, 16) == {"min_size": 1, "max_size": 4}
    assert pool_sizes("ASYNC", 2, 10) == {"min_size": 2, "max_size": 10}


@pytest.mark.parametrize("low,high", [("3", "2"), ("-1", "4"), ("0", "0")])
def test_invalid_pool_budgets_fail_fast(monkeypatch, low, high):
    monkeypatch.setenv("JARVIS_DB_SYNC_MIN", low)
    monkeypatch.setenv("JARVIS_DB_SYNC_MAX", high)
    with pytest.raises(ValueError):
        pool_sizes("SYNC", 2, 16)
