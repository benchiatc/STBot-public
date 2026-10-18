"""Environment configuration shared by the web process and scheduled scraper."""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SECTIONS = ("singapore", "asia", "world", "opinion", "life", "business", "sport")
# Keep tech for callbacks and feed records created by older deployments.
LEGACY_SECTIONS = (*SECTIONS, "tech")
DIGEST_PAGES = (
    ("singapore", "asia", "world"),
    ("opinion", "life", "business"),
    ("tech", "sport"),
)
WEBHOOK_PATH = "/telegram/webhook"


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str = field(repr=False)
    bot_token: str = field(default="", repr=False)
    openai_api_key: str = field(default="", repr=False)
    webhook_secret: str = field(default="", repr=False)
    public_base_url: str = ""
    article_database: str = "st_articles"
    user_database: str = "user_base"
    openai_model: str = "gpt-5-mini"
    notifications_enabled: bool = True
    admin_user_ids: frozenset[int] = frozenset()

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=False)
        uri = os.getenv("MONGODB_URI", "").strip()
        if not uri:
            raise ValueError("MONGODB_URI is required.")
        try:
            admins = frozenset(
                int(value.strip())
                for value in os.getenv("ADMIN_USER_IDS", "").split(",")
                if value.strip()
            )
        except ValueError:
            raise ValueError(
                "ADMIN_USER_IDS must be comma-separated numeric Telegram IDs."
            ) from None
        enabled = os.getenv("NOTIFICATIONS_ENABLED", "true").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("NOTIFICATIONS_ENABLED must be true or false.")
        return cls(
            mongodb_uri=uri,
            bot_token=os.getenv("BOT_TOKEN", "").strip(),
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            article_database=os.getenv("ARTICLE_DATABASE", "st_articles"),
            user_database=os.getenv("USER_DATABASE", "user_base"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
            notifications_enabled=enabled == "true",
            admin_user_ids=admins,
        )

    def validate_web(self):
        if not self.bot_token:
            raise ValueError("BOT_TOKEN is required for the Telegram server.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", self.webhook_secret):
            raise ValueError(
                "WEBHOOK_SECRET must contain 1–256 letters, digits, underscores or hyphens."
            )

    def validate_deployment(self):
        self.validate_web()
        url = urlsplit(self.public_base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.path not in ("", "/")
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("PUBLIC_BASE_URL must be the HTTPS origin of this app.")


def configure_logging():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # SDK request URLs can include the Telegram token. Application logs use error types only.
    for name in ("urllib3", "httpx", "httpcore", "openai", "TeleBot"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
