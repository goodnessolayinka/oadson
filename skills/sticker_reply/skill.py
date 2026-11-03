"""
skills/sticker_reply/skill.py — Send WhatsApp stickers from your personal pack.

Stickers are saved to the `sticker_pack` collection when you send them
to OADSON (handled in api/whatsapp.py's stickerMessage branch).

This skill picks the right sticker and returns a download_url that
_deliver_skill_attachment() in whatsapp.py picks up and sends via
GreenAPI sendFileByUrl — same pattern as voice_reply and image_generation.

Gemini decides when to use this skill — it can choose sticker over text
when the moment calls for it (after a joke, a warm message, etc.).
"""

import os
import logging
import random
from datetime import datetime, timezone

logger = logging.getLogger("oadson.skill.sticker_reply")

BACKEND_URL = os.getenv(
    "BACKEND_URL",
    os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000")
).rstrip("/")


def _serve_url(filename: str, session_id: str) -> str:
    return f"{BACKEND_URL}/api/v3/files/serve/{session_id}/{filename}"


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    user_id = payload.get("user_id")
    session_id = payload.get("session_id", "default")
    db = payload.get("db")

    if not user_id or db is None:
        return {"status": "error", "reason": "Missing user_id or db"}

    action = args.get("action", "send")

    # ── LIST ──
    if action == "list":
        cursor = db["sticker_pack"].find(
            {"user_id": user_id},
            {"_id": 0, "sticker_id": 1, "emoji_hint": 1, "times_sent": 1,
             "saved_at": 1, "filename": 1}
        ).sort("times_sent", -1)
        stickers = [doc async for doc in cursor]
        if not stickers:
            return {
                "status": "empty",
                "message": "No stickers saved yet. Send me a sticker on WhatsApp and I'll remember it!",
            }
        return {
            "status": "ok",
            "count": len(stickers),
            "stickers": stickers,
        }

    # ── SEND ──
    sticker_id = (args.get("sticker_id") or "").strip()
    emoji_hint = (args.get("emoji_hint") or "").strip().lower()

    # 1. Try specific sticker_id first
    sticker = None
    if sticker_id:
        sticker = await db["sticker_pack"].find_one(
            {"user_id": user_id, "sticker_id": sticker_id}
        )
        # BUG FIXED 2026-08-14: this lookup had no download_url check,
        # unlike the emoji_hint search and random fallback below (both
        # filter on download_url existing/non-empty). A sticker_id that
        # matched a doc with no usable download_url would fall through
        # all the way to the final return with download_url missing —
        # producing a "success" status with no actual URL in it, which
        # engineer.py's test-send then reported as
        # "no download_url in skill_output" despite status=success.
        # Reject it here so the id-lookup path has the same requirement
        # as every other selection path, and falls through to search/
        # random instead of silently succeeding with nothing to send.
        if sticker and not sticker.get("download_url"):
            logger.warning(f"sticker_id={sticker_id} matched but has no download_url — falling back")
            sticker = None

    # 2. Try emoji_hint semantic search
    if not sticker and emoji_hint:
        try:
            from memory.vault import _embed_one, _cosine, _EMBED_DIMS
            query_vec = await _embed_one(emoji_hint)
        except Exception:
            query_vec = None

        if query_vec:
            # Score all embedded stickers that have a valid download_url
            # BUG FIXED 2026-08-14: {"$nin": ["", None]} is correct here
            # (a single key, one operator, two excluded values) — this
            # query was already right. The broken one was the random
            # fallback below.
            cursor = db["sticker_pack"].find(
                {"user_id": user_id, "embedding": {"$exists": True},
                 "download_url": {"$exists": True, "$nin": ["", None]}},
            )
            candidates = []
            async for doc in cursor:
                vec = doc.get("embedding")
                if vec and len(vec) == _EMBED_DIMS:
                    score = _cosine(query_vec, vec)
                    candidates.append((score, doc))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                # Pick from top 3 matches so it's not always the same one
                top = candidates[:3]
                sticker = random.choice([d for _, d in top])
        else:
            # Fallback: regex if no embedding available
            cursor = db["sticker_pack"].find(
                {"user_id": user_id, "emoji_hint": {"$regex": emoji_hint, "$options": "i"}}
            ).sort("times_sent", -1).limit(5)
            candidates = [doc async for doc in cursor]
            if candidates:
                sticker = random.choice(candidates)

    # 3. Fall back to random — only from docs that have a download_url
    if not sticker:
        # BUG FIXED 2026-08-14: this was {"$exists": True, "$ne": "",
        # "$ne": None} — a Python dict literal, so the two "$ne" keys
        # collided and only the SECOND one ("$ne": None) survived; the
        # "$ne": "" was silently discarded. That meant this query never
        # actually excluded stickers with an empty-string download_url,
        # only ones missing the field entirely — so it could (and did)
        # pick a doc with download_url="" and return it as a "success"
        # with nothing usable, which is exactly the
        # "no download_url in skill_output" failure being reported
        # from test-send even after the sticker_id-path fix. $nin with
        # a list has a single key, so both exclusions actually apply.
        cursor = db["sticker_pack"].find(
            {"user_id": user_id, "download_url": {"$exists": True, "$nin": ["", None]}}
        ).sort("saved_at", -1).limit(20)
        all_stickers = [doc async for doc in cursor]
        if not all_stickers:
            return {
                "status": "empty",
                "message": "No stickers with valid URLs in your pack yet. Send me some stickers on WhatsApp!",
            }
        sticker = random.choice(all_stickers)

    # Build the serve URL from the stored session/filename
    filename = sticker.get("filename", "")
    stored_session = sticker.get("session_id", session_id)
    download_url = sticker.get("download_url") or (_serve_url(filename, stored_session) if filename else "")

    # SAFETY NET ADDED 2026-08-14: whatever path got us here, never
    # return status=success with an empty download_url — that shape is
    # what produced "no download_url in skill_output" downstream despite
    # a success status. Fail loudly and explicitly instead.
    if not download_url:
        return {
            "status": "error",
            "reason": "Selected sticker has no usable download_url or filename.",
        }

    # Increment times_sent counter
    await db["sticker_pack"].update_one(
        {"_id": sticker["_id"]},
        {"$inc": {"times_sent": 1}, "$set": {"last_sent_at": datetime.now(timezone.utc)}}
    )

    return {
        "status": "success",
        "sticker_id": sticker.get("sticker_id"),
        "emoji_hint": sticker.get("emoji_hint", ""),
        "filename": filename,
        "download_url": download_url,
        "mime_type": "image/webp",
        "message": f"Sticker ready — {sticker.get('emoji_hint', 'no hint')}",
    }
