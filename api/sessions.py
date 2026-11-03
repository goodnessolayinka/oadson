"""
api/sessions.py — OADSON V3 Sessions Router.

Pagination design (per Sonnet 5 review):

  GET /sessions
    Cursor-based on updated_at DESC.
    ?before=<ISO datetime>&limit=N

  GET /sessions/{id}
    Returns metadata + LATEST N messages using MongoDB $slice projection.
    MongoDB slices the array server-side — the full array is never loaded.
    Returns total_messages count so frontend knows whether older pages exist.
    Returns oldest_offset = (total_messages - messages_returned) for next call.

  GET /sessions/{id}/messages
    Offset-based using ?offset=<int>&limit=N (not timestamp-based).
    offset = index from the START of the messages array (0 = oldest).
    MongoDB $slice skips + limits in one projection — no Python loop.
    Frontend gets total_messages from the first GET /sessions/{id} call
    and uses it to calculate the right offset for each page.

    Fetch older messages (infinite scroll upward):
      First load: GET /sessions/{id}  → latest 30, total=87, oldest_offset=57
      Scroll up:  GET /sessions/{id}/messages?offset=27&limit=30  → msgs 27-56
      Scroll up:  GET /sessions/{id}/messages?offset=0&limit=27   → msgs 0-26
"""

import uuid
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from dependency import get_current_user, get_db

logger = logging.getLogger("oadson.api.sessions")

router = APIRouter()

DEFAULT_SESSION_LIMIT = 20
MAX_SESSION_LIMIT     = 50
DEFAULT_MSG_LIMIT     = 30
MAX_MSG_LIMIT         = 100


class CreateSessionRequest(BaseModel):
    name: Optional[str] = None
    surface: Optional[str] = "web"


class RenameSessionRequest(BaseModel):
    name: str


# ── CREATE ──

