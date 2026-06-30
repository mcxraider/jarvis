from agents.agent_api.app.tools.todoist.client import (
    TodoistApiClient,
    todoist_api_key_for_telegram_user,
)


def test_uses_mapped_todoist_token_for_telegram_user(monkeypatch):
    monkeypatch.setenv(
        "TODOIST_API_KEYS_BY_TELEGRAM_USER_ID",
        "111:token-one,222:token-two",
    )
    monkeypatch.setenv("TODOIST_API_KEY", "fallback-token")

    assert todoist_api_key_for_telegram_user(111) == "token-one"
    assert TodoistApiClient(telegram_user_id=222).api_key == "token-two"


def test_missing_mapped_telegram_user_does_not_fall_back_when_map_is_configured(monkeypatch):
    monkeypatch.setenv("TODOIST_API_KEYS_BY_TELEGRAM_USER_ID", "111:token-one")
    monkeypatch.setenv("TODOIST_API_KEY", "fallback-token")

    assert todoist_api_key_for_telegram_user(222) is None
    assert TodoistApiClient(telegram_user_id=222).api_key is None


def test_uses_single_user_fallback_when_no_mapping_is_configured(monkeypatch):
    monkeypatch.delenv("TODOIST_API_KEYS_BY_TELEGRAM_USER_ID", raising=False)
    monkeypatch.setenv("TODOIST_API_KEY", "fallback-token")

    assert todoist_api_key_for_telegram_user(111) == "fallback-token"
    assert TodoistApiClient(telegram_user_id=111).api_key == "fallback-token"

