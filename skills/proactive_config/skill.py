"""
proactive_config — configure how OADSON sounds in proactive WhatsApp messages.

Writes to the proactive_config collection, which proactive_agent.py reads
at the top of every _decide() call. Changes take effect on the NEXT
proactive message cycle (within 30 minutes).
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("oadson.skill.proactive_config")

BUTLER_DEFAULT = (
    "Warm, composed, butler-English. Speak like a trusted personal assistant — "
    "clear diction, no pidgin, no slang. Natural and human, not robotic. "
    "Morning greetings should feel like a calm, well-spoken attendant setting "
    "the tone for his day."
)


async def run(payload: dict) -> dict:
    user_id = payload.get("user_id")
    db = payload.get("db")
    args = payload.get("args", {}) or {}

    if not user_id or db is None:
        return {"status": "error", "reason": "Missing user_id or db"}

    action = args.get("action")

    try:
        if action == "set_voice_style":
            voice_style = (args.get("voice_style") or "").strip()
            if not voice_style:
                return {"status": "error", "reason": "voice_style is required for set_voice_style"}

            await db["proactive_config"].update_one(
                {"user_id": user_id},
                {"$set": {
                    "user_id": user_id,
                    "voice_style": voice_style,
                    "updated_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            return {
                "status": "ok",
                "message": f"Done — proactive voice style updated. Takes effect on the next message cycle.",
                "voice_style": voice_style,
            }

        elif action == "view":
            doc = await db["proactive_config"].find_one({"user_id": user_id}) or {}
            voice_style = doc.get("voice_style", BUTLER_DEFAULT)
            updated = doc.get("updated_at", "never set — using default")
            return {
                "status": "ok",
                "voice_style": voice_style,
                "updated_at": str(updated),
                "is_default": "voice_style" not in doc,
            }

        elif action == "reset":
            await db["proactive_config"].update_one(
                {"user_id": user_id},
                {"$unset": {"voice_style": ""}, "$set": {"updated_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
            return {
                "status": "ok",
                "message": "Reset to default butler-English voice.",
                "default": BUTLER_DEFAULT,
            }

        else:
            return {"status": "error", "reason": f"Unknown action '{action}'"}

    except Exception as e:
        logger.error(f"proactive_config error: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}
