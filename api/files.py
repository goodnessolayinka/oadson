"""
api/files.py — OADSON V3 Files Router.

Stores uploads via Cloudinary when configured, inline in MongoDB
otherwise. Metadata in MongoDB either way. List and delete per user.

CLOUDINARY REINSTATED 2026-09-03: Cloudinary was removed 2026-08-30
because the old USE_CLOUDINARY flag defaulted false and the branch
never ran in production (see core/inline_storage.py's save_upload()
docstring for the full history and what's now shared between this
route and chat attachments). Re-added by request — the actual
Cloudinary-vs-inline decision now lives in save_upload() so this file
doesn't need its own branching. The response/DB field is still named
`cloudinary_url` on purpose — it's the wire contract the frontend and
core/attachment_processor.py already read from; it now genuinely holds
a Cloudinary URL when Cloudinary is configured, and an inline serve URL
when it isn't (or when the Cloudinary call itself fails).
"""

import uuid
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from dependency import get_current_user, get_db
from core.inline_storage import save_upload

logger = logging.getLogger("oadson.api.files")

router = APIRouter()


@router.post("/files/upload")
async def upload_file(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    contents = await file.read()
    if len(contents) > 20 * 1024 * 1024:  # 20MB hard cap
        raise HTTPException(400, "File too large. Max 20MB.")

    file_id = str(uuid.uuid4())

    upload = await save_upload(db, contents, file.filename, file.content_type, bucket="uploads")

    now = datetime.utcnow()
    doc = {
        "file_id": file_id,
        "user_id": user["_id"],
        "filename": file.filename,
        "cloudinary_url": upload["url"],
        "storage": upload["storage"],
        "file_type": file.content_type or "application/octet-stream",
        "file_size": len(contents),
        "created_at": now,
    }
    await db["files"].insert_one(doc)

    return {
        "file_id": file_id,
        "filename": file.filename,
        "cloudinary_url": upload["url"],
        "storage": upload["storage"],
        "file_type": doc["file_type"],
        "file_size": len(contents),
        "created_at": now.isoformat() + "Z",
    }


@router.get("/files")
async def list_files(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    cursor = db["files"].find(
        {"user_id": user["_id"]},
        {"_id": 0},
    ).sort("created_at", -1).limit(100)

    files = []
    async for doc in cursor:
        doc["created_at"] = (doc["created_at"].isoformat() + "Z") if hasattr(doc.get("created_at"), "isoformat") else str(doc.get("created_at", ""))
        files.append(doc)

    return {"files": files, "total": len(files)}


@router.get("/files/{file_id}")
async def get_file(
    file_id: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    doc = await db["files"].find_one(
        {"file_id": file_id, "user_id": user["_id"]},
        {"_id": 0},
    )
    if not doc:
        raise HTTPException(404, "File not found.")
    return doc


@router.delete("/files/{file_id}")
async def delete_file(
    file_id: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    doc = await db["files"].find_one({"file_id": file_id, "user_id": user["_id"]})
    if not doc:
        raise HTTPException(404, "File not found.")

    await db["files"].delete_one({"file_id": file_id})

    # Extract the unique filename from the serve URL to remove the
    # matching file_sessions doc too, so it doesn't linger forever.
    url = doc.get("cloudinary_url", "")
    inline_name = url.rsplit("/", 1)[-1] if url else None
    if inline_name:
        await db["file_sessions"].delete_one({"session_id": "uploads", "filename": inline_name})

    return {"status": "deleted", "file_id": file_id}
