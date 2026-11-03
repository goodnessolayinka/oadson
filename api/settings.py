"""
api/settings.py — OADSON V3 Settings Router.

Profile (name/bio/picture), preferences (theme/model/notifications),
and account deletion — all scoped to the current user's own Bearer
session token (get_current_user), NOT the operator-only
OADSON_ENGINEER_TOKEN used by api/engineer.py.

That distinction matters: /engineer/* exists for the app's operator to
debug the whole system from an external tool (Hoppscotch, curl) using a
separate admin secret that never ships to a browser. This router is what
chat.html's own Settings modal actually calls, authenticated the same way
every other in-app request already is. The two are deliberately not the
same code path — putting OADSON_ENGINEER_TOKEN in client-side JS would
turn a break-glass admin credential into a public one.

`bio` is a new field — it didn't exist anywhere in the user schema
before this file. GET/PATCH /settings/profile is the first place it's
read or written.

Endpoints:
  GET   /settings              — profile + preferences + stats, combined
                                  (matches chat.html's loadSettings())
  PATCH /settings/profile      — update name/bio/picture
  PATCH /settings/preferences  — update theme/model_preference/
                                  notifications_enabled/auto_save
  DELETE /settings/account     — delete user, sessions, ai_sessions,
                                  files (inline storage + DB), memory
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

from dependency import get_current_user, get_db
from core.inline_storage import save_upload

logger = logging.getLogger("oadson.api.settings")

router = APIRouter()

DEFAULT_PREFERENCES = {
    "theme": "dark",
    "model_preference": "balanced",
    "notifications_enabled": True,
    "auto_save": True,
}


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    bio: Optional[str] = None
    picture: Optional[str] = None


class PreferencesUpdate(BaseModel):
    theme: Optional[str] = None
    model_preference: Optional[str] = None
    notifications_enabled: Optional[bool] = None
    auto_save: Optional[bool] = None


async def _compute_stats(db, user_id) -> dict:
    """Shared by GET /settings and GET /analytics/overview — one place
    these numbers are computed so the two can't silently drift apart."""
    total_sessions = await db["ai_sessions"].count_documents({"user_id": str(user_id)})

    # Message count lives inside each session doc's "messages" array, not
    # as a separate collection — has to be summed via aggregation rather
    # than a plain count_documents.
    pipeline = [
        {"$match": {"user_id": str(user_id)}},
        {"$project": {"count": {"$size": {"$ifNull": ["$messages", []]}}}},
        {"$group": {"_id": None, "total": {"$sum": "$count"}}},
    ]
    agg = await db["ai_sessions"].aggregate(pipeline).to_list(length=1)
    total_messages = agg[0]["total"] if agg else 0

    total_files = await db["files"].count_documents({"user_id": user_id})

    return {
        "total_sessions": total_sessions,
        "total_messages": total_messages,
        "total_files": total_files,
    }


@router.get("/settings")
async def get_settings(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    prefs = {**DEFAULT_PREFERENCES, **(user.get("preferences") or {})}
    stats = await _compute_stats(db, user["_id"])

    return {
        "profile": {
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "bio": user.get("bio", ""),
            "picture": user.get("picture", ""),
        },
        "preferences": prefs,
        "stats": stats,
    }


@router.post("/settings/profile/picture")
async def upload_profile_picture(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Live picture upload — previously the only way to set a profile
    picture was pasting a URL by hand (see chat.html's "Picture URL"
    field). Stored via save_upload() (Cloudinary-first, inline
    fallback) — see core/inline_storage.py.

    FIX 2026-09-03: this imported `_upload_inline` directly from
    api/files.py, a private (leading-underscore) helper that was
    removed when api/files.py switched to save_upload() — broke the
    deploy outright (ImportError at module load, since api/settings.py
    is imported unconditionally from main.py). Fixed by importing
    save_upload() from its real home in core/inline_storage.py, the
    same shared entry point api/files.py itself now uses, instead of
    reaching into another router module's private helper at all.
    """
    contents = await file.read()
    if len(contents) > 8 * 1024 * 1024:  # 8MB — plenty for a profile photo
        raise HTTPException(400, "Image too large. Max 8MB.")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "File must be an image.")

    upload = await save_upload(db, contents, file.filename or "profile.jpg", file.content_type, bucket="profile_pictures")

    await db["users"].update_one({"_id": user["_id"]}, {"$set": {"picture": upload["url"]}})
    return {"status": "ok", "picture": upload["url"], "storage": upload["storage"]}


@router.patch("/settings/profile")
async def update_profile(
    body: ProfileUpdate,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update:
        return {"status": "ok", "message": "Nothing to update."}

    await db["users"].update_one({"_id": user["_id"]}, {"$set": update})
    logger.info(f"Profile updated for {user.get('email')}: {list(update.keys())}")
    return {"status": "ok", **update}


@router.patch("/settings/preferences")
async def update_preferences(
    body: PreferencesUpdate,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update:
        return {"status": "ok", "message": "Nothing to update."}

    # Preferences live in a nested sub-document so a partial update here
    # can't accidentally clobber other top-level user fields.
    set_doc = {f"preferences.{k}": v for k, v in update.items()}
    await db["users"].update_one({"_id": user["_id"]}, {"$set": set_doc})
    return {"status": "ok", **update}


@router.delete("/settings/account")
async def delete_account(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Cascading delete — removes everything tied to this user:
    auth sessions, chat sessions, uploaded files (inline storage + DB
    record), and memory. Deleting the user document last, so a failure
    partway through leaves the user document (and therefore their
    ability to log back in and see what's left) intact rather than
    orphaning data with no owner.

    CLOUDINARY REMOVED 2026-08-30: every file in db["files"] is inline
    storage now (see api/files.py) — the old branch here that split
    docs into inline-vs-Cloudinary and best-effort-deleted the
    Cloudinary side was dead in practice (that path never ran once
    USE_CLOUDINARY defaulted to false) and is gone; every doc now just
    gets its matching file_sessions record cleaned up.
    """
    user_id = user["_id"]
    user_id_str = str(user_id)

    await db["sessions"].delete_many({"user_id": user_id})
    await db["ai_sessions"].delete_many({"user_id": user_id_str})
    await db["memory"].delete_many({"user_id": user_id_str})

    file_docs = await db["files"].find({"user_id": user_id}).to_list(length=None)
    for doc in file_docs:
        url = doc.get("cloudinary_url", "")
        inline_name = url.rsplit("/", 1)[-1] if url else None
        if inline_name:
            await db["file_sessions"].delete_one({"session_id": "uploads", "filename": inline_name})

    await db["files"].delete_many({"user_id": user_id})

    await db["users"].delete_one({"_id": user_id})

    logger.warning(f"Account deleted: {user.get('email')}")
    return {"status": "deleted"}
