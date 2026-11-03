"""
style_manager — see core/style_profile.py for the actual learning/writing
logic. On-demand only — the model calls this when a message explicitly
asks for it, nothing runs it automatically in the background.
"""

import os
import logging

from core.style_profile import learn_style, get_profile, write_as_user, reset_profile

logger = logging.getLogger("oadson.skill.style_manager")


async def run(payload: dict) -> dict:
    user_id = payload.get("user_id")
    db = payload.get("db")
    args = payload.get("args", {}) or {}

    if not user_id:
        return {"status": "error", "reason": "User ID not available"}
    if db is None:
        return {"status": "error", "reason": "Database connection not available in payload"}

    action = args.get("action")
    api_key = os.getenv("GEMINI_API_KEY", "")

    try:
        if action == "learn":
            additional_text = args.get("additional_text")
            return await learn_style(db, user_id, api_key, additional_text=additional_text)

        elif action == "view":
            return await get_profile(db, user_id)

        elif action == "write":
            request = args.get("request", "")
            if not request:
                return {"status": "error", "reason": "request is required for action='write'"}
            return await write_as_user(db, user_id, api_key, request)

        elif action == "reset":
            return await reset_profile(db, user_id)

        else:
            return {"status": "error", "reason": f"Unknown action '{action}'. Must be one of: learn, view, write, reset"}

    except Exception as e:
        logger.error(f"style_manager error: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}
