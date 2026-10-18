"""Flask application factory and in-process digest scheduler."""

import atexit
import hmac
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request
from telebot import types

from stbot.bot import NewsBot
from stbot.config import WEBHOOK_PATH, Settings, configure_logging
from stbot.database import Repository, create_client

logger = logging.getLogger(__name__)


def build_scheduler(news_bot):
    scheduler = BackgroundScheduler(
        timezone="Asia/Singapore",
        daemon=True,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
    )
    for period, hour, minute in (("b", 8, 6), ("l", 12, 0), ("d", 19, 0)):
        scheduler.add_job(
            news_bot.send_digest,
            "cron",
            args=[period],
            hour=hour,
            minute=minute,
            jitter=10,
            id=f"digest_{period}",
        )
    return scheduler


def create_app(settings=None, *, client=None, telegram_bot=None, start_scheduler=True):
    configure_logging()
    settings = settings if settings is not None else Settings.from_env()
    settings.validate_web()
    client = client if client is not None else create_client(settings)
    client.admin.command("ping")
    news_bot = NewsBot(Repository(client, settings), settings, bot=telegram_bot)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    app.extensions["news_bot"] = news_bot

    @app.get("/")
    @app.get("/healthz")
    def health():
        return jsonify(name="ST Chat5pt", status="ok")

    @app.post(WEBHOOK_PATH)
    def webhook():
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied, settings.webhook_secret):
            return jsonify(error="Forbidden"), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or type(payload.get("update_id")) is not int:
            return jsonify(error="Invalid Telegram update"), 400
        try:
            update = types.Update.de_json(payload)
        except (ValueError, TypeError, KeyError, AttributeError):
            return jsonify(error="Invalid Telegram update"), 400
        try:
            news_bot.bot.process_new_updates([update])
        except Exception as error:
            logger.error("Webhook processing failed (%s)", type(error).__name__)
            return jsonify(error="Unable to process update"), 503
        return "", 200

    scheduler = build_scheduler(news_bot)
    app.extensions["digest_scheduler"] = scheduler
    if start_scheduler and settings.notifications_enabled:
        scheduler.start()

    def shutdown():
        if scheduler.running:
            scheduler.shutdown(wait=False)
        client.close()

    atexit.register(shutdown)
    return app
