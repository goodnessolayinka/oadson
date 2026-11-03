"""
skills/image_generation/skill.py — OADSON V3 Image Generation.

Uses Pollinations.ai's image endpoint — genuinely free, no card, no
signup required for basic use (unlike Gemini's Nano Banana line, which
is free only inside the Gemini APP, not the API — the API is paid per
image, roughly $0.04-$0.24 depending on resolution, confirmed against
Google's own pricing page). Pollinations is open-source, MIT-licensed,
and used by 500+ community projects specifically because it stays
free at the "hobby project" usage tier this system runs at.

No key at all works, rate-limited by IP. Setting POLLINATIONS_API_KEY
(a free sk_ secret key from enter.pollinations.ai — still no card)
removes that shared rate limit entirely.

Generated image bytes are stored the same way file_generation.py
stores binaries (file_sessions collection, served via
/api/v3/files/serve/{session}/{filename}) so it shows up identically
in chat/WhatsApp/web — same download-link pattern the rest of the
system already uses, deliberately not a separate storage path.
"""

import os
import re
import logging
import base64
from datetime import datetime
from urllib.parse import quote

import httpx

logger = logging.getLogger("oadson.skill.image_generation")

BACKEND_URL = os.getenv(
    "BACKEND_URL",
    os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000")
).rstrip("/")

POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"


def _serve_url(filename: str, session_id: str) -> str:
    return f"{BACKEND_URL}/api/v3/files/serve/{session_id}/{filename}"


def _slugify(prompt: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")
    return (slug[:40] or "image")


async def _store_binary(db, user_id, session_id, filename, file_bytes, mime_type):
    encoded = base64.b64encode(file_bytes).decode()
    await db["file_sessions"].update_one(
        {"user_id": user_id, "session_id": session_id, "filename": filename},
        {"$set": {
            "user_id": user_id, "session_id": session_id,
            "filename": filename, "content": encoded,
            "mime_type": mime_type, "encoding": "base64",
            "uploaded_at": datetime.utcnow(),
        }},
        upsert=True
    )


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    user_id = payload.get("user_id")
    session_id = payload.get("session_id", "default")

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"status": "error", "reason": "No 'prompt' provided."}
    if not user_id:
        return {"status": "error", "reason": "User ID not available."}

    width = int(args.get("width") or 1024)
    height = int(args.get("height") or 1024)
    model = args.get("model") or "flux"
    seed = args.get("seed")
    filename = (args.get("filename") or f"{_slugify(prompt)}.jpg").strip()

    params = {
        "width": width,
        "height": height,
        "model": model,
        "nologo": "true",
    }
    if seed is not None:
        params["seed"] = str(seed)

    api_key = os.getenv("POLLINATIONS_API_KEY")
    if api_key:
        params["key"] = api_key

    url = f"{POLLINATIONS_BASE}/{quote(prompt)}"

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 429:
                return {
                    "status": "error",
                    "reason": "Rate-limited by Pollinations (shared free-tier IP limit). "
                              "Set POLLINATIONS_API_KEY (free, no card — get one at enter.pollinations.ai) to remove this.",
                }
            if resp.status_code != 200:
                return {"status": "error", "reason": f"Pollinations returned HTTP {resp.status_code}: {resp.text[:300]}"}

            content_type = resp.headers.get("content-type", "")
            if "image" not in content_type:
                return {"status": "error", "reason": f"Response wasn't an image (content-type: {content_type}) — prompt may have been blocked by Pollinations' own safety filter."}

            image_bytes = resp.content
    except httpx.TimeoutException:
        return {"status": "timeout", "reason": "Pollinations took too long to respond (>60s)."}
    except Exception as e:
        logger.error(f"image_generation error: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}

    try:
        from bootstrap import get
        db = get("db")
        mime = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"
        await _store_binary(db, user_id, session_id, filename, image_bytes, mime)
    except Exception as e:
        return {"status": "error", "reason": f"Image generated but failed to store: {e}"}

    download_url = _serve_url(filename, session_id)
    return {
        "status": "success",
        "filename": filename,
        "prompt": prompt,
        "model": model,
        "size_bytes": len(image_bytes),
        "download_url": download_url,
        "message": f"✅ Image ready\n📥 {download_url}",
    }
