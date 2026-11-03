"""
Project Memory Skill — long-term memory scoped to a project, not a user.

Thin wrapper — see core/project_memory.py for the actual logic. This
file is just the skill-contract wrapper skill_registry expects.
"""

from core.project_memory import project_memory_action


async def run(payload):
    user_id = payload.get("user_id")
    db = payload.get("db")
    args = payload.get("args", {}) or {}

    if not user_id:
        return {"status": "error", "reason": "User ID not available"}
    if db is None:
        return {"status": "error", "reason": "Database connection not available in payload"}

    action = args.get("action")
    if not action:
        return {"status": "error", "reason": "action is required"}

    try:
        return await project_memory_action(db, user_id, action, args)
    except Exception as e:
        return {"status": "error", "reason": str(e)}
