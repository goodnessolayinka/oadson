"""
memory/manager.py — OADSON V3 Centralized MemoryManager.

One instance. Everything reads/writes memory through here.
No scattered db["memory"] calls across routers.

Memory types:
  - user     : persistent facts about the user (name, projects, preferences)
  - session  : current session context
  - project  : per-project knowledge
  - working  : ephemeral, cleared after session
"""

import logging
from datetime import datetime
from typing import Literal
from bson import ObjectId

logger = logging.getLogger("oadson.memory")

MemoryType = Literal["user", "session", "project", "working"]


def _sanitize(doc: dict) -> dict:
    """Convert ObjectId and datetime to JSON-safe types."""
    result = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            result[k] = str(v)
        elif isinstance(v, datetime):
            result[k] = v.isoformat()
        elif isinstance(v, dict):
            result[k] = _sanitize(v)
        else:
            result[k] = v
    return result


class MemoryManager:

    def __init__(self, db) -> None:
        self.db = db
        self.collection = db["memory"]

    async def store(
        self,
        user_id: str,
        key: str,
        content: str,
        memory_type: MemoryType = "user",
        category: str = "general",
        project: str | None = None,
    ) -> None:
        now = datetime.utcnow()
        doc = {
            "user_id": user_id,
            "key": key,
            "content": content,
            "type": memory_type,
            "category": category,
            "updated_at": now,
        }
        if project:
            doc["project"] = project

        await self.collection.update_one(
            {"user_id": user_id, "key": key},
            {"$set": doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        logger.debug(f"Memory stored: {key} [{memory_type}] for {user_id}")

    async def retrieve(
        self,
        user_id: str,
        query: str = "",
        memory_type: MemoryType | None = None,
        category: str | None = None,
        project: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        filter_: dict = {"user_id": user_id}
        if memory_type:
            filter_["type"] = memory_type
        if category:
            filter_["category"] = category
        if project:
            filter_["project"] = project

        cursor = self.collection.find(
            filter_,
            {"key": 1, "content": 1, "type": 1, "category": 1, "project": 1, "updated_at": 1},
        ).sort("updated_at", -1).limit(limit)

        results = []
        async for doc in cursor:
            results.append(_sanitize(doc))

        return results

    async def get(self, user_id: str, key: str) -> dict | None:
        doc = await self.collection.find_one(
            {"user_id": user_id, "key": key},
        )
        return _sanitize(doc) if doc else None

    async def delete(self, user_id: str, key: str) -> bool:
        result = await self.collection.delete_one({"user_id": user_id, "key": key})
        return result.deleted_count > 0

    async def list_all(self, user_id: str, memory_type: MemoryType | None = None) -> list[dict]:
        filter_: dict = {"user_id": user_id}
        if memory_type:
            filter_["type"] = memory_type

        cursor = self.collection.find(filter_).sort("updated_at", -1)
        return [_sanitize(doc) async for doc in cursor]

    async def clear_working(self, user_id: str) -> None:
        await self.collection.delete_many({"user_id": user_id, "type": "working"})
