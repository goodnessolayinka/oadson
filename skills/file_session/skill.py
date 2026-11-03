"""
file_session skill — OADSON V3.

Server-side file workspace. Zero filesystem. MongoDB stores everything.

Upload a file or zip → OADSON reads, edits, rewrites → download result.
Survives within session. Clears on explicit drop.

Actions:
  list     — list all files in current session
  read     — read a specific file
  write    — write/overwrite a file
  delete   — delete a file from session
  drop     — clear all files in session
  summary  — summarize all files OADSON can see
"""

import base64
import zipfile
import io
import logging
from datetime import datetime

logger = logging.getLogger("oadson.skill.file_session")

MAX_FILE_SIZE = 5 * 1024 * 1024   # 5MB per file
MAX_FILES = 50


async def run(payload: dict) -> dict:
    user_id = payload.get("user_id")
    session_id = payload.get("session_id", "default")

    if not user_id:
        return {"status": "error", "reason": "User ID not available"}

    try:
        from bootstrap import get
        db = get("db")
    except Exception as e:
        return {"status": "error", "reason": f"Database unavailable: {e}"}

    col = db["file_sessions"]
    args = payload.get("args", {}) or {}
    action = args.get("action") or "list"

    try:
        # ── LIST ──
        if action == "list":
            cursor = col.find(
                {"user_id": user_id, "session_id": session_id},
                {"_id": 0, "filename": 1, "size": 1, "mime_type": 1, "uploaded_at": 1}
            ).sort("uploaded_at", -1)
            files = [f async for f in cursor]
            for f in files:
                if f.get("uploaded_at"):
                    f["uploaded_at"] = f["uploaded_at"].isoformat()
            return {"status": "success", "files": files, "count": len(files),
                    "session_id": session_id}

        # ── READ ──
        elif action == "read":
            filename = (args.get("filename") or "").strip()
            if not filename:
                return {"status": "error", "reason": "read requires 'filename'"}

            doc = await col.find_one(
                {"user_id": user_id, "session_id": session_id, "filename": filename},
                {"_id": 0, "content": 1, "filename": 1, "mime_type": 1, "size": 1}
            )
            if not doc:
                return {"status": "error", "reason": f"File '{filename}' not found in session"}

            content = doc["content"]
            truncated = len(content) > 8000
            return {
                "status": "success",
                "filename": filename,
                "content": content[:8000],
                "truncated": truncated,
                "size": doc.get("size", 0),
                "message": "Showing first 8000 chars" if truncated else "Full file"
            }

        # ── WRITE ──
        elif action == "write":
            filename = (args.get("filename") or "").strip()
            content = (args.get("content") or payload.get("message", "")).strip()
            if not filename:
                return {"status": "error", "reason": "write requires 'filename'"}
            if not content:
                return {"status": "error", "reason": "write requires 'content'"}

            now = datetime.utcnow()
            await col.update_one(
                {"user_id": user_id, "session_id": session_id, "filename": filename},
                {"$set": {
                    "user_id": user_id,
                    "session_id": session_id,
                    "filename": filename,
                    "content": content,
                    "size": len(content),
                    "mime_type": "text/plain",
                    "uploaded_at": now,
                    "modified_at": now,
                }},
                upsert=True
            )
            return {"status": "success", "filename": filename,
                    "size": len(content), "message": f"Written: {filename}"}

        # ── DELETE ──
        elif action == "delete":
            filename = (args.get("filename") or "").strip()
            if not filename:
                return {"status": "error", "reason": "delete requires 'filename'"}
            result = await col.delete_one(
                {"user_id": user_id, "session_id": session_id, "filename": filename}
            )
            if result.deleted_count == 0:
                return {"status": "error", "reason": f"File '{filename}' not found"}
            return {"status": "success", "message": f"Deleted: {filename}"}

        # ── DROP ──
        elif action == "drop":
            result = await col.delete_many(
                {"user_id": user_id, "session_id": session_id}
            )
            return {"status": "success", "deleted": result.deleted_count,
                    "message": f"Cleared {result.deleted_count} file(s) from session"}

        # ── SUMMARY ──
        elif action == "summary":
            cursor = col.find(
                {"user_id": user_id, "session_id": session_id},
                {"_id": 0, "filename": 1, "content": 1, "size": 1}
            )
            summaries = []
            async for doc in cursor:
                content = doc["content"]
                lines = content.split("\n")
                summaries.append({
                    "filename": doc["filename"],
                    "lines": len(lines),
                    "size": doc.get("size", 0),
                    "preview": content[:300]
                })
            return {
                "status": "success",
                "session_id": session_id,
                "files": summaries,
                "count": len(summaries),
                "message": f"{len(summaries)} file(s) available in this session"
            }

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Use: list, read, write, delete, drop, summary"
            }

    except Exception as e:
        logger.error(f"File session error: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}
