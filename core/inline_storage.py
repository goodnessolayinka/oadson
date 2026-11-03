"""
core/inline_storage.py — Cloudinary-free file storage, shared by
api/files.py (web uploads) and api/whatsapp.py (WhatsApp media).

Stores bytes as base64 in db.file_sessions — the exact same
collection/schema api/serve.py already serves generated files from —
and returns a URL under our own GET /files/serve/{session_id}/{filename}.
No external upload service involved at all.
"""

import os
import uuid
import base64
import logging
from datetime import datetime

logger = logging.getLogger("oadson.core.inline_storage")


async def save_inline(db, contents: bytes, filename: str, mime_type: str, bucket: str = "uploads", preserve_filename: bool = False) -> str:
    """
    Save bytes inline and return a working, absolute serve URL.

    `bucket` doubles as the file_sessions "session_id" field — callers
    that want their own namespace (e.g. WhatsApp media grouped
    separately from web uploads) can pass a different bucket name;
    everything still goes through the same GET /files/serve/{session_id}/
    {filename} route either way.

    `preserve_filename` — ADDED 2026-08-27 for skills/website_developer's
    multi-file preview feature. By default this function prefixes a
    random id onto every filename to avoid collisions within a shared
    bucket, which is exactly right for something like "uploads" where
    many unrelated files land in the same namespace — but it silently
    breaks any site made of multiple files that reference each other by
    exact relative filename (index.html's `<link href="style.css">`
    needs an ACTUAL file literally named style.css sitting next to it at
    the served URL, or the browser's normal relative-path resolution has
    nothing correct to resolve against). Pass True when the caller
    already guarantees bucket-level uniqueness (e.g. a fresh per-build
    bucket) and filenames must stay exact for that reason.
    """
    safe_name = os.path.basename(filename) or "file"
    unique_name = safe_name if preserve_filename else f"{uuid.uuid4().hex[:8]}_{safe_name}"
    b64_content = base64.b64encode(contents).decode()

    await db["file_sessions"].update_one(
        {"session_id": bucket, "filename": unique_name},
        {"$set": {
            "user_id": None,  # single-user app; serve_file doesn't filter by user
            "session_id": bucket,
            "filename": unique_name,
            "content": b64_content,
            "encoding": "base64",
            "size": len(contents),
            "mime_type": mime_type or "application/octet-stream",
            "uploaded_at": datetime.utcnow(),
        }},
        upsert=True,
    )

    backend_url = os.getenv(
        "BACKEND_URL",
        os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000")
    ).rstrip("/")
    return f"{backend_url}/api/v3/files/serve/{bucket}/{unique_name}"


async def save_upload(db, contents: bytes, filename: str, mime_type: str, bucket: str = "uploads") -> dict:
    """
    ADDED 2026-09-03 — entry point for genuine USER uploads (web
    POST /files/upload and chat image attachments persisted for replay —
    see api/files.py and core/attachment_processor.py's extra_images
    save). Cloudinary was removed 2026-08-30 in favor of inline storage
    (see this file's module docstring / api/files.py's own history
    comment); re-added here by request, scoped narrowly to just these
    two call sites rather than every internal storage use — generated
    output (file_generation, website_developer's multi-file preview,
    archive_creator) and WhatsApp media all keep using save_inline()
    directly and are UNCHANGED, since website_developer in particular
    depends on exact relative-path serving that Cloudinary URLs don't
    give it, and WhatsApp media was never part of this request.

    Tries Cloudinary first when configured; falls back to inline Mongo
    storage on any Cloudinary failure (including "not configured at
    all") so a missing/incomplete CLOUDINARY_* env var never turns into
    a hard upload failure — it just quietly behaves exactly like before
    this change.

    Returns {"url": str, "storage": "cloudinary" | "inline"}.
    """
    from core.uploader import is_cloudinary_configured, upload_to_cloudinary

    if is_cloudinary_configured():
        try:
            safe_name = os.path.basename(filename) or "file"
            # Namespaced by bucket so uploads/attachments don't collide
            # in the same Cloudinary account, mirroring save_inline's own
            # bucket-as-namespace convention below.
            public_id = f"oadson/{bucket}/{uuid.uuid4().hex[:8]}_{safe_name}"
            result = await upload_to_cloudinary(contents, public_id=public_id, resource_type="auto")
            return {"url": result["url"], "storage": "cloudinary"}
        except Exception as e:
            logger.warning(f"Cloudinary upload failed ({e}) — falling back to inline storage for '{filename}'.")

    url = await save_inline(db, contents, filename, mime_type, bucket=bucket)
    return {"url": url, "storage": "inline"}


async def read_inline(db, bucket: str, filename: str) -> tuple[bytes, str] | None:
    """
    ADDED 2026-08-15. Read a previously-saved inline file's raw bytes
    directly from Mongo, with NO network round-trip.

    Every read of an uploaded/generated file previously had to go
    through GET /files/serve/{bucket}/{filename} over real HTTP — even
    from code running inside the same process. On a single-worker
    Render instance (WEB_CONCURRENCY=1, Render's own default for this
    instance size), that meant: the one worker handling the current
    request would curl its OWN server to fetch the file, but there is
    no second worker free to answer that curl — it can only be served
    once the current request finishes, which is exactly what's waiting
    on the curl. That's a real deadlock, not just a slow path, and it's
    what produced the 30-second curl timeouts (0 bytes transferred the
    entire time) seen when terminal tried to fetch an uploaded PDF from
    the app's own /files/serve endpoint to get it into the sandbox.

    Returns (raw_bytes, mime_type) or None if not found. Callers that
    need the file's bytes from inside the same process (e.g. a future
    document_reader action that reads Mongo-backed uploads directly
    into the sandbox) should use this instead of an HTTP round-trip.
    """
    doc = await db["file_sessions"].find_one({"session_id": bucket, "filename": filename})
    if not doc:
        return None
    encoding = doc.get("encoding", "base64")
    content = doc.get("content", "")
    if encoding == "base64":
        raw = base64.b64decode(content)
    else:
        raw = content.encode() if isinstance(content, str) else content
    return raw, doc.get("mime_type", "application/octet-stream")


async def read_inline_by_filename(db, filename: str) -> tuple[bytes, str] | None:
    """
    RESILIENCE FIX 2026-08-28. Same job as read_inline(), but looks up by
    filename ALONE — no bucket/session_id required — matching the same
    "search across all sessions, most recent wins" behavior
    api/serve.py's GET /files/serve/{filename} route already uses.

    Exists because a model occasionally hands document_reader's
    fetch_upload a malformed or half-remembered download_url (paraphrased
    from something earlier in the conversation rather than copied
    exactly) that doesn't parse into bucket+filename at all — seen in
    production as repeated "Could not parse bucket/filename" errors that
    the model then retried verbatim over a dozen times with the same bad
    URL, since nothing about that error told it the URL itself was the
    problem. Rather than fail outright the moment the two-segment form
    doesn't match, the caller can fall back to this — a same-cost lookup
    (one Mongo query either way) that recovers the file whenever a
    filename can still be pulled out of the malformed URL, and only
    surfaces a real error when the file genuinely isn't findable by any
    means.

    Returns (raw_bytes, mime_type) or None if not found.
    """
    doc = await db["file_sessions"].find_one(
        {"filename": filename},
        sort=[("uploaded_at", -1)],
    )
    if not doc:
        return None
    encoding = doc.get("encoding", "base64")
    content = doc.get("content", "")
    if encoding == "base64":
        raw = base64.b64decode(content)
    else:
        raw = content.encode() if isinstance(content, str) else content
    return raw, doc.get("mime_type", "application/octet-stream")
