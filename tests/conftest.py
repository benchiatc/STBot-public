from unittest.mock import Mock

import mongomock
import pytest
from telebot import TeleBot

from stbot.config import Settings
from stbot.database import Repository


@pytest.fixture
def settings():
    return Settings(
        mongodb_uri="mongodb://localhost:27017",
        bot_token="0:unit-test",
        webhook_secret="unit-test",
        public_base_url="https://test.example",
    )


@pytest.fixture
def mongo():
    return mongomock.MongoClient(tz_aware=True)


@pytest.fixture
def repository(mongo, settings):
    return Repository(mongo, settings)


@pytest.fixture
def telegram(settings):
    bot = TeleBot(settings.bot_token, threaded=False)
    for method in (
        "send_message",
        "send_photo",
        "edit_message_text",
        "edit_message_reply_markup",
        "answer_callback_query",
        "set_webhook",
    ):
        setattr(bot, method, Mock(return_value=True))
    return bot


def telegram_message(text="/start", *, user_id=7, chat_id=70, username="reader"):
    return {
        "message_id": 1,
        "date": 0,
        "from": {"id": user_id, "is_bot": False, "first_name": "Reader", "username": username},
        "chat": {"id": chat_id, "type": "private", "first_name": "Reader"},
        "text": text,
    }


@pytest.fixture
def article():
    return {
        "title": "A & B <news>",
        "original_link": "https://www.straitstimes.com/singapore/sample",
        "published_datetime": "2026-10-18T08:00:00+08:00",
        "updated_datetime": "2026-10-18T09:00:00+08:00",
        "original_article": "Public news.",
        "response": "1. First point.\n2. Second point.",
        "premium": False,
        "summary_status": "ready",
    }
