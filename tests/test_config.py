from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from stbot import setup
from stbot.config import Settings


def test_settings_redact_secret_fields(settings):
    settings = replace(
        settings,
        mongodb_uri="private-db-value",
        bot_token="private-bot-value",
        openai_api_key="private-api-value",
        webhook_secret="private-hook-value",
    )
    assert "private-" not in repr(settings)


def test_environment_config_and_validation(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("ADMIN_USER_IDS", "7, 8")
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "false")
    with patch("stbot.config.load_dotenv"):
        settings = Settings.from_env()
    assert settings.admin_user_ids == frozenset({7, 8})
    assert settings.notifications_enabled is False
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "no")
    with patch("stbot.config.load_dotenv"), pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://example.com/path",
        "https://example.com/?secret=value",
        "https://user:pass@example.com",
    ],
)
def test_deployment_rejects_invalid_webhook_origins(settings, origin):
    with pytest.raises(ValueError):
        replace(settings, public_base_url=origin).validate_deployment()


def test_deployment_setup_registers_authenticated_webhook_without_messaging(settings, mongo):
    bot = Mock()
    with (
        patch.object(Settings, "from_env", return_value=settings),
        patch.object(setup, "create_client", return_value=mongo),
        patch.object(setup, "TeleBot", return_value=bot),
    ):
        setup.main()
    bot.set_webhook.assert_called_once_with(
        url="https://test.example/telegram/webhook",
        secret_token="unit-test",
        allowed_updates=["message", "callback_query"],
        max_connections=4,
    )
    bot.send_message.assert_not_called()
