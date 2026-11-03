"""
skills/speech_profile/skill.py — manage the owner's speech profile.

Thin wrapper over core/speech_profile.py, same split as style_manager
over core/style_profile.py: the logic lives in core/, the skill is the
tool-facing surface.

Actions: view, add_term, add_correction, add_example, add_phrase, reset.
"""

import logging

logger = logging.getLogger("oadson.skill.speech_profile")


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    user_id = payload.get("user_id")

    if not user_id:
        return {"status": "error", "reason": "User ID not available."}

    action = (args.get("action") or "view").strip().lower()

    from bootstrap import get
    from core import speech_profile as sp

    db = get("db")

    if action == "view":
        return await sp.get_profile(db, user_id)

    if action == "add_term":
        return await sp.add_term(db, user_id, args.get("term"))

    if action == "add_correction":
        return await sp.add_correction(db, user_id, args.get("heard"), args.get("meant"))

    if action == "add_example":
        return await sp.add_example(db, user_id, args.get("audio_ref"), args.get("transcript"))

    if action == "add_phrase":
        return await sp.add_phrase(db, user_id, args.get("phrase"))

    if action == "reset":
        return await sp.reset_profile(db, user_id)

    return {
        "status": "error",
        "reason": f"Unknown action '{action}'. Valid: view, add_term, add_correction, add_example, add_phrase, reset.",
    }
