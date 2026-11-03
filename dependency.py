"""
dependency.py — OADSON V3 Dependencies.

FastAPI Depends() functions.
Services come from bootstrap registry — no local instantiation.
"""

import os
import logging
from typing import Optional, Any
from datetime import datetime

from fastapi import HTTPException, Depends, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from bson import ObjectId
from core.db_router import MultiClusterDB

logger = logging.getLogger("oadson.dependency")

# Registers the Authorize button in Swagger UI
security = HTTPBearer(auto_error=False)

ALLOWED_EMAILS = {
    *[e.strip() for e in os.getenv("OADSON_ALLOWED_EMAILS", "").split(",") if e.strip()],
    "dev@oadson.local",
}


# ── DATABASE ──

async def get_db() -> MultiClusterDB:
    from bootstrap import get
    return get("db")


# ── ORCHESTRATOR ──

def get_orchestrator():
    from bootstrap import get
    return get("orchestrator")


# ── SKILL REGISTRY ──

def get_skill_registry():
    from bootstrap import get
    return get("skill_registry")


# ── MEMORY MANAGER ──

def get_memory_manager():
    from bootstrap import get
    return get("memory_manager")


# ── EVENT BUS ──

def get_event_bus():
    from bootstrap import get
    return get("event_bus")


# ── AUTH ──

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security),
    db: Any = Depends(get_db),
) -> dict:

    if not credentials or not credentials.credentials:
        raise HTTPException(401, "Missing or malformed Authorization header.")

    token = credentials.credentials

    # Service token bypass (CLI / terminal surface)
    service_token = os.getenv("OADSON_SERVICE_TOKEN", "")
    if service_token and token == service_token:
        user = await db["users"].find_one({})
        if not user:
            raise HTTPException(401, "No user found for service token.")
        return user

    # Session lookup
    session = await db["sessions"].find_one({"session_token": token})
    if not session:
        raise HTTPException(401, "Invalid session token.")

    # Expiry check
    expires_at = session.get("expires_at")
    if expires_at and expires_at < datetime.utcnow():
        await db["sessions"].delete_one({"session_token": token})
        raise HTTPException(401, "Token expired. Log in again.")

    # Resolve user_id
    user_id = session.get("user_id")
    if isinstance(user_id, dict) and "$oid" in user_id:
        user_id = user_id["$oid"]
    if not isinstance(user_id, ObjectId):
        try:
            user_id = ObjectId(user_id)
        except Exception:
            raise HTTPException(401, "Malformed user_id in session.")

    # Fetch user
    user = await db["users"].find_one({"_id": user_id})
    if not user:
        raise HTTPException(401, "User not found.")

    # Whitelist
    if user.get("email") not in ALLOWED_EMAILS:
        raise HTTPException(403, "Access denied.")

    return user
