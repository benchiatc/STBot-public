import json
from unittest.mock import Mock

import pytest

from stbot.ingestion import (
    PREMIUM_NOTICE,
    discover_links,
    needs_refresh,
    parse_article,
    refresh_articles,
    summarize,
    timestamp,
)


def listing():
    return """<div class="container" data-testid="popular-story-test-id">
      <div data-testid="content-block-header"><a href="/singapore">Singapore</a></div>
      <a data-testid="popular-baseline-card-test-id" href="/singapore/sample">Story</a>
      <a data-testid="popular-baseline-card-test-id" href="/singapore/sample">Duplicate</a>
      <a data-testid="popular-baseline-card-test-id" href="https://unrelated.example/story">Other</a>
    </div>"""


def story(*, premium=False, body=True, graph=False):
    metadata = {
        "@type": ["Article", "NewsArticle"],
        "headline": "A story",
        "datePublished": "2026-10-18T00:00:00Z",
        "dateModified": "2026-10-18T01:00:00Z",
        "isAccessibleForFree": not premium,
        "image": {"url": "https://example.com/photo.jpg"},
    }
    if graph:
        metadata = {"@graph": [metadata]}
    paragraphs = (
        '<p data-testid="article-paragraph-annotation-test-id">Public text.</p>' if body else ""
    )
    return '<script type="application/ld+json">' + json.dumps(metadata) + "</script>" + paragraphs


def test_discovery_preserves_order_deduplicates_and_rejects_offsite_links():
    assert discover_links(listing()) == {
        "singapore": ["https://www.straitstimes.com/singapore/sample"],
    }


@pytest.mark.parametrize("graph", [False, True])
def test_article_extraction_handles_json_ld_graphs_and_visible_body(graph):
    article = parse_article(story(graph=graph), "https://www.straitstimes.com/singapore/sample")
    assert article["title"] == "A story"
    assert article["original_article"] == "Public text."
    assert article["updated_datetime"] == "2026-10-18T09:00:00+08:00"
    assert article["premium"] is False


def test_meta_fallback_and_missing_dates_do_not_crash():
    html = '<meta property="og:title" content="Fallback"><meta property="article:published_time" content="2026-10-18T00:00:00Z">'
    article = parse_article(html, "https://example.com/story")
    assert article["title"] == "Fallback"
    assert article["updated_datetime"] == article["published_datetime"]
    assert article["original_article"] == ""
    assert timestamp("not a date") == ""


def test_premium_articles_never_call_openai():
    client = Mock()
    article = parse_article(story(premium=True, body=False), "https://example.com/story")
    assert summarize(article, client, "gpt-5-mini") == PREMIUM_NOTICE
    client.responses.create.assert_not_called()


def test_summary_uses_existing_model_prompt_and_sdk_output(article):
    client = Mock()
    client.responses.create.return_value.output_text = "1. A summary"
    assert summarize(article, client, "gpt-5-mini") == "1. A summary"
    assert client.responses.create.call_args.kwargs["model"] == "gpt-5-mini"
    assert "6 bullet points" in client.responses.create.call_args.kwargs["input"]
    client.responses.create.return_value.output_text = " "
    with pytest.raises(ValueError):
        summarize(article, client, "gpt-5-mini")


def test_unchanged_article_is_cached_and_failed_summary_is_retried(article):
    assert needs_refresh(article, article) is False
    assert needs_refresh(dict(article, summary_status="failed"), article) is True
    assert needs_refresh(dict(article, updated_datetime="2026-10-18T01:00:00Z"), article) is False


def test_complete_refresh_populates_new_database_and_skips_second_summary(repository):
    session, client = Mock(), Mock()
    session.get.side_effect = [Mock(text=listing()), Mock(text=story())] * 2
    client.responses.create.return_value.output_text = "1. A summary"
    assert refresh_articles(repository, session, client, "gpt-5-mini") == (1, 0)
    assert len(repository.feed_articles("singapore")) == 1
    assert repository.last_refresh()
    assert refresh_articles(repository, session, client, "gpt-5-mini") == (0, 0)
    assert client.responses.create.call_count == 1


def test_empty_free_article_is_not_sent_to_openai(repository):
    session, client = Mock(), Mock()
    session.get.side_effect = [Mock(text=listing()), Mock(text=story(body=False))]
    assert refresh_articles(repository, session, client, "gpt-5-mini") == (0, 1)
    client.responses.create.assert_not_called()


def test_failed_summary_is_stored_and_retried_on_next_run(repository):
    session, client = Mock(), Mock()
    session.get.side_effect = [Mock(text=listing()), Mock(text=story())] * 2
    client.responses.create.return_value.output_text = ""
    assert refresh_articles(repository, session, client, "gpt-5-mini") == (1, 1)
    assert repository.latest_articles("singapore")[0]["summary_status"] == "failed"
    client.responses.create.return_value.output_text = "1. A summary"
    assert refresh_articles(repository, session, client, "gpt-5-mini") == (1, 0)
    assert repository.latest_articles("singapore")[0]["summary_status"] == "ready"


def test_layout_change_fails_without_claiming_refresh_succeeded(repository):
    session = Mock()
    session.get.return_value.text = "<html>New website</html>"
    with pytest.raises(ValueError, match="No article links"):
        refresh_articles(repository, session, Mock(), "gpt-5-mini")
    assert repository.last_refresh() is None
