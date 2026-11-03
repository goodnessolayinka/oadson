"""
core/project_memory.py — Phase 10, Group 3: Long-Term Project Memory.

Distinct from memory_manager (facts about the user) — this is facts
about a PROJECT: architecture decisions, "don't do X because Y broke
before", conventions, known gotchas. Deliberately thin: memory.manager.
MemoryManager already has a `project` field and `memory_type="project"`
support built in, it's just never been used by any skill until now.
Reuses that infra rather than inventing new storage.
"""

from memory.manager import MemoryManager


async def project_memory_action(db, user_id: str, action: str, args: dict) -> dict:
    manager = MemoryManager(db)
    project = (args.get("project") or "default").strip().lower().replace(" ", "_")

    if action == "save":
        key = (args.get("key") or "").strip().lower().replace(" ", "_")
        value = (args.get("value") or "").strip()
        if not key:
            return {"status": "error", "reason": "save requires a 'key'"}
        if not value:
            return {"status": "error", "reason": "save requires a 'value'"}

        await manager.store(
            user_id=user_id, key=f"project_{project}_{key}", content=value,
            memory_type="project", category="project", project=project,
        )
        return {"status": "success", "message": f"Saved to project '{project}': {key} = {value}"}

    elif action == "recall":
        key = (args.get("key") or "").strip().lower().replace(" ", "_")
        if not key:
            return {"status": "error", "reason": "recall requires a 'key'"}

        doc = await manager.get(user_id, f"project_{project}_{key}")
        if doc:
            return {"status": "success", "message": f"{key}: {doc['content']}", "value": doc["content"]}
        return {"status": "info", "message": f"No memory found for '{key}' in project '{project}'"}

    elif action == "list":
        docs = await manager.retrieve(
            user_id=user_id, memory_type="project", category="project", project=project, limit=50,
        )
        return {
            "status": "success",
            "message": f"{len(docs)} memor{'y' if len(docs) == 1 else 'ies'} for project '{project}'.",
            "memories": docs,
        }

    elif action == "delete":
        key = (args.get("key") or "").strip().lower().replace(" ", "_")
        if not key:
            return {"status": "error", "reason": "delete requires a 'key'"}

        deleted = await manager.delete(user_id, f"project_{project}_{key}")
        if deleted:
            return {"status": "success", "message": f"Deleted '{key}' from project '{project}'"}
        return {"status": "error", "reason": f"'{key}' not found in project '{project}'"}

    return {
        "status": "error",
        "reason": f"Unknown action '{action}'. Must be one of: save, recall, list, delete",
    }
