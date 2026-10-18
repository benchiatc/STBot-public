"""Heroku release task: initialize indexes and register this app's webhook."""

import logging

from telebot import TeleBot

from stbot.config import WEBHOOK_PATH, Settings, configure_logging
from stbot.database import Repository, create_client


def main():
    configure_logging()
    settings = Settings.from_env()
    settings.validate_deployment()
    with create_client(settings) as client:
        client.admin.command("ping")
        Repository(client, settings).ensure_indexes()
    bot = TeleBot(settings.bot_token, threaded=False)
    if not bot.set_webhook(
        url=settings.public_base_url + WEBHOOK_PATH,
        secret_token=settings.webhook_secret,
        allowed_updates=["message", "callback_query"],
        max_connections=4,
    ):
        raise RuntimeError("Telegram did not accept the webhook configuration.")
    logging.getLogger(__name__).info("Database indexes and Telegram webhook configured.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logging.getLogger(__name__).error("Deployment setup failed (%s)", type(error).__name__)
        raise SystemExit(1) from None
