"""
skill_creator — see core/skill_creator.py for the actual propose/create
logic and the full explanation of why this is deliberately two-step and
never autonomous.
"""

import os
import logging

from core.skill_creator import propose_skill, create_skill_from_proposal

logger = logging.getLogger("oadson.skill.skill_creator")


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
        if action == "propose":
            capability_description = args.get("capability_description", "")
            return await propose_skill(api_key, "gemini-3.1-flash-lite", db, user_id, capability_description)

        elif action == "create":
            proposal_id = args.get("proposal_id", "")
            if not proposal_id:
                return {"status": "error", "reason": "proposal_id is required for action='create'"}

            from bootstrap import get
            skills_dir = os.getenv("SKILLS_DIR", "skills")
            skill_loader = get("skill_loader")
            return await create_skill_from_proposal(db, skills_dir, skill_loader, user_id, proposal_id)

        else:
            return {"status": "error", "reason": f"Unknown action '{action}'. Must be 'propose' or 'create'"}

    except Exception as e:
        logger.error(f"skill_creator error: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}
