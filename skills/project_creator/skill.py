"""
project_creator — Phase 10-adjacent, from the "OADSON Engineering Upgrade"
roadmap, Problem 2 (file_generation can't produce complete projects) +
Problem 6 (mandatory validation before delivery), now with a real
iterative repair loop instead of a one-shot syntax check.

Generates a full multi-file project as one zip, using the exact same
storage/serving contract file_generation already established: base64
content in db.file_sessions, served via GET /api/v3/files/serve/{filename}.
No Cloudinary, no local filesystem for the final artifact — same reason
file_generation doesn't use them: Render's filesystem is ephemeral,
Mongo isn't.

Validation is now real, via core/code_validator.py:
  1. Syntax check every .py file (ast.parse — free, instant).
  2. If an entrypoint is given, actually RUN it in a subprocess and check
     for a clean exit — catches real logic/runtime errors syntax
     checking can't see.
  3. If anything's broken, feed the actual error back to Gemini for a
     fix, re-check, repeat (max 2 rounds by default) — this is the
     "iterate and improve it" loop, not just a report-and-ship pass.

Still Python-only for the execution check (no JS/TS runtime available
here without a much bigger dependency footprint), and still no
architecture/naming/consistency review (Problem 8) — that's a separate,
genuinely fuzzy check this doesn't attempt.
"""

import base64
import io
import zipfile
import logging
import os
from datetime import datetime

from core.code_validator import iterative_repair

logger = logging.getLogger("oadson.skill.project_creator")


def _build_zip(files: list) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            path = f.get("path", "").lstrip("/")
            content = f.get("content", "")
            if not path:
                continue
            zf.writestr(path, content)
    return buf.getvalue()


async def run(payload: dict) -> dict:
    user_id = payload.get("user_id")
    session_id = payload.get("session_id", "default")
    args = payload.get("args", {}) or {}

    if not user_id:
        return {"status": "error", "reason": "User ID not available"}

    project_name = (args.get("project_name") or "").strip()
    files = args.get("files")
    entrypoint = (args.get("entrypoint") or "").strip() or None

    if not project_name:
        return {"status": "error", "reason": "project_name is required"}
    if not files or not isinstance(files, list):
        return {"status": "error", "reason": "files (a non-empty list) is required"}

    clean_files = []
    for f in files:
        if not isinstance(f, dict) or not f.get("path"):
            continue
        clean_files.append({"path": f["path"], "content": f.get("content", "")})

    if not clean_files:
        return {"status": "error", "reason": "No valid files provided (each needs a 'path')"}

    try:
        api_key = os.getenv("GEMINI_API_KEY", "")
        repair_result = await iterative_repair(
            clean_files, api_key=api_key, run_entrypoint=entrypoint,
        )
        final_files = repair_result["files"]

        zip_bytes = _build_zip(final_files)
        safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in project_name).strip("-") or "project"
        filename = f"{safe_name}.zip"

        from bootstrap import get
        db = get("db")

        encoded = base64.b64encode(zip_bytes).decode()
        await db["file_sessions"].update_one(
            {"user_id": user_id, "session_id": session_id, "filename": filename},
            {"$set": {
                "user_id": user_id, "session_id": session_id,
                "filename": filename, "content": encoded,
                "mime_type": "application/zip",
                "encoding": "base64", "uploaded_at": datetime.utcnow(),
            }},
            upsert=True,
        )

        backend_url = os.getenv(
            "BACKEND_URL",
            os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000")
        ).rstrip("/")
        download_url = f"{backend_url}/api/v3/files/serve/{filename}"

        message = f"✅ *{filename}* ready — {len(final_files)} file(s)"
        if repair_result["clean"]:
            if repair_result["iterations"] > 0:
                message += f" (fixed after {repair_result['iterations']} repair round(s))"
        else:
            message += f"\n⚠️ Still has issues after {repair_result['iterations']} repair round(s): {repair_result.get('remaining_errors')}"
        message += f"\n📥 Download: {download_url}"

        return {
            "status": "success",
            "project_name": project_name,
            "filename": filename,
            "files_created": len(final_files),
            "download_url": download_url,
            "validation": {
                "clean": repair_result["clean"],
                "repair_iterations": repair_result["iterations"],
                "remaining_errors": repair_result.get("remaining_errors", []),
                "trace": repair_result.get("trace", []),
            },
            "message": message,
        }

    except Exception as e:
        logger.error(f"project_creator error: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}
