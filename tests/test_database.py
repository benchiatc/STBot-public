from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pymongo.errors import OperationFailure


def test_registration_handles_missing_and_changed_usernames(repository):
    first = SimpleNamespace(id=1, username=None, first_name="One", last_name=None)
    second = SimpleNamespace(id=2, username=None, first_name="Two", last_name=None)
    repository.register_user(10, first)
    repository.register_user(20, second)
    repository.set_notifications(10, False)
    first.username = "new_name"
    repository.register_user(10, first)
    assert repository.users.count_documents({}) == 2
    assert repository.users.find_one({"chat_id": 10})["username"] == "new_name"
    assert repository.users.find_one({"chat_id": 10})["notification_preference"] is False


def test_article_upsert_and_feed_rebuild_do_not_duplicate_entries(repository, article):
    repository.ensure_indexes()
    repository.save_article("singapore", article)
    repository.save_article("singapore", dict(article, response="Updated summary"))
    assert repository.articles.singapore.count_documents({}) == 1
    repository.articles["notification feed"].insert_one(
        {
            "current_latest": "singapore_latest_3",
            "section": "singapore",
            "title": "Old entry",
        }
    )
    repository.refresh_feed()
    assert len(repository.feed_articles("singapore")) == 1
    assert repository.feed_articles("singapore")[0]["response"] == "Updated summary"


def test_storage_quota_prunes_and_retries_the_failed_write(repository, article):
    for index in range(10):
        repository.save_article(
            "singapore", dict(article, original_link=f"https://example.com/{index}")
        )
    collection = repository.articles.singapore
    write = Mock(side_effect=[OperationFailure("You are over your space quota"), None])
    with patch.object(collection, "update_one", write):
        repository.save_article("singapore", article)
    assert write.call_count == 2
    assert collection.count_documents({}) == 8


def test_refresh_lock_blocks_overlap_and_recovers_after_expiry(repository):
    with repository.refresh_lock() as first:
        assert first is True
        with repository.refresh_lock() as second:
            assert second is False
    with repository.refresh_lock() as third:
        assert third is True
    repository.articles.job_locks.insert_one(
        {
            "_id": "article_refresh",
            "expires_at": datetime.now(UTC) - timedelta(hours=2),
        }
    )
    with repository.refresh_lock() as recovered:
        assert recovered is True
