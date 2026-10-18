import importlib
from dataclasses import replace
from unittest.mock import Mock, patch

from conftest import telegram_message
from telebot import apihelper, types

from stbot.bot import NewsBot, display_time, escaped_chunks
from stbot.web import build_scheduler, create_app


def dispatch(bot, message):
    bot.process_new_updates([types.Update.de_json({"update_id": 1, "message": message})])


def callback(bot, action, *, message=None):
    payload = {
        "update_id": 2,
        "callback_query": {
            "id": "callback",
            "chat_instance": "instance",
            "data": action,
            "from": {"id": 7, "is_bot": False, "first_name": "Reader", "username": "reader"},
            "message": message or telegram_message(),
        },
    }
    bot.process_new_updates([types.Update.de_json(payload)])


def test_start_works_before_first_scrape_and_enables_notifications(repository, settings, telegram):
    NewsBot(repository, settings, telegram)
    dispatch(telegram, telegram_message())
    text = telegram.send_message.call_args.args[1]
    assert "not yet available" in text
    assert "6 point" in text
    assert repository.users.find_one({"chat_id": 70})["notification_preference"] is True


def test_section_selection_and_summary_survive_new_bot_instance(
    repository, settings, telegram, article
):
    repository.save_article("singapore", article)
    news = NewsBot(repository, settings, telegram)
    dispatch(telegram, telegram_message())
    callback(telegram, "singapore")
    assert "/1. A &amp; B &lt;news&gt;" in telegram.send_message.call_args.args[1]
    new_bot = type(telegram)(settings.bot_token, threaded=False)
    new_bot.send_message = Mock()
    NewsBot(repository, settings, new_bot)
    dispatch(new_bot, telegram_message("/1@unit_test_bot"))
    assert article["response"] in new_bot.send_message.call_args.args[1]
    assert "Link to article" in new_bot.send_message.call_args.args[1]
    assert news.repository.get_view(70)["state"] == "singapore"


def test_out_of_range_or_expired_selection_recovers(repository, settings, telegram):
    NewsBot(repository, settings, telegram)
    dispatch(telegram, telegram_message("/10"))
    assert "/start" in telegram.send_message.call_args.args[1]


def test_callback_menu_uses_reader_identity_and_keeps_preference(repository, settings, telegram):
    NewsBot(repository, settings, telegram)
    dispatch(telegram, telegram_message())
    callback(telegram, "notification")
    assert repository.users.find_one({"chat_id": 70})["notification_preference"] is False
    bot_message = telegram_message(user_id=999, username="some_bot")
    callback(telegram, "menu", message=bot_message)
    profile = repository.users.find_one({"chat_id": 70})
    assert profile["user_id"] == 7
    assert profile["notification_preference"] is False
    callback(telegram, "notification")
    assert repository.users.find_one({"chat_id": 70})["notification_preference"] is True


def test_digest_pages_and_summary_use_matching_cached_articles(
    repository, settings, telegram, article
):
    repository.save_article("sport", article)
    repository.refresh_feed()
    news = NewsBot(repository, settings, telegram)
    dispatch(telegram, telegram_message())
    news.show_digest(70, greeting="Lunch")
    callback(telegram, "page3")
    assert "/1. A &amp; B" in telegram.edit_message_text.call_args.args[0]
    dispatch(telegram, telegram_message("/1"))
    assert article["response"] in telegram.send_message.call_args.args[1]


def test_digest_uses_current_preferences_and_deduplicates_chat_ids(repository, settings, telegram):
    repository.users.insert_many(
        [
            {"chat_id": 1, "notification_preference": True},
            {"chat_id": 1, "notification_preference": True},
            {"chat_id": 2, "notification_preference": False},
        ]
    )
    news = NewsBot(repository, settings, telegram)
    news.show_digest = Mock()
    with patch("stbot.bot.time.sleep"):
        news.send_digest("b")
    assert news.show_digest.call_args.args[0] == 1
    assert news.show_digest.call_count == 1
    repository.set_notifications(1, False)
    news.show_digest.reset_mock()
    with patch("stbot.bot.time.sleep"):
        news.send_digest("b")
    news.show_digest.assert_not_called()


def test_failed_digest_does_not_delete_subscribers(repository, settings, telegram):
    repository.users.insert_one({"chat_id": 1, "notification_preference": True})
    news = NewsBot(repository, settings, telegram)
    news.show_digest = Mock(
        side_effect=apihelper.ApiTelegramException(
            "sendMessage", Mock(), {"error_code": 403, "description": "blocked"}
        )
    )
    with patch("stbot.bot.time.sleep"):
        news.send_digest("b")
    assert repository.users.count_documents({}) == 1


def test_count_is_admin_only(repository, settings, telegram):
    NewsBot(repository, replace(settings, admin_user_ids=frozenset({7})), telegram)
    dispatch(telegram, telegram_message("/seecurrentusers", user_id=8))
    assert "administrator" in telegram.send_message.call_args.args[1]
    dispatch(telegram, telegram_message("/seecurrentusers", user_id=7))
    assert "No. users count:" in telegram.send_message.call_args.args[1]


def test_schedules_have_original_singapore_times(repository, settings, telegram):
    scheduler = build_scheduler(NewsBot(repository, settings, telegram))
    triggers = {job.id: str(job.trigger) for job in scheduler.get_jobs()}
    assert "hour='8', minute='6'" in triggers["digest_b"]
    assert "hour='12', minute='0'" in triggers["digest_l"]
    assert "hour='19', minute='0'" in triggers["digest_d"]
    assert str(scheduler.timezone) == "Asia/Singapore"
    assert not scheduler.running


def test_webhook_auth_validation_and_actual_command_dispatch(settings, mongo, telegram):
    app = create_app(settings, client=mongo, telegram_bot=telegram, start_scheduler=False)
    client = app.test_client()
    headers = {"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret}
    payload = {"update_id": 1, "message": telegram_message()}
    assert client.post("/telegram/webhook", json=payload).status_code == 403
    assert client.post("/telegram/webhook", json={}, headers=headers).status_code == 400
    assert client.post("/telegram/webhook", json=payload, headers=headers).status_code == 200
    assert telegram.send_message.call_count == 1
    assert client.get("/").status_code == 200
    assert client.get("/healthz").status_code == 200
    telegram.set_webhook.assert_not_called()


def test_webhook_returns_retryable_failure_without_leaking_credentials(settings, mongo, telegram):
    app = create_app(settings, client=mongo, telegram_bot=telegram, start_scheduler=False)
    telegram.process_new_updates = Mock(side_effect=RuntimeError("private credential"))
    response = app.test_client().post(
        "/telegram/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret},
    )
    assert response.status_code == 503
    assert b"private credential" not in response.data


def test_runtime_entry_points_are_safe_to_import():
    with (
        patch("stbot.config.Settings.from_env", side_effect=AssertionError("loaded live config")),
        patch("stbot.database.MongoClient", side_effect=AssertionError("connected to database")),
    ):
        import st_chat5pt_bot
        import wrapper_gpt

        importlib.reload(st_chat5pt_bot)
        importlib.reload(wrapper_gpt)


def test_html_chunks_are_bounded_and_time_display_is_explicit():
    chunks = list(escaped_chunks("<&😀" * 2000, 1000))
    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 1000 for chunk in chunks)
    assert all("<" not in chunk for chunk in chunks)
    assert display_time("2026-10-18T00:00:00Z") == "18 Oct 2026, 08.00 AM"
    assert display_time("0") == "not yet available"