@router.post("/sessions")
async def create_session(
    body: CreateSessionRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    session_id = str(uuid.uuid4())
    now = datetime.utcnow()
    await db["ai_sessions"].insert_one({
        "session_id": session_id,
        "user_id": str(user["_id"]),
        "name": body.name or f"Chat {now.strftime('%b %d')}",
        "surface_origin": body.surface or "web",
        "messages": [],
        "created_at": now,
        "updated_at": now,
    })
    return {"session_id": session_id, "created_at": now.isoformat() + "Z"}


# ── LIST (cursor-paginated) ──

@router.get("/sessions")
async def list_sessions(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
    limit: int = Query(DEFAULT_SESSION_LIMIT, ge=1, le=MAX_SESSION_LIMIT),
    before: Optional[str] = Query(None),
):
    """
    Paginated session list, newest first.
    Pass next_cursor from previous response as ?before= for next page.
    """
    filter_ = {"user_id": str(user["_id"])}
    if before:
        try:
            before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
            filter_["updated_at"] = {"$lt": before_dt}
        except ValueError:
            raise HTTPException(400, f"Invalid 'before' cursor: {before!r}")

    cursor = db["ai_sessions"].find(
        filter_,
        {
            "_id": 0,
            "session_id": 1, "name": 1, "surface_origin": 1,
            "created_at": 1, "updated_at": 1,
            "messages": {"$slice": -1},   # only last message for preview
        },
    ).sort("updated_at", -1).limit(limit + 1)

    sessions = []
    async for doc in cursor:
        last_msgs = doc.get("messages", [])
        last_msg = last_msgs[-1] if last_msgs else None
        sessions.append({
            "session_id": doc["session_id"],
            "name": doc.get("name", "Untitled"),
            "surface": doc.get("surface_origin", "web"),
            "last_message": (last_msg.get("content", "")[:80]) if last_msg else "",
            "last_role": last_msg.get("role") if last_msg else None,
            # TIMESTAMP FIX 2026-08-20 — appending "Z" here (and at every
            # other .isoformat() in this file) for the same reason as
            # _serialise_message below: these are naive datetime.utcnow()
            # values with no timezone marker, which JS's `new Date(...)`
            # would otherwise misread as the viewer's LOCAL time instead
            # of UTC. See that function's comment for the full trace.
            "created_at": doc["created_at"].isoformat() + "Z" if doc.get("created_at") else "",
            "updated_at": doc["updated_at"].isoformat() + "Z" if doc.get("updated_at") else "",
        })

    has_more = len(sessions) > limit
    if has_more:
        sessions = sessions[:limit]

    return {
        "sessions": sessions,
        "total": len(sessions),
        "has_more": has_more,
        "next_cursor": sessions[-1]["updated_at"] if sessions and has_more else None,
        "limit": limit,
    }


# ── SEARCH MESSAGES (across all of the user's sessions) ──
#
# ADDED 2026-08-19 — the backend half of a feature the frontend already
# had a title-only version of (client-side filter over session names).
# This is real full-text search over message CONTENT, across every
# session the user owns, not just titles.
#
# HONEST SCOPING NOTE, matching what was actually built vs. what a
# larger version would need:
#
# - Uses a case-insensitive regex match against messages.content via
#   $elemMatch, NOT a Mongo $text index. No text index exists yet on
#   ai_sessions (confirmed by reading database.py's actual
#   create_index calls before writing this — only user_id, updated_at,
#   and the (user_id, surface_origin) compound index exist). A regex
#   scan works correctly at the data volumes a single-user system like
#   this actually has, and needs zero index/migration risk to ship. A
#   $text index would be faster at much larger scale (real relevance
#   ranking, language-aware stemming) but is a real infra change that
#   deserves its own testing pass against live data, not a same-turn
#   addition bundled into a search feature request.
# - Returns up to `limit` MATCHING SESSIONS (not individual messages)
#   with the single best-matching message per session as a preview —
#   mirrors how list_sessions() already returns one preview message per
#   session, so the frontend can reuse the exact same session-row
#   rendering it already has for the results, no new UI shape needed.
# - $elemMatch necessarily returns the WHOLE messages array for any
#   session where at least one message matches (Mongo can't project
#   "only the matching array element" without $ positional projection,
#   which doesn't support case-insensitive regex cleanly across all
#   driver versions) — this is fine at personal-project message-count
#   scale, but would need revisiting (aggregation pipeline with
#   $filter) if a single session ever grew to thousands of messages.

class MessageSearchResult(BaseModel):
    session_id: str
    session_name: str
    surface: str
    matched_message: dict
    match_count: int
    updated_at: str


@router.get("/sessions/search")
async def search_messages(
    q: str = Query(..., min_length=1, max_length=200),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
    limit: int = Query(20, ge=1, le=50),
):
    """
    Full-text search over message CONTENT across all of the user's
    sessions (not just titles — see api/sessions.py's search section
    docstring for the title-only client-side version this complements,
    and the honest scoping notes on what this endpoint does and
    doesn't optimize for yet).
    """
    import re as _re
    # Escape the query so special regex characters in what the user
    # typed (parentheses, brackets, etc.) are treated as literal text
    # to search for, not regex syntax — a raw, unescaped $regex here
    # would let a query like "a(b" 500-error instead of just finding
    # nothing, or worse, behave in a way the user never intended.
    safe_pattern = _re.escape(q.strip())

    cursor = db["ai_sessions"].find(
        {
            "user_id": str(user["_id"]),
            "messages": {"$elemMatch": {"content": {"$regex": safe_pattern, "$options": "i"}}},
        },
        {
            "_id": 0, "session_id": 1, "name": 1, "surface_origin": 1,
            "updated_at": 1, "messages": 1,
        },
    ).sort("updated_at", -1).limit(limit)

    results = []
    async for doc in cursor:
        messages = doc.get("messages", [])
        matches = [
            m for m in messages
            if _re.search(safe_pattern, m.get("content", "") or "", _re.IGNORECASE)
        ]
        if not matches:
            continue  # shouldn't happen given the $elemMatch filter above, but never trust it blindly
        # Best match: prefer the most RECENT matching message in the
        # session, since that's usually most relevant to "find that
        # thing I asked about" — not the first one chronologically.
        best = matches[-1]
        results.append({
            "session_id": doc["session_id"],
            "session_name": doc.get("name", "Untitled"),
            "surface": doc.get("surface_origin", "web"),
            "matched_message": _serialise_message(best),
            "match_count": len(matches),
            "updated_at": doc["updated_at"].isoformat() + "Z" if doc.get("updated_at") else "",
        })

    return {"query": q, "results": results, "count": len(results)}




# ── GET SESSION (metadata + latest N messages via $slice) ──

@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
    limit: int = Query(DEFAULT_MSG_LIMIT, ge=1, le=MAX_MSG_LIMIT),
):
    """
    Returns session metadata and the LATEST `limit` messages.
    MongoDB $slice does the slicing — full array never loaded.
    Use oldest_offset with GET /sessions/{id}/messages to fetch older messages.
    """
    # First: get total count without loading messages
    meta = await db["ai_sessions"].find_one(
        {"session_id": session_id, "user_id": str(user["_id"])},
        {"_id": 0, "messages": 0},  # exclude messages for count query
    )
    if not meta:
        raise HTTPException(404, "Session not found.")

    # Get total count via $size aggregation
    agg = await db["ai_sessions"].aggregate([
        {"$match": {"session_id": session_id, "user_id": str(user["_id"])}},
        {"$project": {"total": {"$size": "$messages"}}},
    ]).to_list(1)
    total_messages = agg[0]["total"] if agg else 0

    # Fetch only the last `limit` messages via $slice
    doc = await db["ai_sessions"].find_one(
        {"session_id": session_id, "user_id": str(user["_id"])},
        {"_id": 0, "messages": {"$slice": -limit}},
    )
    messages_raw = doc.get("messages", []) if doc else []
    messages = [_serialise_message(m) for m in messages_raw]

    returned = len(messages)
    # oldest_offset: how many messages are OLDER than what we returned
    # frontend passes this as ?offset= to load the previous page
    oldest_offset = max(0, total_messages - returned)
    has_older = oldest_offset > 0

    return {
        "session_id": session_id,
        "name": meta.get("name", "Untitled"),
        "surface": meta.get("surface_origin", "web"),
        "messages": messages,
        "total_messages": total_messages,
        "has_older": has_older,
        "oldest_offset": oldest_offset if has_older else None,
        "created_at": meta["created_at"].isoformat() + "Z" if meta.get("created_at") else "",
        "updated_at": meta["updated_at"].isoformat() + "Z" if meta.get("updated_at") else "",
    }


