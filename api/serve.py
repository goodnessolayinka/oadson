"""
api/serve.py — OADSON V3 File Serving.

Serves generated files directly from db.file_sessions.
No Cloudinary. No filesystem. Pure MongoDB → HTTP response.

URL pattern: GET /api/v3/files/serve/{filename}
             GET /api/v3/files/serve/{session_id}/{filename}

Works on WhatsApp, Telegram, web, app — any surface that can open a URL.
"""

import logging
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import Response

from dependency import get_db

logger = logging.getLogger("oadson.api.serve")

router = APIRouter()

MIME_TYPES = {
    ".md":   "text/markdown; charset=utf-8",
    ".txt":  "text/plain; charset=utf-8",
    ".py":   "text/x-python; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".ts":   "application/typescript; charset=utf-8",
    ".tsx":  "application/typescript; charset=utf-8",
    ".jsx":  "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".csv":  "text/csv; charset=utf-8",
    ".xml":  "application/xml; charset=utf-8",
    ".yaml": "text/yaml; charset=utf-8",
    ".yml":  "text/yaml; charset=utf-8",
    ".sh":   "text/x-sh; charset=utf-8",
    ".sql":  "text/x-sql; charset=utf-8",
    ".pdf":  "application/pdf",
    ".zip":  "application/zip",
}


def _get_mime(filename: str) -> str:
    from pathlib import Path
    ext = Path(filename).suffix.lower()
    return MIME_TYPES.get(ext, "application/octet-stream")


@router.get("/files/serve/{filename}")
async def serve_file(
    filename: str,
    db=Depends(get_db),
    session_id: str = None,
    download: int = 0,
):
    """
    Serve a file by name from file_sessions.
    Searches across all sessions for the filename.
    Most recent match wins.
    """
    query = {"filename": filename}
    if session_id:
        query["session_id"] = session_id

    doc = await db["file_sessions"].find_one(
        query,
        {"content": 1, "filename": 1, "mime_type": 1, "encoding": 1},
        sort=[("uploaded_at", -1)]
    )

    if not doc:
        raise HTTPException(404, f"File '{filename}' not found.")

    raw = doc.get("content", "")
    mime = doc.get("mime_type") or _get_mime(filename)
    encoding = doc.get("encoding", "utf-8")

    # Handle base64-encoded binary files (DOCX, PDF)
    if encoding == "base64":
        import base64
        file_bytes = base64.b64decode(raw)
    else:
        file_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw

    # BUG FOUND AND FIXED 2026-08-27 — "hosted preview, not just code".
    # This unconditionally sent `Content-Disposition: attachment`, which
    # tells the browser "always save this, never render it" — completely
    # correct for the download links this endpoint was originally built
    # for, but it makes this same endpoint unusable for the ONE thing a
    # live preview needs: pointing an <iframe src="..."> straight at a
    # generated index.html and having the browser actually render it.
    # With `attachment` forced, the iframe would just show a blank/broken
    # frame — browsers refuse to render an attachment-disposition
    # response inline, full stop. Also matters for a MULTI-FILE site:
    # index.html's own relative `<link href="style.css">` becomes a
    # request to this same endpoint, and that request would ALSO get
    # forced to `attachment`, breaking the browser's normal "just fetch
    # and apply this stylesheet" behavior.
    # Now defaults to inline (so preview/iframe/relative-asset-loading
    # all work normally, exactly like visiting a real hosted page), and
    # a real download is opt-in via `?download=1` — which is what every
    # actual "Download" button now explicitly appends, so those keep
    # working exactly as before.
    disposition = "attachment" if download else "inline"
    return Response(
        content=file_bytes,
        media_type=mime,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Cache-Control": "no-cache",
        }
    )


@router.get("/files/serve/{session_id}/{filename}")
async def serve_file_by_session(
    session_id: str,
    filename: str,
    db=Depends(get_db),
    download: int = 0,
):
    """Serve a file from a specific session."""
    return await serve_file(filename=filename, db=db, session_id=session_id, download=download)
