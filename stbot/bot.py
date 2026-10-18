"""Telegram menus, article navigation, and the three daily news digests."""

import logging
import time
from datetime import datetime
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from telebot import TeleBot, apihelper, types

from stbot.config import DIGEST_PAGES, LEGACY_SECTIONS

logger = logging.getLogger(__name__)
GREETINGS = {
    "b": "Good morning! Here's your morning newsfeed to go along with your hearty breakfast :) ",
    "l": "It's lunch time! Here's some afternoon news to go with your cai png :) ",
    "d": "Good evening! Let's wrap up the day with an evening newsfeed :) ",
}


def display_time(value):
    if not value:
        return "not yet available"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Singapore"))
        return parsed.astimezone(ZoneInfo("Asia/Singapore")).strftime("%d %b %Y, %I.%M %p")
    except (ValueError, AttributeError):
        return "not yet available"


def escaped_chunks(text, limit):
    """Split before escaping so a long summary never cuts an HTML entity in half."""
    chunk = []
    size = 0
    for character in text:
        encoded = escape(character)
        # Telegram uses UTF-16 offsets; count supplementary characters conservatively.
        length = len(encoded.encode("utf-16-le")) // 2
        if chunk and size + length > limit:
            yield "".join(chunk)
            chunk, size = [], 0
        chunk.append(encoded)
        size += length
    if chunk:
        yield "".join(chunk)


def button(label, callback):
    return types.InlineKeyboardButton(label, callback_data=callback)


def navigation(state=None):
    keyboard = types.InlineKeyboardMarkup()
    if state:
        keyboard.row(button("Back to " + state.capitalize(), state), button("Main Menu", "menu"))
    else:
        keyboard.row(button("Main Menu", "menu"))
    return keyboard


