"""
Code Index Skill — semantic search over OADSON's own codebase.

Indexes files where the running process actually lives (its own deployed
source, via os.walk — no shell/terminal round-trip needed since this
runs in-process), embeds via Gemini's real embedding model, and searches
by cosine similarity. See core/code_index.py for the actual logic — this
file is just the skill-contract wrapper skill_registry expects.
"""

import os

from core.code_index import index_project, search_code, index_status


async def run(payload):
    user_id = payload.get("user_id")
    db = payload.get("db")
    args = payload.get("args", {}) or {}

    if not user_id:
        return {"status": "error", "reason": "User ID not available"}
    if db is None:
        return {"status": "error", "reason": "Database connection not available in payload"}

    action = args.get("action")
    project = (args.get("project") or "oadson").strip().lower().replace(" ", "_")
    api_key = os.getenv("GEMINI_API_KEY", "")

    try:
        if action == "index":
            root = args.get("root") or os.getcwd()
            result = await index_project(db, user_id, project, root, api_key)
            return result

        elif action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}
            limit = int(args.get("limit") or 5)
            result = await search_code(db, user_id, project, query, api_key, limit)
            return result

        elif action == "status":
            result = await index_status(db, user_id, project)
            return result

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: index, search, status",
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
