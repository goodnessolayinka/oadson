"""
api/skills.py — OADSON V3 Skills Router.

Read-only metadata endpoints for the frontend.
Hot-reload for when you push a new skill without restarting.
"""

import logging
import os
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dependency import get_current_user, get_skill_registry, get_db

logger = logging.getLogger("oadson.api.skills")

router = APIRouter()


@router.get("/skills")
async def list_skills(
    category: str = None,
    user: dict = Depends(get_current_user),
    registry=Depends(get_skill_registry),
):
    skills = registry.all()
    if category:
        skills = [s for s in skills if s.get("category", "").lower() == category.lower()]
    return {"skills": skills, "count": len(skills)}


@router.get("/skills/{name}")
async def get_skill(
    name: str,
    user: dict = Depends(get_current_user),
    registry=Depends(get_skill_registry),
):
    entry = registry.get(name)
    if not entry:
        raise HTTPException(404, f"Skill '{name}' not found.")
    # Return everything except the module object
    return {k: v for k, v in entry.items() if k != "module"}


class SkillExecuteRequest(BaseModel):
    args: dict = {}
    session_id: str = ""


@router.post("/skills/{name}/execute")
async def execute_skill(
    name: str,
    body: SkillExecuteRequest,
    user: dict = Depends(get_current_user),
    registry=Depends(get_skill_registry),
    db=Depends(get_db),
):
    """
    FIX 2026-08-22: proxy endpoint so local_execution surfaces (terminal)
    can actually reach server-side skills (websearch, github, etc.), not
    just the ~13 local file/shell tools their own client implements.

    Native Gemini function-calling is deliberately disabled entirely when
    local_execution=True (see core/tool_loop.py's tools_for_call gating)
    — the model can only emit JSON tool-call blocks in text, which the
    terminal client parses and executes locally. Before this endpoint,
    any tool name the model tried that wasn't one of the client's local
    handlers had nowhere to go — the client's executor simply had no
    server-side fallback path at all, so the model calling "websearch"
    or "github" from the terminal surface would just fail silently on
    the client with an "unknown tool" error, regardless of how clearly
    the prompt described those skills as available.

    FIX 2026-08-22 (second pass): the first version of this endpoint
    only forwarded the raw request body straight to SkillExecutor.execute()
    — missing everything core/orchestrator.py's base_payload normally
    supplies to every native function-calling skill call (user_id,
    session_id, db, google_token, github_token, surface, session_state).
    Skills like memory_manager read payload["user_id"]/payload["db"]
    directly and fail immediately without them ("User ID not available")
    — this wasn't a skill bug, it was this endpoint sending an
    incomplete payload no skill was ever designed to receive. Now builds
    the same enriched shape base_payload does, so a skill behaves
    identically whether it's invoked via native function-calling (web/
    app) or via this proxy (terminal) — same permission model, same
    context, no reduced capability for the terminal surface.

    "terminal" is refused here too (defense-in-depth alongside the
    client-side refusal in oadson_pkg's _execute_tool_with_proxy) — it
    runs shell commands on THIS server's filesystem, not the caller's
    machine, and proxying it would silently execute in the wrong place
    with no visible difference to the caller.
    """
    if name == "terminal":
        raise HTTPException(
            400,
            "The 'terminal' skill cannot be proxied — it runs on the backend's "
            "own filesystem, not the caller's machine. Use a local shell tool instead.",
        )
    if not registry.has(name):
        raise HTTPException(404, f"Skill '{name}' not found.")

    from bootstrap import get
    from core.google_oauth import get_valid_access_token

    executor = get("skill_executor")
    google_token = await get_valid_access_token(user, db)

    payload = {
        "message": "",  # direct skill invocation, not a chat turn — no objective text
        "user_id": str(user["_id"]),
        "session_id": body.session_id,
        "google_token": google_token,
        "github_token": os.getenv("GITHUB_TOKEN"),
        "surface": "terminal",  # only surfaces without native function-calling reach this endpoint at all
        "db": db,
        "session_state": {},
        "args": body.args,
    }
    result = await executor.execute(name, payload)
    return result


@router.post("/skills/{name}/reload")
async def reload_skill(
    name: str,
    user: dict = Depends(get_current_user),
):
    from bootstrap import get
    loader = get("skill_loader")
    success = loader.reload(name)
    if not success:
        raise HTTPException(500, f"Failed to reload skill '{name}'.")
    registry = get("skill_registry")
    entry = registry.get(name)
    return {
        "status": "reloaded",
        "skill": name,
        "version": entry.get("version", "?") if entry else "?",
    }
