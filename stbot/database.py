"""MongoDB access, using the collection names of the original deployment."""

import math
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import certifi
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from stbot.config import LEGACY_SECTIONS


def create_client(settings):
    return MongoClient(
        settings.mongodb_uri,
        tls=True,
        tlsCAFile=certifi.where(),
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        socketTimeoutMS=20000,
    )


class Repository:
    def __init__(self, client, settings):
        self.articles = client[settings.article_database]
        self.users = client[settings.user_database]["users"]

    def ensure_indexes(self):
        # Nonunique indexes also work with historical databases containing duplicates.
        self.users.create_index("chat_id")
        for section in LEGACY_SECTIONS:
            self.articles[section].create_index("published_datetime")
            self.articles[section].create_index("original_link")
        self.articles["notification feed"].create_index("current_latest")

    def register_user(self, chat_id, user):
        return self.users.find_one_and_update(
            {"chat_id": chat_id},
            {
                "$set": {
                    "user_id": user.id,
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                },
                "$setOnInsert": {"notification_preference": True},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    def set_notifications(self, chat_id, enabled):
        # Apply the choice to every legacy record for this chat, not just the first.
        self.users.update_many({"chat_id": chat_id}, {"$set": {"notification_preference": enabled}})

    def save_view(self, chat_id, state, articles, greeting=""):
        items = [
            {
                "collection": collection,
                "original_link": article.get("original_link"),
                "title": article["title"],
            }
            for collection, article in articles
        ]
        self.users.update_one(
            {"chat_id": chat_id},
            {"$set": {"view": {"state": state, "items": items, "greeting": greeting}}},
        )

    def get_view(self, chat_id):
        return (self.users.find_one({"chat_id": chat_id}) or {}).get("view", {})

    def latest_articles(self, section, limit=9):
        return list(self.articles[section].find().sort("published_datetime", -1).limit(limit))

    def feed_articles(self, section):
        return list(
            self.articles["notification feed"]
            .find({"section": section})
            .sort("published_datetime", -1)
            .limit(3)
        )

    def find_article(self, selection):
        query = (
            {"original_link": selection["original_link"]}
            if selection.get("original_link")
            else {"title": selection["title"]}
        )
        return self.articles[selection["collection"]].find_one(query)

    def last_refresh(self):
        return (self.articles["Polling"].find_one({"polling_data": "marker"}) or {}).get("poll_end")

    def subscribed_chats(self):
        return self.users.distinct("chat_id", {"notification_preference": True})

    def save_article(self, section, article):
        collection = self.articles[section]
        query = {"original_link": article["original_link"]}
        try:
            collection.update_one(query, {"$set": article}, upsert=True)
        except OperationFailure as error:
            # Preserve the original small-Atlas-plan retention policy, then really retry.
            if "space quota" not in str(error).lower():
                raise
            for name in LEGACY_SECTIONS:
                self.prune_oldest(name)
            collection.update_one(query, {"$set": article}, upsert=True)

    def prune_oldest(self, section, fraction=0.2):
        collection = self.articles[section]
        count = math.floor(collection.count_documents({}) * fraction)
        if count:
            ids = [
                item["_id"] for item in collection.find({}, {"_id": 1}).sort("_id", 1).limit(count)
            ]
            collection.delete_many({"_id": {"$in": ids}})

    def refresh_feed(self):
        feed = self.articles["notification feed"]
        for section in LEGACY_SECTIONS:
            markers = []
            for index, article in enumerate(self.latest_articles(section, 3), 1):
                marker = f"{section}_latest_{index}"
                markers.append(marker)
                values = {key: value for key, value in article.items() if key != "_id"}
                values.update(section=section, current_latest=marker)
                feed.update_one({"current_latest": marker}, {"$set": values}, upsert=True)
            feed.delete_many({"section": section, "current_latest": {"$nin": markers}})

    @contextmanager
    def refresh_lock(self):
        """Prevent overlapping hourly scraper runs across one-off Heroku dynos."""
        locks = self.articles["job_locks"]
        now = datetime.now(UTC)
        owner = uuid4().hex
        try:
            locks.find_one_and_update(
                {"_id": "article_refresh", "expires_at": {"$lte": now}},
                {"$set": {"owner": owner, "expires_at": now + timedelta(hours=1)}},
                upsert=True,
            )
        except DuplicateKeyError:
            yield False
            return
        try:
            yield True
        finally:
            locks.delete_one({"_id": "article_refresh", "owner": owner})
