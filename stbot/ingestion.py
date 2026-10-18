"""Fetch public articles, summarize them, and refresh the cached Telegram feed."""

import json
import logging
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from openai import OpenAI, OpenAIError

from stbot.config import SECTIONS, Settings, configure_logging
from stbot.database import Repository, create_client

logger = logging.getLogger(__name__)
BASE_URL = "https://www.straitstimes.com"
PREMIUM_NOTICE = (
    "This is a Premium Article. ChatGPT cannot summarise premium articles or any contents "
    "behind a paywall. Please visit the article using the original link below if you have a subscription."
)
SUMMARY_ERROR = (
    "The summary is temporarily unavailable. You can read the original article using the link below. "
    "The bot will try again on the next refresh."
)


def timestamp(value):
    """Normalize source timestamps for consistent sorting and Singapore-time display."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")


def discover_links(html):
    soup = BeautifulSoup(html, "html.parser")
    links = {section: [] for section in SECTIONS}
    for box in soup.select('div.container[data-testid="popular-story-test-id"]'):
        header = box.select_one('[data-testid="content-block-header"] a[href]')
        section = urlsplit(header["href"]).path.strip("/").split("/")[0] if header else ""
        if section not in links and header:
            label = header.get_text(" ", strip=True).lower()
            section = next((name for name in SECTIONS if label.startswith(name)), "")
        if section not in links:
            continue
        for anchor in box.select('a[data-testid="popular-baseline-card-test-id"][href]'):
            url = urljoin(BASE_URL, anchor["href"])
            if urlsplit(url).netloc == urlsplit(BASE_URL).netloc and url not in links[section]:
                links[section].append(url)
    return {section: urls for section, urls in links.items() if urls}


def article_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from article_objects(item)
    elif isinstance(value, dict):
        kinds = value.get("@type", [])
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if any(kind in ("Article", "NewsArticle") for kind in kinds):
            yield value
        yield from article_objects(value.get("@graph", []))


def parse_article(html, url):
    soup = BeautifulSoup(html, "html.parser")
    metadata = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            for article in article_objects(json.loads(script.string or script.get_text())):
                metadata.update({key: value for key, value in article.items() if value is not None})
        except (json.JSONDecodeError, TypeError):
            continue

    def meta(name, attribute="property"):
        tag = soup.find("meta", attrs={attribute: name})
        return tag.get("content", "") if tag else ""

    accessible = metadata.get("isAccessibleForFree", True)
    if isinstance(accessible, str):
        accessible = accessible.strip().lower() in ("true", "1", "yes")
    image = metadata.get("image") or meta("og:image")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")
    published = timestamp(metadata.get("datePublished") or meta("article:published_time"))
    updated = timestamp(metadata.get("dateModified") or meta("article:modified_time")) or published
    paragraphs = [
        paragraph.get_text(" ", strip=True)
        for paragraph in soup.select('p[data-testid="article-paragraph-annotation-test-id"]')
    ]
    return {
        "title": metadata.get("headline")
        or metadata.get("name")
        or meta("og:title")
        or (soup.title.get_text(strip=True) if soup.title else ""),
        "original_link": url,
        "published_datetime": published,
        "updated_datetime": updated,
        "original_article": "\n\n".join(paragraph for paragraph in paragraphs if paragraph),
        "premium": not bool(accessible),
        "thumbnail": image,
    }


def needs_refresh(existing, article):
    return (
        not existing
        or not existing.get("response")
        or existing.get("summary_status") == "failed"
        or not article["updated_datetime"]
        or timestamp(existing.get("updated_datetime")) != article["updated_datetime"]
    )


def summarize(article, client, model):
    if article["premium"]:
        return PREMIUM_NOTICE
    response = client.responses.create(
        model=model,
        instructions="You are a news reporter that specialises in capturing key messages and summarising them into six points.",
        input=(
            "Summarise the following article in 6 bullet points. The summary MUST be presented "
            "in a point by point format. Starting with 1. 2. 3. and so on until 6.\n"
            + article["original_article"]
        ),
    )
    text = response.output_text.strip()
    if not text:
        raise ValueError("The summary response was empty.")
    return text


def refresh_articles(repository, session, openai_client, model):
    started = datetime.now(UTC).isoformat(timespec="seconds")
    response = session.get(BASE_URL + "/breaking-news", timeout=(10, 30))
    response.raise_for_status()
    links = discover_links(response.text)
    if not links:
        raise ValueError("No article links found; the source page layout may have changed.")
    saved = failures = 0
    for section, urls in links.items():
        for url in urls:
            try:
                response = session.get(url, timeout=(10, 30))
                response.raise_for_status()
                article = parse_article(response.text, url)
                if not article["title"] or (
                    not article["premium"] and not article["original_article"]
                ):
                    logger.warning("Skipped article without a title or public body in %s", section)
                    failures += 1
                    continue
                existing = repository.articles[section].find_one({"original_link": url})
                if not needs_refresh(existing, article):
                    continue
                try:
                    article["response"] = summarize(article, openai_client, model)
                    article["summary_status"] = "ready"
                except (OpenAIError, ValueError) as error:
                    logger.warning("Summary unavailable in %s (%s)", section, type(error).__name__)
                    article["response"] = SUMMARY_ERROR
                    article["summary_status"] = "failed"
                    failures += 1
                repository.save_article(section, article)
                saved += 1
            except requests.RequestException as error:
                logger.warning("Article fetch failed in %s (%s)", section, type(error).__name__)
                failures += 1
    repository.refresh_feed()
    repository.articles["Polling"].update_one(
        {"polling_data": "marker"},
        {
            "$set": {
                "poll_start": timestamp(started),
                "poll_end": timestamp(datetime.now(UTC).isoformat()),
            }
        },
        upsert=True,
    )
    logger.info("Article refresh finished: %d saved; %d unavailable", saved, failures)
    return saved, failures


def main():
    configure_logging()
    settings = Settings.from_env()
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for article summaries.")
    with (
        create_client(settings) as mongo,
        requests.Session() as session,
        OpenAI(
            api_key=settings.openai_api_key,
            timeout=90,
            max_retries=2,
        ) as openai_client,
    ):
        mongo.admin.command("ping")
        repository = Repository(mongo, settings)
        repository.ensure_indexes()
        with repository.refresh_lock() as acquired:
            if not acquired:
                logger.info("Another article refresh is running; skipped this invocation.")
                return
            session.headers.update(
                {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
            )
            refresh_articles(repository, session, openai_client, settings.openai_model)


def cli():
    """Run the scheduled refresh without exposing credentials in error output."""
    try:
        main()
    except Exception as error:
        logger.error("Article refresh failed (%s)", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
