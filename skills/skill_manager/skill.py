"""
skill_manager — V3 version.

V2 read from skills_manager object passed in payload.
V3 reads from bootstrap registry directly.
Same actions: list, describe, search.
"""


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    action = args.get("action", "list")
    message = payload.get("message", "").lower()

    # Natural language fallback
    if not action and message:
        if "list" in message or "what skills" in message:
            action = "list"
        elif "search" in message:
            action = "search"
        else:
            action = "list"

    try:
        from bootstrap import get
        registry = get("skill_registry")
        catalog = registry.all()
    except Exception as e:
        return {"status": "error", "reason": f"Registry unavailable: {e}"}

    if action == "list":
        skills = [
            {
                "name": s["name"],
                "description": s.get("description", ""),
                "category": s.get("category", ""),
            }
            for s in catalog
        ]
        return {
            "status": "success",
            "total_skills": len(skills),
            "skills": skills,
            "message": f"{len(skills)} skill(s) loaded",
        }

    elif action == "describe":
        skill_name = (args.get("skill_name") or "").strip()
        if not skill_name:
            return {"status": "error", "reason": "describe requires 'skill_name'"}

        entry = registry.get(skill_name)
        if not entry:
            available = ", ".join(sorted(s["name"] for s in catalog))
            return {
                "status": "error",
                "reason": f"Unknown skill '{skill_name}'. Available: {available}",
            }

        manifest = entry.get("manifest", {})
        return {
            "status": "success",
            "skill": {
                "name": entry["name"],
                "description": entry.get("description", ""),
                "category": entry.get("category", ""),
                "version": entry.get("version", "1.0.0"),
                "permissions": entry.get("permissions", []),
                "internet_access": manifest.get("internet_access", False),
                "filesystem_access": manifest.get("filesystem_access", False),
                "shell_access": manifest.get("shell_access", False),
            },
        }

    elif action == "search":
        query = (args.get("query") or "").strip().lower()
        if not query:
            return {"status": "error", "reason": "search requires 'query'"}

        matches = [
            {"name": s["name"], "description": s.get("description", "")}
            for s in catalog
            if query in s["name"].lower() or query in s.get("description", "").lower()
        ]
        return {
            "status": "success",
            "query": query,
            "matches": matches,
            "count": len(matches),
            "message": f"Found {len(matches)} skill(s) matching '{query}'",
        }

    elif action == "reload":
        skill_name = (args.get("skill_name") or "").strip()
        try:
            from bootstrap import get
            loader = get("skill_loader")
            if skill_name:
                success = loader.reload(skill_name)
                return {"status": "success" if success else "error", "skill": skill_name}
            else:
                loader.load_all()
                return {"status": "success", "message": "All skills reloaded"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    else:
        return {
            "status": "error",
            "reason": f"Unknown action '{action}'. Use: list, describe, search, reload",
        }
