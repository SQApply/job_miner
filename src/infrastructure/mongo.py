from __future__ import annotations

from functools import lru_cache
from typing import Any

from pymongo import MongoClient
from pymongo.database import Database
from pymongo.errors import PyMongoError

from .settings import load_app_settings


@lru_cache(maxsize=1)
def get_mongo_client() -> MongoClient:
    settings = load_app_settings().warehouse
    client: MongoClient = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    return client


@lru_cache(maxsize=1)
def get_mongo_database() -> Database:
    settings = load_app_settings().warehouse
    return get_mongo_client()[settings.mongo_db]


def healthcheck() -> dict[str, Any]:
    try:
        db = get_mongo_database()
        db.client.admin.command("ping")
        return {"ok": True, "database": db.name}
    except PyMongoError as exc:
        return {"ok": False, "error": str(exc)}
