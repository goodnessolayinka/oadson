"""
Session Log Skill — retrieve conversation history from past OADSON sessions.

Was previously documentation-only (skill.md, no skill.py/manifest.json),
so SkillRegistry silently skipped it at boot and it never showed up as a
callable function for Gemini. This is the implementation described in
skill.md.

Reads from the same `ai_sessions` collection the orchestrator writes to
in core/orchestrator.py::_persist_memory.
"""

from datetime import datetime


def _session_summary(doc: dict) -> dict:
    messages = doc.get("messages", [])
    skills_used = sorted({
        m.get("skill") for m in messages if m.get("skill")
    })
    last_preview = ""
    if messages:
        last_preview = (messages[-1].get("content") or "")[:120]

    created_at = doc.get("created_at")
    return {
        "session_id": doc.get("session_id"),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": doc.get("updated_at").isoformat() if isinstance(doc.get("updated_at"), datetime) else doc.get("updated_at"),
        "message_count": len(messages),
        "skills_used": skills_used,
        "last_message_preview": last_preview,
        "surface": doc.get("surface_origin"),
    }


async def run(payload):
    """Execute session_log skill."""
    user_id = payload.get("user_id")
    db = payload.get("db")
    args = payload.get("args", {}) or {}
    current_session_id = payload.get("session_id")

    if not user_id:
        return {"status": "error", "reason": "User ID not available"}
    if db is None:
        return {"status": "error", "reason": "Database connection not available in payload"}

    action = args.get("action")
    limit = int(args.get("limit") or 10)

    try:
        if action == "list":
            cursor = db["ai_sessions"].find(
                {"user_id": user_id, "session_id": {"$ne": current_session_id}}
            ).sort("updated_at", -1).limit(limit)
            docs = [d async for d in cursor]
            sessions = [_session_summary(d) for d in docs]
            return {
                "status": "success",
                "message": f"Found {len(sessions)} recent session(s).",
                "sessions": sessions,
                "total": len(sessions),
            }

        elif action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}

            cursor = db["ai_sessions"].find({
                "user_id": user_id,
                "messages.content": {"$regex": query, "$options": "i"},
            }).sort("updated_at", -1).limit(limit)
            docs = [d async for d in cursor]
            sessions = [_session_summary(d) for d in docs]
            return {
                "status": "success",
                "message": f"Found {len(sessions)} session(s) matching '{query}'.",
                "sessions": sessions,
                "total": len(sessions),
            }

        elif action == "get":
            session_id = args.get("session_id")
            if not session_id:
                return {"status": "error", "reason": "get requires a 'session_id'"}

            doc = await db["ai_sessions"].find_one({
                "user_id": user_id,
                "session_id": session_id,
            })
            if not doc:
                return {"status": "error", "reason": f"Session '{session_id}' not found"}

            messages = doc.get("messages", [])
            for m in messages:
                if isinstance(m.get("time"), datetime):
                    m["time"] = m["time"].isoformat()

            return {
                "status": "success",
                "message": f"Retrieved {len(messages)} message(s) from session '{session_id}'.",
                "session_id": session_id,
                "messages": messages,
            }

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: list, search, get",
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
