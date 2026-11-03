"""
api/analytics.py — OADSON V3 Analytics Router.

User-scoped usage stats, backing chat.html's Analytics modal. Same
Bearer-token auth as every other user-facing router — see api/settings.py's
docstring for why this deliberately does NOT reuse api/engineer.py's
OADSON_ENGINEER_TOKEN gate.

Endpoints:
  GET /analytics/overview     — sessions/messages/files/memories counts,
                                 skills used this week, avg messages/session
  GET /analytics/usage        — top skills by execution count (?days=N)
  GET /analytics/models       — model usage breakdown (?days=N)
  GET /analytics/memory       — memory count + breakdown by category
  GET /analytics/files        — file count + breakdown by type
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends

from dependency import get_current_user, get_db
from api.settings import _compute_stats

logger = logging.getLogger("oadson.api.analytics")

router = APIRouter()


@router.get("/analytics/overview")
async def analytics_overview(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = user["_id"]
    user_id_str = str(user_id)

    stats = await _compute_stats(db, user_id)

    total_memories = await db["memory"].count_documents({"user_id": user_id_str})

    week_ago = datetime.utcnow() - timedelta(days=7)
    skill_names = await db["skill_executions"].distinct(
        "skill", {"user_id": user_id_str, "timestamp": {"$gte": week_ago}}
    )

    avg_messages = (
        round(stats["total_messages"] / stats["total_sessions"], 1)
        if stats["total_sessions"] else 0.0
    )

    return {
        "total_sessions": stats["total_sessions"],
        "total_messages": stats["total_messages"],
        "total_files": stats["total_files"],
        "total_memories": total_memories,
        "skills_used_this_week": len(skill_names),
        "average_messages_per_session": avg_messages,
    }


@router.get("/analytics/usage")
async def analytics_usage(
    days: int = 7,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id_str = str(user["_id"])
    since = datetime.utcnow() - timedelta(days=days)

    pipeline = [
        {"$match": {"user_id": user_id_str, "timestamp": {"$gte": since}}},
        {"$group": {"_id": "$skill", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 15},
    ]
    rows = await db["skill_executions"].aggregate(pipeline).to_list(length=15)
    top_skills = [[row["_id"], row["count"]] for row in rows if row["_id"]]

    return {"top_skills": top_skills, "days": days}


@router.get("/analytics/models")
async def analytics_models(
    days: int = 7,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Reads model_used off each persisted assistant message
    (core/orchestrator.py's _persist_memory — see that file's comment on
    why this field wasn't there before). Messages saved before that fix
    won't have this field and are correctly excluded, not miscounted as
    a fake "unknown" bucket.
    """
    user_id_str = str(user["_id"])
    since = datetime.utcnow() - timedelta(days=days)

    pipeline = [
        {"$match": {"user_id": user_id_str}},
        {"$unwind": "$messages"},
        {"$match": {
            "messages.role": "assistant",
            "messages.time": {"$gte": since},
            "messages.model_used": {"$exists": True},
        }},
        {"$group": {"_id": "$messages.model_used", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    rows = await db["ai_sessions"].aggregate(pipeline).to_list(length=50)
    models = {row["_id"]: row["count"] for row in rows}
    total_requests = sum(models.values())

    return {"models": models, "total_requests": total_requests, "days": days}


@router.get("/analytics/memory")
async def analytics_memory(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id_str = str(user["_id"])

    pipeline = [
        {"$match": {"user_id": user_id_str}},
        {"$group": {"_id": "$category", "count": {"$sum": 1}}},
    ]
    rows = await db["memory"].aggregate(pipeline).to_list(length=50)
    categories = {(row["_id"] or "general"): row["count"] for row in rows}
    total_memories = sum(categories.values())

    return {"total_memories": total_memories, "categories": categories}


@router.get("/analytics/files")
async def analytics_files(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = user["_id"]

    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": "$file_type", "count": {"$sum": 1}}},
    ]
    rows = await db["files"].aggregate(pipeline).to_list(length=50)
    by_type = {(row["_id"] or "unknown"): row["count"] for row in rows}
    total_files = sum(by_type.values())

    return {"total_files": total_files, "by_type": by_type}
