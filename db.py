"""MongoDB storage for scraped products."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path


class StorageError(Exception):
    """A MongoDB setup or write error safe to show in the terminal."""


def connect(stack):
    try:
        from dotenv import dotenv_values
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
    except ImportError as exc:
        raise StorageError("Install the MongoDB dependencies with pip install -r requirements.txt.") from exc

    settings = {**os.environ, **dotenv_values(Path(__file__).with_name(".env"))}
    uri = (settings.get("MONGODB_URI") or "").strip()
    database = (settings.get("MONGODB_DATABASE", "universal_scraper") or "").strip()
    collection = (settings.get("MONGODB_COLLECTION", "products") or "").strip()
    if not uri or not database or not collection:
        raise StorageError("Set MONGODB_URI, MONGODB_DATABASE, and MONGODB_COLLECTION in .env.")

    try:
        client = stack.enter_context(MongoClient(uri, serverSelectionTimeoutMS=5000, timeoutMS=10000))
        client.admin.command("ping")
        return client[database][collection]
    except (PyMongoError, ValueError) as exc:
        raise StorageError(f"MongoDB connection failed ({type(exc).__name__}). Check the URI and database access.") from exc


def save_product(collection, record):
    from pymongo.errors import PyMongoError

    identity = json.dumps([record["product_url"], record.get("p_id") or record.get("name") or record["product_url"]],
                          ensure_ascii=False)
    key = sha256(identity.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    try:
        collection.update_one(
            {"_id": key},
            {"$set": {**record, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except PyMongoError as exc:
        raise StorageError(f"MongoDB product write failed ({type(exc).__name__}).") from exc
