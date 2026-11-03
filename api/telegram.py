"""
api/telegram.py — OADSON V3 Telegram Stub.

Same pattern as WhatsApp. Different ingress.
Telegram Bot API sends updates to this webhook.
Orchestrator handles it with surface="telegram".

To activate:
  1. Create bot via @BotFather
  2. Set webhook: https://api.telegram.org/bot<TOKEN>/setWebhook?url=<BACKEND>/api/v3/telegram/webhook
  3. Set TELEGRAM_BOT_TOKEN in env vars
"""

import os
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends, Header
from dependency import get_db

logger = logging.getLogger("oadson.api.telegram")

router = APIRouter()

# In-memory fallback ONLY — see api/whatsapp.py's identical pattern for
# why. The durable flag lives in db.bridge_settings.
_telegram_enabled = False   # Off by default. Enable explicitly.


def _verify_engineer_token(token: Optional[str]) -> None:
    expected = os.getenv("OADSON_ENGINEER_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Engineer Mode not configured. Set OADSON_ENGINEER_TOKEN.")
    if not token or token != expected:
        raise HTTPException(403, "Invalid engineer token.")


async def _get_telegram_enabled(db) -> bool:
    global _telegram_enabled
    try:
        doc = await db["bridge_settings"].find_one({"_id": "telegram_enabled"})
        if doc is not None:
            _telegram_enabled = bool(doc.get("value", False))
    except Exception as e:
        logger.warning(f"Could not read telegram_enabled from DB, using in-memory value: {e}")
    return _telegram_enabled


async def _set_telegram_enabled(db, value: bool) -> None:
    global _telegram_enabled
    _telegram_enabled = value
    await db["bridge_settings"].update_one(
        {"_id": "telegram_enabled"}, {"$set": {"value": value}}, upsert=True,
    )


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request, db=Depends(get_db)):
    if not await _get_telegram_enabled(db):
        return {"status": "disabled"}

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise HTTPException(500, "TELEGRAM_BOT_TOKEN not configured.")

    payload = await request.json()

    # Extract message
    message = payload.get("message", {})
    text = message.get("text", "").strip()
    chat_id = str(message.get("chat", {}).get("id", ""))

    # LOCATION SUPPORT 2026-08-26 — "add a location feature". A location
    # share arrives with NO "text" field at all (per Telegram Bot API's
    # Location object) — the guard right below this used to drop any
    # message without text unconditionally, so sharing location here
    # silently did nothing, not even an error. Converted into the same
    # synthetic-text convention used for WhatsApp locationMessage (see
    # api/whatsapp.py) so the model treats "the user just shared their
    # location" like any other message — it'll have just asked for this
    # in the previous turn (visible in its own conversation history) and
    # can naturally continue, no separate pending-request tracking
    # needed. skills/user_location/skill.py turns the coordinates into
    # something readable once the model calls it with them.
    location = message.get("location")
    if not text and location and location.get("latitude") is not None and location.get("longitude") is not None:
        text = f"My current location: latitude {location['latitude']}, longitude {location['longitude']}"

    if not text or not chat_id:
        return {"status": "ignored"}

    user = await db["users"].find_one({})
    if not user:
        raise HTTPException(500, "No user configured.")

    from bootstrap import get
    orchestrator = get("orchestrator")

    result = await orchestrator.run(
        objective=text,
        user=user,
        session_id=f"telegram_{chat_id}",
        surface="telegram",
    )

    response_text = result.get("response", "")

    # Send reply via Telegram Bot API
    import httpx
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": response_text},
        )

    await db["telegram_log"].insert_one({
        "chat_id": chat_id,
        "message": text,
        "response": response_text,
        "timestamp": datetime.utcnow(),
    })

    return {"status": "ok"}


@router.post("/telegram/enable")
async def enable_telegram(
    db=Depends(get_db),
    x_engineer_token: Optional[str] = Header(None, alias="x-engineer-token"),
):
    _verify_engineer_token(x_engineer_token)
    await _set_telegram_enabled(db, True)
    return {"status": "enabled"}


@router.post("/telegram/disable")
async def disable_telegram(
    db=Depends(get_db),
    x_engineer_token: Optional[str] = Header(None, alias="x-engineer-token"),
):
    _verify_engineer_token(x_engineer_token)
    await _set_telegram_enabled(db, False)
    return {"status": "disabled"}
