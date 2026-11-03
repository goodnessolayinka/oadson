"""
api/memory.py — OADSON V3 Memory Router.

All memory reads/writes go through MemoryManager or MemoryVault.
No direct db calls here.

Route order matters for FastAPI:
  /memory/core-summary        — new, must be above /memory/{key}
  /memory/core-summary/bootstrap  — new
  /memory/search              — must be above /memory/{key}
  /memory/{key}               — catch-all last
"""

import logging
import os
from typing import Optional, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dependency import get_current_user, get_memory_manager, get_db

logger = logging.getLogger("oadson.api.memory")

router = APIRouter()

MemoryType = Literal["user", "session", "project", "working"]


class StoreMemoryRequest(BaseModel):
    key: str
    content: str
    type: MemoryType = "user"
    category: str = "general"
    project: Optional[str] = None


class UpdateMemoryRequest(BaseModel):
    content: str
    category: Optional[str] = None


class CoreSummaryUpdateRequest(BaseModel):
    summary: str


# ── CORE SUMMARY (must be above /memory/{key}) ──

@router.get(
    "/memory/core-summary",
    summary="Get the core memory summary paragraph",
    description=(
        "Returns the single flowing paragraph that OADSON keeps about you — "
        "the Claude-style always-in-context summary. Empty string if none built yet."
    ),
)
async def get_core_summary(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    from memory.vault import MemoryVault
    vault = MemoryVault(db, str(user["_id"]))
    doc = await vault.get_core_summary()
    return {
        "summary": (doc.get("content") or "") if doc else "",
        "updated_at": (doc.get("updated_at").isoformat() + "Z" if doc and doc.get("updated_at") else None),
    }


@router.patch(
    "/memory/core-summary",
    summary="Manually edit the core memory summary",
    description=(
        "Replaces the summary paragraph with whatever you type — same as "
        "Claude's 'Manage summary' / edit flow in its memory panel."
    ),
)
async def update_core_summary(
    body: CoreSummaryUpdateRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    from memory.vault import MemoryVault
    vault = MemoryVault(db, str(user["_id"]))
    await vault.set_core_summary(body.summary)
    return {"status": "ok", "summary": body.summary.strip()}


@router.post(
    "/memory/core-summary/bootstrap",
    summary="Build initial summary from existing core memories",
    description=(
        "One-time migration: reads all existing core key-value memories, "
        "synthesizes them into the new flowing paragraph format, and saves it. "
        "Safe to re-run — just re-synthesizes from whatever core entries exist."
    ),
)
async def bootstrap_core_summary(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    from memory.vault import MemoryVault
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise HTTPException(500, "GEMINI_API_KEY not configured.")
    vault = MemoryVault(db, str(user["_id"]))
    return await vault.bootstrap_core_summary(api_key)


# ── STANDARD MEMORY CRUD ──

@router.post("/memory")
async def store_memory(
    body: StoreMemoryRequest,
    user: dict = Depends(get_current_user),
    memory=Depends(get_memory_manager),
):
    user_id = str(user["_id"])
    await memory.store(
        user_id=user_id,
        key=body.key,
        content=body.content,
        memory_type=body.type,
        category=body.category,
        project=body.project,
    )
    return {"status": "stored", "key": body.key}


@router.get("/memory")
async def list_memory(
    type: Optional[MemoryType] = None,
    category: Optional[str] = None,
    user: dict = Depends(get_current_user),
    memory=Depends(get_memory_manager),
):
    user_id = str(user["_id"])
    results = await memory.list_all(user_id=user_id, memory_type=type)

    if category:
        results = [r for r in results if r.get("category") == category]

    return {"memories": results, "total": len(results)}


@router.get("/memory/search")
async def search_memory(
    q: str,
    limit: int = 10,
    user: dict = Depends(get_current_user),
    memory=Depends(get_memory_manager),
):
    user_id = str(user["_id"])
    results = await memory.retrieve(user_id=user_id, query=q, limit=limit)
    return {"results": results, "total": len(results)}


# NOTE: /memory/{key} MUST stay below /memory/search and /memory/core-summary
# — FastAPI matches routes in registration order.
@router.get("/memory/{key}")
async def get_memory(
    key: str,
    user: dict = Depends(get_current_user),
    memory=Depends(get_memory_manager),
):
    user_id = str(user["_id"])
    doc = await memory.get(user_id=user_id, key=key)
    if not doc:
        raise HTTPException(404, f"Memory key '{key}' not found.")
    return doc


@router.patch("/memory/{key}")
async def update_memory(
    key: str,
    body: UpdateMemoryRequest,
    user: dict = Depends(get_current_user),
    memory=Depends(get_memory_manager),
):
    user_id = str(user["_id"])
    existing = await memory.get(user_id=user_id, key=key)
    if not existing:
        raise HTTPException(404, f"Memory key '{key}' not found.")

    await memory.store(
        user_id=user_id,
        key=key,
        content=body.content,
        memory_type=existing.get("type", "user"),
        category=body.category or existing.get("category", "general"),
    )
    return {"status": "updated", "key": key}


@router.delete("/memory/{key}")
async def delete_memory(
    key: str,
    user: dict = Depends(get_current_user),
    memory=Depends(get_memory_manager),
):
    user_id = str(user["_id"])
    deleted = await memory.delete(user_id=user_id, key=key)
    if not deleted:
        raise HTTPException(404, f"Memory key '{key}' not found.")
    return {"status": "deleted", "key": key}