# ── GET MESSAGES (offset-based, for infinite scroll) ──

@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
    offset: int = Query(0, ge=0, description="Start index from beginning of messages array"),
    limit: int = Query(DEFAULT_MSG_LIMIT, ge=1, le=MAX_MSG_LIMIT),
):
    """
    Fetch a page of messages by offset for infinite scroll upward.

    offset=0 means the oldest messages. Use oldest_offset from GET /sessions/{id}
    to start from the right place, then subtract limit each page.

    MongoDB $slice [skip, count] does this server-side — no Python loops.

    Example for 87 total messages, limit=30:
      GET /sessions/{id}                       → msgs 57-86 (latest), oldest_offset=57
      GET /sessions/{id}/messages?offset=27    → msgs 27-56
      GET /sessions/{id}/messages?offset=0&limit=27 → msgs 0-26, has_older=false
    """
    # Validate session belongs to user (no messages needed for this check)
    exists = await db["ai_sessions"].find_one(
        {"session_id": session_id, "user_id": str(user["_id"])},
        {"_id": 1},
    )
    if not exists:
        raise HTTPException(404, "Session not found.")

    # $slice with [skip, count] — MongoDB slices server-side
    doc = await db["ai_sessions"].find_one(
        {"session_id": session_id, "user_id": str(user["_id"])},
        {"_id": 0, "messages": {"$slice": [offset, limit + 1]}},
    )
    messages_raw = doc.get("messages", []) if doc else []

    has_more_ahead = len(messages_raw) > limit  # more messages after this slice
    if has_more_ahead:
        messages_raw = messages_raw[:limit]

    messages = [_serialise_message(m) for m in messages_raw]
    has_older = offset > 0

    return {
        "session_id": session_id,
        "messages": messages,
        "count": len(messages),
        "offset": offset,
        "has_older": has_older,
        "oldest_offset": max(0, offset - limit) if has_older else None,
        "limit": limit,
    }


# ── RENAME ──

@router.patch("/sessions/{session_id}")
async def rename_session(
    session_id: str,
    body: RenameSessionRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    result = await db["ai_sessions"].update_one(
        {"session_id": session_id, "user_id": str(user["_id"])},
        {"$set": {"name": body.name, "updated_at": datetime.utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Session not found.")
    return {"status": "ok", "name": body.name}


# ── DELETE ──

@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    result = await db["ai_sessions"].delete_one({
        "session_id": session_id,
        "user_id": str(user["_id"]),
    })
    if result.deleted_count == 0:
        raise HTTPException(404, "Session not found.")
    return {"status": "deleted", "session_id": session_id}


# ── CLEAR MESSAGES ──

@router.delete("/sessions/{session_id}/messages")
async def clear_messages(
    session_id: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    result = await db["ai_sessions"].update_one(
        {"session_id": session_id, "user_id": str(user["_id"])},
        {"$set": {"messages": [], "updated_at": datetime.utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Session not found.")
    return {"status": "cleared"}


# ── HELPERS ──

def _serialise_message(m: dict) -> dict:
    t = m.get("time", "")
    if hasattr(t, "isoformat"):
        # BUG FIXED: t.isoformat() on a naive datetime (datetime.utcnow(),
        # which is what's actually stored — see orchestrator.py's
        # _persist_memory) produces a string with NO timezone marker at
        # all, e.g. "2026-08-20T09:13:45.938000" — no "Z", no "+00:00".
        # JavaScript's `new Date(...)` treats an offset-less ISO string as
        # LOCAL time to the browser, not UTC. So on a WAT (UTC+1) device,
        # a genuinely-correct UTC timestamp of 09:13 got silently
        # misread as 09:13 WAT (=08:13 UTC internally) — a full hour off,
        # and specifically off in a way that only showed up on reload
        # (this serializer), never on the live SSE path, since that path
        # already had this same fix via orchestrator.py's persisted_at
        # +.isoformat() — which has the identical bug, just not yet
        # noticed there because the live path also has a same-clock
        # fallback that usually masks it. Appending "Z" (ISO 8601's own
        # explicit-UTC marker) makes every consumer — chat.html's
        # `new Date(m.time)`, any other client — parse this unambiguously
        # as UTC regardless of what timezone the viewing device is in.
        t = t.isoformat() + "Z"
    return {
        "role": m.get("role", "user"),
        "content": m.get("content", ""),
        "time": str(t),
        "steps": m.get("steps", []),
        "model_used": m.get("model_used"),
        "reasoning_mode": m.get("reasoning_mode", False),
        "attachment_url": m.get("attachment_url"),
        "attachment_type": m.get("attachment_type"),
        "attachment_filename": m.get("attachment_filename"),
    }