class NewsBot:
    def __init__(self, repository, settings, bot=None):
        self.repository = repository
        self.settings = settings
        # Process the update before acknowledging its webhook request.
        self.bot = bot if bot is not None else TeleBot(settings.bot_token, threaded=False)
        self.bot.register_message_handler(self.welcome, commands=["start", "menu"])
        self.bot.register_message_handler(
            self.summary, commands=[str(index) for index in range(1, 11)]
        )
        self.bot.register_message_handler(self.updates, commands=["update"])
        self.bot.register_message_handler(self.user_count, commands=["seecurrentusers"])
        self.bot.register_callback_query_handler(self.callback, func=lambda call: True)

    def send(self, chat_id, text, keyboard=None):
        return self.bot.send_message(
            chat_id, text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True
        )

    def menu_keyboard(self, enabled):
        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(button("🇸🇬 Singapore", "singapore"))
        keyboard.row(button("🌐 World", "world"), button("⛩ Asia", "asia"))
        keyboard.row(button("✨ Life", "life"), button("💼 Business", "business"))
        keyboard.row(button("💬 Opinion", "opinion"), button("🏅 Sport", "sport"))
        label = "🔕 Turn Notif Off" if enabled else "🔔 Turn Notif On"
        keyboard.row(button(label, "notification"))
        return keyboard

    def welcome(self, message):
        self.show_menu(message.chat.id, message.from_user)

    def show_menu(self, chat_id, user):
        profile = self.repository.register_user(chat_id, user)
        refreshed = display_time(self.repository.last_refresh())
        self.send(
            chat_id,
            "Welcome to ST Chat5pt! This bot is powered by OpenAI's ChatGPT to bring you a 6 point "
            "summary of the latest headline from The Straits Times!\n\n"
            f"<b>Powered by {escape(self.settings.openai_model)}.</b>\n\n"
            "Latest updates of the bot: /update\n\n"
            "Please select the section of the latest news you want to read\n\n"
            f"<i>Articles last refreshed @ {refreshed}</i>",
            self.menu_keyboard(profile.get("notification_preference", True)),
        )

    def callback(self, call):
        chat_id = call.message.chat.id
        action = call.data
        if action == "menu":
            self.show_menu(chat_id, call.from_user)
        elif action == "notification":
            profile = self.repository.register_user(chat_id, call.from_user)
            enabled = not profile.get("notification_preference", True)
            self.repository.set_notifications(chat_id, enabled)
            self.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=self.menu_keyboard(enabled),
            )
            self.bot.answer_callback_query(
                call.id, text="Notification is turned " + ("on" if enabled else "off")
            )
            return
        elif action in LEGACY_SECTIONS or action == "top stories":
            self.repository.register_user(chat_id, call.from_user)
            self.show_section(chat_id, action)
        elif action in ("page1", "page2", "page3"):
            self.repository.register_user(chat_id, call.from_user)
            view = self.repository.get_view(chat_id)
            self.show_digest(
                chat_id, int(action[-1]), view.get("greeting", ""), call.message.message_id
            )
        else:
            self.bot.answer_callback_query(call.id, text="Please open the menu with /start.")
            return
        self.bot.answer_callback_query(call.id)

    @staticmethod
    def headline(index, article):
        premium = " [PREMIUM]" if article.get("premium") else ""
        return f"/{index}. {escape(article['title'][:180])}{premium}\n"

    def show_section(self, chat_id, section):
        articles = self.repository.latest_articles(section)
        self.repository.save_view(chat_id, section, [(section, article) for article in articles])
        headlines = "".join(
            self.headline(index, article) for index, article in enumerate(articles, 1)
        )
        if not headlines:
            headlines = "Articles are being prepared. Please try again after the next refresh.\n"
        self.send(
            chat_id,
            f"<b>Latest headlines from {escape(section.capitalize())}:</b>\n"
            "<i>⬇️<b>TAP on the number</b> to read summary!</i>\n" + headlines,
            navigation(),
        )

    def show_digest(self, chat_id, page=1, greeting="", message_id=None):
        selections = []
        lines = [f"<b>{escape(greeting)}</b>\n" if greeting else "", "<b>Latest Newsfeed:</b>\n"]
        for section in DIGEST_PAGES[page - 1]:
            lines.append(f"<u>{section.capitalize()}:</u>\n")
            for article in self.repository.feed_articles(section):
                selections.append(("notification feed", article))
                lines.append(self.headline(len(selections), article))
        if not selections:
            lines.append("Articles are being prepared. Please check back after the next refresh.\n")
        self.repository.save_view(chat_id, f"page{page}", selections, greeting)
        lines.append(f"\n<i>{page} of 3</i>")
        keyboard = types.InlineKeyboardMarkup()
        pages = []
        if page > 1:
            pages.append(button("Previous", f"page{page - 1}"))
        if page < 3:
            pages.append(button("Next", f"page{page + 1}"))
        keyboard.row(*pages)
        keyboard.row(button("Main Menu", "menu"))
        text = "".join(lines)
        if message_id is None:
            self.send(chat_id, text, keyboard)
        else:
            self.bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

    def summary(self, message):
        chat_id = message.chat.id
        view = self.repository.get_view(chat_id)
        index = int(message.text.split("@", 1)[0].lstrip("/")) - 1
        items = view.get("items", [])
        if not 0 <= index < len(items):
            self.send(
                chat_id, "Please select a headline from the latest menu. Type /start to begin."
            )
            return
        article = self.repository.find_article(items[index])
        if not article:
            self.send(
                chat_id,
                "This article is no longer available. Please select another headline with /start.",
            )
            return
        title = escape(article["title"][:240])
        updated = display_time(article.get("updated_datetime"))
        header = f"<b>{title}</b>\n<i>Updated as at {updated}</i>\n\nSummary:\n"
        link = article.get("original_link", "")
        footer = (
            f"\n<a href='{escape(link, quote=True)}'>Link to article</a>"
            if urlsplit(link).scheme in ("http", "https")
            else ""
        )
        budget = 4000 - len(header.encode("utf-16-le")) // 2 - len(footer.encode("utf-16-le")) // 2
        response = (
            article.get("response")
            or "The summary is temporarily unavailable. Please try again later."
        )
        for chunk in escaped_chunks(response, max(100, budget)):
            self.send(chat_id, header + chunk + footer, navigation(view.get("state")))

    def send_digest(self, period):
        sent = failed = 0
        for chat_id in self.repository.subscribed_chats():
            try:
                self.show_digest(chat_id, greeting=GREETINGS[period])
                sent += 1
            except apihelper.ApiTelegramException as error:
                if error.error_code == 429:
                    time.sleep(error.result_json.get("parameters", {}).get("retry_after", 1))
                    try:
                        self.show_digest(chat_id, greeting=GREETINGS[period])
                        sent += 1
                    except Exception as retry_error:
                        logger.warning("Digest retry failed (%s)", type(retry_error).__name__)
                        failed += 1
                else:
                    logger.warning("Digest rejected (Telegram code %s)", error.error_code)
                    failed += 1
            except Exception as error:
                logger.warning("Digest delivery failed (%s)", type(error).__name__)
                failed += 1
            time.sleep(0.1)
        logger.info("Digest %s finished: %d sent, %d failed", period, sent, failed)

    def updates(self, message):
        self.send(
            message.chat.id,
            "UPDATE: Six-point news summaries.\n\n"
            "If you encounter an error, use /start to reopen the menu.",
            navigation(),
        )

    def user_count(self, message):
        if message.from_user.id in self.settings.admin_user_ids:
            self.send(
                message.chat.id, f"No. users count: {self.repository.users.count_documents({})}"
            )
        else:
            self.send(message.chat.id, "This command is available to the bot administrator.")
