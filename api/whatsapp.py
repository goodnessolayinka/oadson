"""
api/whatsapp.py — OADSON V3 WhatsApp via Green API.

Rebuilt on V2's proven pattern (whatsapp_router.py) — the key fix:
images are ACTUALLY DOWNLOADED and base64-encoded, then passed as
real inline image data to the orchestrator/provider_manager. V3's
previous version just pasted the image URL as text into the prompt,
which caused Gemini to hallucinate plausible-sounding image content
instead of ever looking at real pixels.

Flow per image:
  1. Green API webhook gives a downloadUrl (often stale/expired)
  2. Call Green API's own /downloadFile endpoint with idMessage to
     get a FRESH download URL
  3. Fetch actual bytes from that fresh URL
  4. Base64-encode and pass through as real image data

Multi-image messages are batched (Green API fires one webhook per
image) using the same 3.5s window pattern as V2.
"""

import os
import re
import base64
import asyncio
import hashlib
import hmac
import time
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends, Header
from pydantic import BaseModel
import httpx

from dependency import get_db
from core.oadson_home import get_user_home, is_safe_path, save_to_sandbox
from core.inline_storage import save_inline

logger = logging.getLogger("oadson.api.whatsapp")

router = APIRouter()


class _WATimer:
    """
    Per-message stage timer. ADDED 2026-09-19 after WhatsApp reply times were
    reported varying from ~1 minute to ~15 minutes with NOTHING in the logs to
    say where the time went — the webhook handler had no timing at all.

    Every line is tagged with the message id and prints both the time since
    the previous stage (+Ns) and since the webhook arrived (total Ns), so one
    grep on the id reconstructs the whole timeline:

        WA[3EB0..] webhook received                       total 0.00s
        WA[3EB0..] session resolved      (+0.04s)         total 0.04s
        WA[3EB0..] orchestrator returned (+412.30s)       total 412.34s   <- the slow part
        WA[3EB0..] reply sent            (+0.9s)          total 413.24s
    """
    def __init__(self, msg_id: str):
        self.tag = f"WA[{(msg_id or 'no-id')[:12]}]"
        self.t0 = time.monotonic()
        self.last = self.t0

    def mark(self, stage: str, **extra) -> float:
        now = time.monotonic()
        step, total = now - self.last, now - self.t0
        self.last = now
        detail = " ".join(f"{k}={v}" for k, v in extra.items())
        logger.info(f"{self.tag} {stage:<28} +{step:7.2f}s  total {total:7.2f}s  {detail}".rstrip())
        return total


# In-memory fallback ONLY — used if the DB read below fails. The real,
# durable flag lives in db.bridge_settings (see _get_whatsapp_enabled /
# _set_whatsapp_enabled). Previously this in-memory global WAS the only
# state, which meant it silently reset to True on every Render redeploy
# regardless of whether you'd disabled the bridge, and there was no auth
# on the endpoints that flip it — anyone who found the URL could toggle
# your WhatsApp bridge.
_whatsapp_enabled = True

BATCH_WINDOW_SECS = 5.0
_media_buffer: dict = {}     # chat_id -> list of {image_data, image_mime, caption}
_batch_timers: dict = {}     # chat_id -> asyncio TimerHandle
# ADDED 2026-09-01 alongside the "typing starts immediately on image
# receipt" fix — tracks the early typing task per chat_id so
# _flush_media_batch (which runs later, once the batch window closes)
# can cancel THIS SAME task instead of spawning a second, independent
# send_typing() loop that would never get cancelled (its own local
# typing_task variable there only ever tracks whatever IT starts, not
# an earlier one started elsewhere) and would keep pinging forever
# until the whole process context ended.
_early_typing_tasks: dict = {}     # chat_id -> asyncio Task

GREEN_API_INSTANCE = os.getenv("GREEN_API_INSTANCE_ID", "")
GREEN_API_TOKEN = os.getenv("GREEN_API_TOKEN", "")
_prefix = GREEN_API_INSTANCE[:4] if GREEN_API_INSTANCE else "7107"
GREEN_API_BASE = f"https://{_prefix}.api.greenapi.com/waInstance{GREEN_API_INSTANCE}"


def _verify_engineer_token(token: Optional[str]) -> None:
    """Same gate as api/engineer.py — reused here so flipping the bridge
    on/off requires the same credential as every other admin action."""
    expected = os.getenv("OADSON_ENGINEER_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Engineer Mode not configured. Set OADSON_ENGINEER_TOKEN.")
    if not token or token != expected:
        raise HTTPException(403, "Invalid engineer token.")


async def _get_whatsapp_enabled(db) -> bool:
    """Reads the durable flag from Mongo; falls back to the in-memory
    default only if no doc exists yet or the read itself fails."""
    global _whatsapp_enabled
    try:
        doc = await db["bridge_settings"].find_one({"_id": "whatsapp_enabled"})
        if doc is not None:
            _whatsapp_enabled = bool(doc.get("value", True))
    except Exception as e:
        logger.warning(f"Could not read whatsapp_enabled from DB, using in-memory value: {e}")
    return _whatsapp_enabled


async def _set_whatsapp_enabled(db, value: bool) -> None:
    global _whatsapp_enabled
    _whatsapp_enabled = value
    await db["bridge_settings"].update_one(
        {"_id": "whatsapp_enabled"}, {"$set": {"value": value}}, upsert=True,
    )


# ── KILL SWITCH ──

@router.post("/whatsapp/disable")
async def disable_whatsapp(
    db=Depends(get_db),
    x_engineer_token: Optional[str] = Header(None, alias="x-engineer-token"),
):
    _verify_engineer_token(x_engineer_token)
    await _set_whatsapp_enabled(db, False)
    logger.warning("WhatsApp bridge DISABLED.")
    return {"status": "disabled"}


@router.post("/whatsapp/enable")
async def enable_whatsapp(
    db=Depends(get_db),
    x_engineer_token: Optional[str] = Header(None, alias="x-engineer-token"),
):
    _verify_engineer_token(x_engineer_token)
    await _set_whatsapp_enabled(db, True)
    return {"status": "enabled"}


@router.get("/whatsapp/status")
async def whatsapp_status(db=Depends(get_db)):
    enabled = await _get_whatsapp_enabled(db)
    if not GREEN_API_TOKEN:
        return {"whatsapp_enabled": enabled, "connected": False}
    try:
        url = f"{GREEN_API_BASE}/getStateInstance/{GREEN_API_TOKEN}"
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(url)
        data = res.json()
        state = data.get("stateInstance", "unknown")
        return {"whatsapp_enabled": enabled, "connected": state == "authorized", "state": state}
    except Exception as e:
        return {"whatsapp_enabled": enabled, "connected": False, "error": str(e)}


# ── GREEN API SEND HELPERS ──

def _extract_code_blocks(text: str):
    blocks = []
    counter = [0]

    def replacer(m):
        lang = (m.group(1) or "").strip() or "code"
        code = m.group(2).strip()
        blocks.append((lang, code))
        counter[0] += 1
        return f"[Code block {counter[0]} below]"

    clean = re.sub(r"```([a-zA-Z+#-]*)\n([\s\S]*?)```", replacer, text)
    return clean, blocks


async def _send_raw(chat_id: str, text: str, quoted_message_id: str = None) -> None:
    if not GREEN_API_TOKEN:
        logger.warning("GREEN_API_TOKEN not set — cannot send reply.")
        return
    try:
        url = f"{GREEN_API_BASE}/sendMessage/{GREEN_API_TOKEN}"
        body = {"chatId": chat_id, "message": text}
        if quoted_message_id:
            body["quotedMessageId"] = quoted_message_id
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(url, json=body)
    except Exception as e:
        logger.error(f"Failed to send WhatsApp reply: {e}")


async def send_whatsapp_reply(chat_id: str, text: str, quoted_message_id: str = None) -> None:
    """
    Send reply. Code blocks sent as separate messages for easy copying.

    quoted_message_id: Added 2026-08-08. If set, the FIRST message sent
    (the primary reply) shows in WhatsApp as a native quote-reply to
    that specific message — the little indented preview bubble showing
    which message this is answering. Only applied to the first chunk;
    follow-up chunks (overflow text, code blocks) aren't quoted, since
    quoting every fragment of a multi-part reply would be visual noise,
    not clarity — the first bubble establishing "this is what I'm
    replying to" is enough context for the whole thread that follows.
    """
    clean_text, code_blocks = _extract_code_blocks(text)

    clean_text = clean_text.strip()
    first_chunk = True
    if clean_text:
        for i in range(0, len(clean_text), 3000):
            await _send_raw(chat_id, clean_text[i:i + 3000], quoted_message_id if first_chunk else None)
            first_chunk = False

    for i, (lang, code) in enumerate(code_blocks, 1):
        label = lang.upper() if lang and lang != "code" else "CODE"
        await _send_raw(chat_id, f"{label} — Block {i} (long-press to copy)", quoted_message_id if first_chunk else None)
        first_chunk = False
        body = "```\n" + code + "\n```"
        for j in range(0, len(body), 3000):
            await _send_raw(chat_id, body[j:j + 3000])


async def _deliver_skill_attachment(chat_id: str, skill_output: dict, quoted_message_id: str = None) -> dict:
    """
    Deliver a skill's file (any result with a download_url) to WhatsApp as a
    native attachment, and return a dict so failures are diagnosable:
    {delivered, reason, status_code, body, endpoint}.

    Stickers (webp) keep GreenAPI's sendSticker endpoint, which takes a URL.
    Every other file is downloaded here and uploaded to sendFileByUpload as
    multipart/form-data, so GreenAPI never has to fetch the file itself. That
    is what makes it arrive as a real attachment instead of a link to tap.
    """
    if not GREEN_API_TOKEN:
        return {"delivered": False, "reason": "GREEN_API_TOKEN not set", "status_code": None, "body": None}
    data = skill_output.get("data") or skill_output
    download_url = data.get("download_url")
    filename = data.get("filename") or "file"
    mime_type = data.get("mime_type", "")
    if not download_url:
        return {"delivered": False, "reason": "no download_url in skill_output", "status_code": None, "body": None}

    is_sticker = mime_type == "image/webp" or filename.endswith(".webp")
    if is_sticker:
        endpoint = "sendSticker"
        url = f"{GREEN_API_BASE}/sendSticker/{GREEN_API_TOKEN}"
        body = {"chatId": chat_id, "urlFile": download_url, "fileName": filename}
        if quoted_message_id:
            body["quotedMessageId"] = quoted_message_id
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=body)
        except Exception as e:
            logger.error(f"Failed to deliver sticker: {e}")
            return {"delivered": False, "reason": str(e), "status_code": None, "body": None, "endpoint": endpoint}
    else:
        endpoint = "sendFileByUpload"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                file_res = await client.get(download_url)
                if file_res.status_code != 200 or not file_res.content:
                    return {
                        "delivered": False,
                        "reason": f"could not fetch file bytes (HTTP {file_res.status_code})",
                        "status_code": file_res.status_code,
                        "body": None,
                        "endpoint": endpoint,
                    }
                url = f"{GREEN_API_BASE}/sendFileByUpload/{GREEN_API_TOKEN}"
                form = {"chatId": chat_id}
                if quoted_message_id:
                    form["quotedMessageId"] = quoted_message_id
                resp = await client.post(
                    url,
                    data=form,
                    files={"file": (filename, file_res.content, mime_type or "application/octet-stream")},
                )
        except Exception as e:
            logger.error(f"Failed to upload skill file: {e}")
            return {"delivered": False, "reason": str(e), "status_code": None, "body": None, "endpoint": endpoint}

    # GreenAPI can return 200 with an error payload for validation failures,
    # so capture the body either way.
    try:
        resp_body = resp.json()
    except Exception:
        resp_body = resp.text[:500]

    if resp.status_code != 200:
        logger.error(f"{endpoint} failed ({resp.status_code}): {str(resp_body)[:300]}")
        return {
            "delivered": False,
            "reason": f"GreenAPI returned {resp.status_code}",
            "status_code": resp.status_code,
            "body": resp_body,
            "endpoint": endpoint,
        }

    return {
        "delivered": True,
        "reason": None,
        "status_code": resp.status_code,
        "body": resp_body,
        "endpoint": endpoint,
    }


async def _ping_typing(chat_id: str) -> None:
    """
    Fixed 2026-08-06: this was calling /showTyping and, on failure,
    /sendChatAction — NEITHER is a real Green API endpoint, which is
    exactly why this was 404ing every time (confirmed against Green
    API's own docs: green-api.com/en/docs/api/service/SendTyping/).
    The real method is POST /sendTyping/{token} with a required
    "chatId" and an optional "typingTime" in milliseconds (integer,
    1000-20000; Green API rejects it outside that range with a 400).
    We call it every 8s from send_typing() below, so 12000ms (12s)
    comfortably outlasts each ping without over/under-shooting Green
    API's own 1000-20000 bound.
    """
    if not GREEN_API_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(
                f"{GREEN_API_BASE}/sendTyping/{GREEN_API_TOKEN}",
                json={"chatId": chat_id, "typingTime": 12000},
            )
    except Exception:
        pass


async def send_typing(chat_id: str) -> None:
    try:
        while True:
            await _ping_typing(chat_id)
            await asyncio.sleep(8)
    except asyncio.CancelledError:
        pass


def _new_whatsapp_session_id(chat_id: str) -> str:
    ts = int(datetime.now(timezone.utc).timestamp())
    safe_chat = chat_id.replace("@", "_").replace(".", "_")
    return f"whatsapp_{safe_chat}_{ts}"


async def _get_active_session_id(db, chat_id: str, user_id: str) -> str:
    """
    Get (or create) the active WhatsApp session for this chat.

    Ported from V2's whatsapp_router.py, which had this working correctly
    for real — V3 had no equivalent at all: every message just used one
    single fixed session_id derived from chat_id, forever, with no way to
    start a fresh conversation ("/new") or track a rotating history of
    past ones ("/history"/"/delete"). This is what those commands
    actually operate on now.
    """
    doc = await db["whatsapp_active_session"].find_one({"chat_id": chat_id})
    if doc:
        return doc["active_session_id"]
    session_id = _new_whatsapp_session_id(chat_id)
    await db["whatsapp_active_session"].insert_one({
        "chat_id": chat_id,
        "user_id": user_id,
        "active_session_id": session_id,
        "created_at": datetime.now(timezone.utc),
    })
    return session_id


async def _set_active_session_id(db, chat_id: str, user_id: str, session_id: str) -> None:
    await db["whatsapp_active_session"].update_one(
        {"chat_id": chat_id},
        {"$set": {"active_session_id": session_id, "user_id": user_id, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def _list_whatsapp_sessions(db, user_id: str) -> list:
    """Most recent WhatsApp-originated sessions for this user, newest
    first — queried straight from ai_sessions (V3's real session store),
    not a separate meta collection like V2 used."""
    return await db["ai_sessions"].find({
        "user_id": user_id,
        "session_id": {"$regex": "^whatsapp_"},
    }).sort("updated_at", -1).limit(10).to_list(length=10)


async def _handle_command(cmd: str, arg: str, chat_id: str, user_id: str, db) -> str:
    """
    Real command handlers — actually touch the database, unlike before
    when these strings just got sent to Gemini as ordinary chat text and
    it improvised a response describing an action that never happened.
    """
    if cmd == "/new":
        session_id = _new_whatsapp_session_id(chat_id)
        await _set_active_session_id(db, chat_id, user_id, session_id)
        return "🆕 New chat started. What's on your mind?"

    elif cmd == "/history":
        sessions = await _list_whatsapp_sessions(db, user_id)
        if not sessions:
            return "No WhatsApp session history yet."
        lines = ["Your recent WhatsApp sessions:"]
        for i, s in enumerate(sessions, 1):
            updated = s.get("updated_at")
            when = updated.strftime("%b %d, %H:%M") if hasattr(updated, "strftime") else "unknown time"
            msg_count = len(s.get("messages", []))
            preview = ""
            for m in s.get("messages", []):
                if m.get("role") == "user" and m.get("content"):
                    preview = m["content"][:50]
                    break
            lines.append(f"{i}. {when} — {msg_count} message(s){' — ' + preview if preview else ''}")
        lines.append("\nReply /delete N to remove one (e.g. /delete 1).")
        return "\n".join(lines)

    elif cmd == "/delete":
        arg = arg.strip()
        if not arg.isdigit():
            return "Usage: /delete N — e.g. /delete 1 (run /history first to see numbers)."
        n = int(arg)
        sessions = await _list_whatsapp_sessions(db, user_id)
        if n < 1 or n > len(sessions):
            return f"No session #{n} — you have {len(sessions)} in your /history list."
        target = sessions[n - 1]
        await db["ai_sessions"].delete_one({"_id": target["_id"]})
        # If the deleted session was the active one, clear the pointer so
        # the next message starts a fresh session instead of resurrecting
        # a session_id that no longer has a document behind it.
        active_doc = await db["whatsapp_active_session"].find_one({"chat_id": chat_id})
        if active_doc and active_doc.get("active_session_id") == target.get("session_id"):
            await db["whatsapp_active_session"].delete_one({"chat_id": chat_id})
        return f"🗑 Deleted session #{n}."

    elif cmd == "/help":
        return (
            "Available commands:\n"
            "/new — start a fresh chat\n"
            "/history — list your 10 most recent WhatsApp sessions\n"
            "/delete N — delete session N from that list\n"
            "/help — show this message"
        )

    return f"Unknown command: {cmd}\nSend /help to see available commands."
    """
    Save downloaded bytes into the user's oadson_home sandbox (the same
    directory @terminal and @document_reader already operate in) so a
    document sent over WhatsApp is actually usable by those skills —
    previously there was no path at all from "file arrives on WhatsApp"
    to "file exists somewhere a skill can read it".

    Returns (relative_path, None) on success, or (None, error_message) on
    failure. Never raises — this is called from the webhook, which
    always needs to return 200 to Green API regardless of outcome.
    """
    MAX_DOCUMENT_BYTES = 15 * 1024 * 1024  # 15MB — generous for source
    # files/PDFs/small archives without letting a webhook hold megabytes
    # of arbitrary attacker-controlled data in memory indefinitely.
    if len(raw_bytes) > MAX_DOCUMENT_BYTES:
        return None, f"file too large ({len(raw_bytes) // (1024*1024)}MB, max 15MB)"

    try:
        safe_name = os.path.basename(filename) or "attachment"
        home = get_user_home(user_id)

        # Avoid silently clobbering an existing file with the same name —
        # append a short counter suffix instead.
        candidate = safe_name
        counter = 1
        while (home / candidate).exists():
            stem, dot, ext = safe_name.rpartition(".")
            candidate = f"{stem}_{counter}.{ext}" if dot else f"{safe_name}_{counter}"
            counter += 1

        if not is_safe_path(user_id, candidate):
            return None, "invalid filename"

        target = home / candidate
        target.write_bytes(raw_bytes)
        return candidate, None
    except Exception as e:
        logger.error(f"Failed to save {filename} to sandbox: {e}")
        return None, str(e)


# ── REAL FILE DOWNLOAD (generalized from the image-only fix below) ──

async def _download_file(chat_id: str, msg_id: str, fallback_url: str) -> tuple[Optional[bytes], Optional[str]]:
    """
    Download actual file bytes and return (raw_bytes, mime_type).
    Returns (None, None) if download fails at every step.

    Same real-download pattern as _download_image below (Green API's
    downloadUrl in the webhook payload is often stale/expired — always
    ask Green API's own /downloadFile for a fresh one first), just
    without forcing an image/* mime type. Used for documentMessage
    (see whatsapp_webhook) — previously there was no equivalent for
    non-image attachments at all, which is why documents silently did
    nothing when sent to the bot.
    """
    try:
        fresh_url = None
        if msg_id:
            dl_url = f"{GREEN_API_BASE}/downloadFile/{GREEN_API_TOKEN}"
            async with httpx.AsyncClient(timeout=10) as client:
                dl_res = await client.post(dl_url, json={"chatId": chat_id, "idMessage": msg_id})
                if dl_res.status_code == 200:
                    dl_json = dl_res.json()
                    fresh_url = dl_json.get("downloadUrl") or dl_json.get("url")

        fetch_url = fresh_url or fallback_url
        if not fetch_url:
            return None, None

        # NOTE 2026-09-01: kept at 60s here (not tightened to 20s like
        # _download_image above) — a document attachment (PDF, zip, etc)
        # is legitimately expected to be larger than an image and take
        # longer; see _download_image's own comment for why ITS timeout
        # needed tightening specifically (the multi-image sequential-
        # download compounding problem doesn't apply the same way to a
        # single document attachment).
        async with httpx.AsyncClient(timeout=60) as client:
            raw = await client.get(fetch_url)
            if raw.status_code != 200 or not raw.content:
                logger.warning(f"File fetch failed: HTTP {raw.status_code}")
                return None, None

            mime = raw.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
            logger.info(f"File downloaded: {len(raw.content)} bytes, mime={mime}")
            return raw.content, mime

    except Exception as e:
        logger.error(f"File download error: {e}")
        return None, None


# ── REAL IMAGE DOWNLOAD (the actual fix) ──

async def _download_image(chat_id: str, msg_id: str, fallback_url: str) -> tuple[Optional[str], Optional[str]]:
    """
    Download actual image bytes and return (base64_data, mime_type).
    Returns (None, None) if download fails at every step — caller
    must then be honest that no image is available, never hallucinate.

    TIMEOUTS TIGHTENED 2026-09-01: were 30s (Green API metadata call) +
    60s (actual file fetch) = up to 90s worst case per image, with
    ZERO user-facing feedback during that wait (no typing indicator
    fires until AFTER every image in a multi-image message has already
    finished downloading — see whatsapp_webhook's imageMessage branch
    and _flush_media_batch). Confirmed as the real cause of "a WhatsApp
    message with attachments takes 3+ minutes": 4 images arrive as 4
    separate webhook deliveries, each running its OWN full download
    sequentially (Green API doesn't batch multiple images into one
    webhook call), so 4 x up to 90s worst-case genuinely adds up to
    several real minutes of total silence before anything visible
    happens. A CDN media fetch that's actually healthy should complete
    in a few seconds, not tens of seconds — these timeouts were sized
    for "never time out on anything," not "fail fast on something that
    is genuinely stuck," which on the multi-image path multiplies the
    cost of exactly the case this was meant to protect against.
    """
    try:
        fresh_url = None
        if msg_id:
            dl_url = f"{GREEN_API_BASE}/downloadFile/{GREEN_API_TOKEN}"
            async with httpx.AsyncClient(timeout=10) as client:
                dl_res = await client.post(dl_url, json={"chatId": chat_id, "idMessage": msg_id})
                if dl_res.status_code == 200:
                    dl_json = dl_res.json()
                    fresh_url = dl_json.get("downloadUrl") or dl_json.get("url")

        fetch_url = fresh_url or fallback_url
        if not fetch_url:
            return None, None

        async with httpx.AsyncClient(timeout=20) as client:
            raw = await client.get(fetch_url)
            if raw.status_code != 200 or not raw.content:
                logger.warning(f"Image fetch failed: HTTP {raw.status_code}")
                return None, None

            mime = raw.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            if not mime.startswith("image/"):
                mime = "image/jpeg"
            b64_data = base64.b64encode(raw.content).decode()
            logger.info(f"Image downloaded: {len(raw.content)} bytes, mime={mime}")
            return b64_data, mime

    except Exception as e:
        logger.error(f"Image download error: {e}")
        return None, None


# ── WEBHOOK ──

@router.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request, db=Depends(get_db)):
    if not await _get_whatsapp_enabled(db):
        return {"status": "disabled"}

    # ── WEBHOOK TOKEN CHECK (fixed to match how Green API actually
    # authenticates) ──
    # Checked BEFORE trusting anything in the JSON body — this is what
    # proves the request genuinely came from Green API's servers, rather
    # than someone who found this URL and POSTed a hand-crafted payload
    # with sender_phone forged to match the owner number, which the
    # allowlist check below can't catch on its own since it only looks
    # at fields inside a body an attacker fully controls.
    #
    # Was checking an "x-oadson-signature" HMAC header — Green API has no
    # such mechanism. Per their docs (green-api.com/docs, "Webhook
    # Endpoint technology"): if you set a `webhookUrlToken` in your
    # instance settings, EVERY webhook call arrives with a plain
    # `Authorization: Bearer <token>` header (or `Basic <token>` if you
    # chose that mode) carrying that exact value — not a computed
    # signature of the body. The old check would never have matched
    # anything Green API actually sends, secret set or not.
    #
    # Set WHATSAPP_BRIDGE_SECRET here to the SAME value you configure as
    # webhookUrlToken in Green API's console/SetSettings — that's what
    # makes this check meaningful. Fails CLOSED if unset, same reasoning
    # as the owner-number check below: an unconfigured secret should stop
    # everything, not silently accept everything.
    bridge_secret = os.getenv("WHATSAPP_BRIDGE_SECRET", "")
    if not bridge_secret:
        logger.error("WHATSAPP_BRIDGE_SECRET is not set — refusing all WhatsApp webhook calls until it is, to fail closed rather than accept unauthenticated requests.")
        return {"status": "ignored", "reason": "bridge_secret_not_configured"}

    auth_header = request.headers.get("authorization", "")
    token_from_header = auth_header.split(" ", 1)[-1] if " " in auth_header else auth_header
    if not hmac.compare_digest(token_from_header, bridge_secret):
        logger.warning("Rejected WhatsApp webhook call with invalid/missing Authorization token.")
        raise HTTPException(401, "Invalid webhook token.")

    try:
        payload = await request.json()
    except Exception:
        return {"status": "ok"}

    webhook_type = payload.get("typeWebhook", "")
    if webhook_type != "incomingMessageReceived":
        return {"status": "ignored", "type": webhook_type}

    # Timestamp Green API says the message was sent, vs. when we received it.
    # A large gap here means the delay is UPSTREAM of this server entirely
    # (Green API queueing / webhook redelivery), not anything in this code.
    _wa_sent_ts = payload.get("timestamp")
    _wa_lag = None
    if isinstance(_wa_sent_ts, (int, float)):
        _wa_lag = round(time.time() - _wa_sent_ts, 1)

    sender_data = payload.get("senderData", {})
    message_data = payload.get("messageData", {})
    chat_id = sender_data.get("chatId", "")
    # BUG FIXED 2026-08-14: WhatsApp is rolling out "@lid" (linked ID) as
    # a replacement identity format for "@c.us" (phone-number-based) on
    # some accounts/sessions. When that happens, GreenAPI sends sender/
    # chatId as e.g. "147983735738378@lid" instead of
    # "XXXXXXXXXXX@c.us" — same human, same WhatsApp account, but a
    # completely different string. The old .replace("@c.us", "") did
    # nothing for an "@lid" value, so sender_phone stayed as the full
    # "147983735738378@lid" string, which could never digit-match
    # WHATSAPP_OWNER_NUMBER below — every message from the owner was
    # silently rejected as "unauthorized_sender" the moment WhatsApp
    # switched formats. Stripping "@lid" too doesn't fix it alone (the
    # numeric lid isn't the phone number, it's a different identifier),
    # so the owner check below now also accepts a pinned lid value from
    # WHATSAPP_OWNER_LID as a second valid identity for the same person.
    sender_phone = sender_data.get("sender", "").replace("@c.us", "").replace("@lid", "")
    sender_is_lid = sender_data.get("sender", "").endswith("@lid")
    msg_id = payload.get("idMessage", "") or message_data.get("idMessage", "")
    type_message = message_data.get("typeMessage", "")

    # ── Quoted-message context ──
    # Added 2026-08-08. When the user long-presses a message and hits
    # Reply, GreenAPI sets typeMessage="quotedMessage" and includes a
    # "quotedMessage" object with the ORIGINAL message being replied to
    # (its own typeMessage/textMessage/etc — confirmed against GreenAPI's
    # own webhook docs). Without this, OADSON never knew "this new
    # message is specifically about that earlier one" — it just saw
    # the new text in isolation, same as any other message, even in a
    # fast-moving chat where three different topics might be active at
    # once. Captured here as quoted_stanza_id (used below to make
    # OADSON's OWN reply a native quote-reply back) and quoted_text
    # (folded into the text sent to the orchestrator, so the model has
    # the actual context, not just "user replied to something").
    # ── QUOTED MESSAGE CONTEXT ──
    # If user quoted something, fold it into the text so the model sees
    # what they were replying to.
    #
    # Special case: if the quoted stanzaId matches the last proactive
    # message OADSON sent, this is a direct reply to a proactive message.
    # Inject that context explicitly so OADSON knows the conversation
    # started from its own initiative, not a cold user open.
    quoted_stanza_id = None
    quoted_text = None
    is_proactive_reply = False
    proactive_reply_context = ""

    if type_message == "quotedMessage":
        quoted = message_data.get("quotedMessage", {})
        quoted_stanza_id = quoted.get("stanzaId") or message_data.get("extendedTextMessageData", {}).get("stanzaId")
        quoted_text = (
            quoted.get("textMessage")
            or quoted.get("caption")
            or quoted.get("extendedTextMessageData", {}).get("text")
        )

        # Check if this is a reply to our own proactive message
        if quoted_stanza_id:
            proactive_meta = await db["proactive_meta"].find_one({}) or {}
            last_sent_msg_id = proactive_meta.get("last_sent_msg_id", "")
            if last_sent_msg_id and quoted_stanza_id == last_sent_msg_id:
                is_proactive_reply = True
                last_proactive_msg = proactive_meta.get("last_message", "")
                proactive_reply_context = (
                    f'[This is a reply to your proactive message: "{last_proactive_msg[:200]}"] '
                )
                logger.info(f"Detected reply to proactive message (stanzaId={quoted_stanza_id})")

    if not chat_id:
        return {"status": "ignored", "reason": "no chat_id"}

    _wa = _WATimer(msg_id)
    _wa.mark("webhook received", type=type_message, upstream_lag_s=_wa_lag)

    # ── DEDUPLICATION (the actual fix for the resend storm) ──
    # msg_id was already being extracted above but never checked against
    # anything. Green API redelivers a webhook whenever it doesn't get a
    # timely ack — which this endpoint can easily miss, since a full
    # orchestrator run (skills, review, sometimes a recovery-generation
    # round trip on top) can take longer than its delivery timeout. With
    # no dedup, every redelivery of the SAME message ran the entire
    # pipeline again and sent a brand new reply — compounding the delay
    # on the next ack too, which is why this could spiral into a
    # continuous resend loop instead of just one or two retries.
    #
    # Atomic upsert (not check-then-insert) so two redeliveries landing
    # within milliseconds of each other can't both slip through a race:
    # upserted_id is only set on a genuinely NEW id; a matched-but-not-
    # upserted result means this id was already seen.
    if msg_id:
        dedup_result = await db["whatsapp_processed_messages"].update_one(
            {"msg_id": msg_id},
            {"$setOnInsert": {"msg_id": msg_id, "created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        if dedup_result.upserted_id is None:
            _wa.mark("DUPLICATE redelivery ignored")
            logger.info(f"Ignoring duplicate WhatsApp webhook redelivery for idMessage={msg_id}")
            return {"status": "duplicate", "idMessage": msg_id}
        # NOTE: create a TTL index once, wherever the rest of this app's
        # indexes are set up (the same place that logs "OADSON V3 indexes
        # created" on startup) — e.g.
        #   db["whatsapp_processed_messages"].create_index("created_at", expireAfterSeconds=86400)
        # so this collection doesn't grow forever; Green API won't retry
        # a message that's a day old, so 24h of history is plenty.

    # ── OWNER-ONLY ALLOWLIST (the actual fix for "anyone can chat my
    # number") ──
    # sender_phone was extracted above but NEVER checked against
    # anything — this webhook responded to whatever number messaged the
    # connected WhatsApp Business account, full stop. Anyone who found or
    # guessed the number got the exact same access as the owner: files,
    # memory, every skill. WHATSAPP_OWNER_NUMBER was already set on
    # Render but nothing in this file ever read it.
    #
    # Compares digits-only so formatting differences (+, spaces, leading
    # 00 vs country code) between what Green API sends and what's typed
    # into Render don't cause a false mismatch.
    owner_number_raw = os.getenv("WHATSAPP_OWNER_NUMBER", "")
    if not owner_number_raw:
        logger.error("WHATSAPP_OWNER_NUMBER is not set — refusing all WhatsApp messages until it is, to fail closed rather than stay open to anyone.")
        return {"status": "ignored", "reason": "owner_number_not_configured"}

    def _digits_only(s: str) -> str:
        return re.sub(r"\D", "", s or "")

    # BUG FIXED 2026-08-14: see the sender_phone extraction comment
    # above. A message arriving under the new "@lid" format never
    # matches WHATSAPP_OWNER_NUMBER on digits alone (the lid is not a
    # phone number). WHATSAPP_OWNER_LID pins the specific lid value
    # confirmed to belong to the owner (from GreenAPI's own message
    # export) as a second accepted identity — set it once Render's env
    # vars are updated with the real value. Until then this falls back
    # to the phone-number check only, same as before.
    owner_lid_raw = os.getenv("WHATSAPP_OWNER_LID", "").strip()
    is_owner_by_phone = _digits_only(sender_phone) == _digits_only(owner_number_raw)
    is_owner_by_lid = sender_is_lid and owner_lid_raw and sender_data.get("sender", "") == owner_lid_raw

    if not (is_owner_by_phone or is_owner_by_lid):
        logger.warning(f"Ignored WhatsApp message from non-owner number: {sender_phone}")
        # Deliberately silent — no reply at all. Confirming "you're not
        # authorized" back to an unknown sender still tells them the bot
        # exists and is filtering by number, which is free reconnaissance
        # they shouldn't get.
        return {"status": "ignored", "reason": "unauthorized_sender"}

    # ── OWNER LOOKUP ──
    # Moved up from after media extraction — the documentMessage branch
    # below needs user_id to know which sandboxed oadson_home to save
    # into, and single-user OADSON only ever has the one owner anyway.
    user = await db["users"].find_one({})
    if not user:
        logger.error("No user configured.")
        return {"status": "error"}
    user_id = str(user["_id"])

    # ── TEXT EXTRACTION ──
    text = message_data.get("textMessageData", {}).get("textMessage", "").strip()
    if not text:
        text = message_data.get("extendedTextMessageData", {}).get("text", "").strip()

    # Fold quoted context into what the model actually sees.
    # Proactive replies get their own context prefix so OADSON knows
    # the conversation is continuing from its own initiative.
    if is_proactive_reply:
        text = f"{proactive_reply_context}{text}"
    elif quoted_text and text:
        text = f'[Replying to: "{quoted_text[:200]}"] {text}'

    # ── SLASH COMMANDS (handled directly, never reach Gemini) ──
    # Previously there was no command parsing at all — "/delete 1" etc.
    # went straight into orchestrator.run() as plain chat text, and
    # Gemini just improvised a plausible-sounding "I have deleted..."
    # reply with zero real action behind it. These are now intercepted
    # and handled for real before anything is sent to the model at all —
    # ported from V2's whatsapp_router.py, which had this working
    # correctly, onto V3's actual ai_sessions schema.
    if text.startswith("/"):
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        reply = await _handle_command(cmd, arg, chat_id, user_id, db)
        await send_whatsapp_reply(chat_id, reply)
        return {"status": "ok", "command": cmd}

    # ── IMAGE EXTRACTION ──
    image_data = None
    image_mime = None
    attachment_url = None
    attachment_type = None
    attachment_filename = None

    if type_message == "imageMessage":
        file_data = message_data.get("fileMessageData", {})
        caption = file_data.get("caption", "").strip()
        if caption and not text:
            text = caption

        # FEEDBACK-DURING-WAIT 2026-09-01: previously nothing signaled
        # to the user that their image was even received until AFTER
        # the full download (up to ~90s pre-tightening, still real
        # seconds now) completed AND the 5s batch window passed — total
        # silence the whole time. Fires immediately on receipt instead.
        # Tracked in _early_typing_tasks so _flush_media_batch (which
        # runs once the batch window later closes) cancels THIS task
        # rather than starting a redundant second typing loop — see
        # that dict's own declaration comment for the full reasoning.
        # A second image arriving before the first's task is cleaned up
        # replaces the tracked task rather than stacking a new loop on
        # top of the still-running one.
        old_early_task = _early_typing_tasks.get(chat_id)
        if old_early_task and not old_early_task.done():
            old_early_task.cancel()
        _early_typing_tasks[chat_id] = asyncio.create_task(send_typing(chat_id))

        fallback_url = file_data.get("downloadUrl", "")
        image_data, image_mime = await _download_image(chat_id, msg_id, fallback_url)

        if not image_data:
            # Be honest — never let this fall through to a text-only
            # prompt that could cause hallucinated image content.
            # BUGFIX 2026-09-01: this early return previously left the
            # just-started _early_typing_tasks entry running forever —
            # nothing else would ever reach the cancel in
            # _flush_media_batch (that only runs on a SUCCESSFUL
            # download reaching the batch), so a failed download meant
            # Green API kept getting pinged every 8s indefinitely, for
            # the lifetime of the whole process, not just this request.
            failed_task = _early_typing_tasks.pop(chat_id, None)
            if failed_task and not failed_task.done():
                failed_task.cancel()
            await send_whatsapp_reply(
                chat_id,
                "I couldn't download that image — it may have expired or there was a network issue. "
                "Try sending it again."
            )
            return {"status": "ok", "reason": "image_download_failed"}

        # Also save inline so this image can actually be DISPLAYED later —
        # previously there was no durable copy anywhere, only the
        # ephemeral base64 sent to Gemini for this one call, so a
        # WhatsApp-originated session never showed the image at all when
        # viewed on the website, even after reload. No Cloudinary
        # involved — same inline mechanism everything else now uses.
        try:
            raw_img_bytes = base64.b64decode(image_data)
            attachment_url = await save_inline(db, raw_img_bytes, f"{msg_id or 'image'}.jpg", image_mime, bucket="whatsapp")
            attachment_type = image_mime
            attachment_filename = f"{msg_id or 'image'}.jpg"
        except Exception as e:
            logger.warning(f"Could not save WhatsApp image inline for display: {e}")

    elif type_message == "videoMessage":
        await send_whatsapp_reply(chat_id, "Video isn't supported yet. Send images, documents, or text instead.")
        return {"status": "ok"}

    elif type_message == "locationMessage":
        # LOCATION SUPPORT 2026-08-26 — "add a location feature".
        # WhatsApp location shares arrive as their own message type (per
        # Green-API's own webhook docs) — completely unhandled before
        # this branch existed, so a shared location silently vanished
        # with no reply at all, not even an error.
        #
        # Converted into ordinary-looking text rather than a special
        # marker/schema — deliberate: it lets the model treat "the user
        # just shared their location" exactly like any other message,
        # including noticing (via its own conversation history) that it
        # just asked for this and picking up right where it left off —
        # no bespoke pending-request state machine needed server-side.
        # skills/user_location/skill.py is what actually turns these
        # coordinates into something readable, once the model calls it.
        # BUG FOUND AND FIXED 2026-08-26 — confirmed via GREEN API's own
        # "Incoming location message" docs: the field is
        # "locationMessageData", not "location". I mixed it up with the
        # DIFFERENT nested format GREEN API uses when a location is
        # being QUOTED inside another message (quotedMessage.location —
        # that one really is called "location"), and used that field
        # name here by mistake. Reading a nonexistent key just silently
        # returned an empty dict every time, so lat/lon were always None
        # regardless of what was actually shared — the coordinates were
        # sitting right there in the payload the whole time, just under
        # a different key than the one being read.
        loc = message_data.get("locationMessageData", {}) or {}
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat is None or lon is None:
            await send_whatsapp_reply(chat_id, "I got a location share but couldn't read the coordinates. Try sending it again?")
            return {"status": "ok", "reason": "location_missing_coords"}
        place_name = loc.get("nameLocation") or ""
        text = f"My current location: latitude {lat}, longitude {lon}" + (f" ({place_name})" if place_name else "")

    elif type_message == "stickerMessage":
        # Stickers are WebP images — download and pass to Gemini as an image
        # so OADSON can actually see and react to what was sent.
        file_data = message_data.get("fileMessageData", {})
        fallback_url = file_data.get("downloadUrl", "")

        image_data, image_mime = await _download_image(chat_id, msg_id, fallback_url)
        if not image_data:
            await send_whatsapp_reply(chat_id, "I got your sticker but couldn't load it. Try again?")
            return {"status": "ok", "reason": "sticker_download_failed"}

        if not text:
            text = "[Sticker received]"

        # Save to session for display
        try:
            raw_sticker_bytes = base64.b64decode(image_data)
            sticker_filename = f"{msg_id or 'sticker'}.webp"
            # session_id may not exist in WA webhook scope — use msg_id as key
            _sticker_session = locals().get("session_id") or f"wa_{msg_id}"
            attachment_url = await save_inline(
                db, raw_sticker_bytes, sticker_filename, "image/webp", bucket="whatsapp"
            )
            attachment_type = "image/webp"
            attachment_filename = sticker_filename

            # ── AUTO-SAVE TO STICKER PACK ──
            await db["sticker_pack"].update_one(
                {"user_id": user_id, "sticker_id": msg_id},
                {"$setOnInsert": {
                    "user_id": user_id,
                    "sticker_id": msg_id,
                    "filename": sticker_filename,
                    "session_id": _sticker_session,
                    "download_url": attachment_url,
                    "mime_type": "image/webp",
                    "emoji_hint": "",
                    "times_sent": 0,
                    "saved_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            logger.info(f"Sticker saved to pack: {msg_id}")

            # ── AUTO-LABEL via Gemini vision (fire-and-forget) ──
            # Pass the sticker image to Gemini and ask for a 3-5 word
            # description + relevant emoji. Saves as emoji_hint so
            # sticker_reply can find it by mood/context later.
            # Example: "Ronaldo siuuu celebration 🎉" or "crying laughing 😂"
            # Never blocks the main message flow — runs in background.
            import asyncio as _asyncio

            async def _auto_label_sticker(sticker_b64, sticker_msg_id, uid):
                try:
                    import httpx as _httpx, os as _os
                    gemini_key = _os.getenv("GEMINI_API_KEY", "")
                    thinking_model = _os.getenv("GEMINI_THINKING_MODEL", "gemini-2.5-flash-lite")
                    if not gemini_key or not sticker_b64:
                        return
                    # Use flash-lite for sticker labeling — cheap, fast, vision-capable
                    label_model = thinking_model
                    label_body = {
                        "contents": [{
                            "parts": [
                                {
                                    "inline_data": {
                                        "mime_type": "image/webp",
                                        "data": sticker_b64,
                                    }
                                },
                                {
                                    "text": (
                                        "Describe this sticker in 3-5 words maximum, plus 1-2 relevant emoji. "
                                        "Be specific — if it's a footballer, name them. "
                                        "If it's an expression, name the mood. "
                                        "Format: just the description and emoji, nothing else. "
                                        "Examples: 'Ronaldo siuuu celebration 🎉', 'crying laughing 😂', "
                                        "'Mbappe speed run ⚡', 'shocked face 😱', 'thumbs up 👍'"
                                    )
                                }
                            ]
                        }],
                        "generationConfig": {"maxOutputTokens": 30, "temperature": 0.2},
                    }
                    # SECURITY FIX 2026-08-14: key moved from URL to
                    # x-goog-api-key header — was leaking into Render's
                    # httpx request logs.
                    async with _httpx.AsyncClient(timeout=15) as client:
                        resp = await client.post(
                            f"https://generativelanguage.googleapis.com/v1beta/models/{label_model}:generateContent",
                            json=label_body,
                            headers={"x-goog-api-key": gemini_key},
                        )
                    if resp.status_code != 200:
                        logger.warning(f"Sticker label API error {resp.status_code}: {resp.text[:200]}")
                        return
                    if resp.status_code == 200:
                        data = resp.json()
                        label = (
                            data.get("candidates", [{}])[0]
                            .get("content", {})
                            .get("parts", [{}])[0]
                            .get("text", "")
                            .strip()[:80]
                        )
                        if label:
                            # Embed the label so sticker_reply can find
                            # this sticker by semantic similarity later
                            label_vec = None
                            try:
                                from memory.vault import _embed_one
                                label_vec = await _embed_one(label)
                            except Exception:
                                pass

                            update = {"$set": {"emoji_hint": label}}
                            if label_vec:
                                update["$set"]["embedding"] = label_vec
                            await db["sticker_pack"].update_one(
                                {"user_id": uid, "sticker_id": sticker_msg_id},
                                update,
                            )
                            logger.info(f"Sticker auto-labeled: '{label}' ({sticker_msg_id})")
                except Exception as label_err:
                    logger.warning(f"Sticker auto-label failed: {label_err}")

            _asyncio.create_task(_auto_label_sticker(image_data, msg_id, user_id))
        except Exception as e:
            logger.warning(f"Could not save sticker: {e}")

    elif type_message == "documentMessage":
        # Was completely unhandled — no branch existed for documentMessage
        # at all, so a zip/pdf/md/py sent to the bot fell through every
        # if/elif untouched. `text` stayed empty (caption is nested under
        # fileMessageData, never extracted for non-image types) and
        # image_data stayed None, so this hit the "empty message" guard
        # below and got silently ignored — no reply, no error, nothing.
        # This is the "Oadson on WhatsApp cannot read documents" bug.
        file_data = message_data.get("fileMessageData", {})
        caption = file_data.get("caption", "").strip()
        file_name = file_data.get("fileName") or "attachment"
        fallback_url = file_data.get("downloadUrl", "")

        raw_bytes, mime = await _download_file(chat_id, msg_id, fallback_url)
        if not raw_bytes:
            await send_whatsapp_reply(
                chat_id,
                f"I couldn't download {file_name} — it may have expired or there was a "
                "network issue. Try sending it again."
            )
            return {"status": "ok", "reason": "document_download_failed"}

        # Intentionally does NOT touch Cloudinary anywhere in this path —
        # this is the "normal inline file" fallback: download real bytes
        # directly from Green API and save them straight into the user's
        # own sandboxed workspace, same place @terminal and document_reader
        # already read from. No external upload service in the loop at all.
        # WORKSPACE SPLIT 2026-08-28: explicitly promises @terminal access
        # below (unzip/cat/ls) — must land in workspace/, not the new
        # inputs/ default, since terminal only operates in workspace/.
        saved_path, save_error = save_to_sandbox(user_id, file_name, raw_bytes, area="workspace")
        if save_error:
            await send_whatsapp_reply(chat_id, f"Couldn't save {file_name}: {save_error}")
            return {"status": "ok", "reason": "document_save_failed"}

        # REPO INDEX — a zip sent over WhatsApp is indexed like a web upload, so
        # heavy turns can retrieve from it. Best-effort; never blocks the reply.
        if (file_name or "").lower().endswith(".zip") or "zip" in (mime or ""):
            try:
                from core import repo_index as _ri
                from bootstrap import get as _bget, has as _bhas
                _db = _bget("db") if _bhas("db") else None
                _idx = _ri.ingest_zip(f"{user_id}:upload:wa:{msg_id or saved_path}", raw_bytes, str(user_id))
                await _ri.save(_db, _idx, str(user_id))
            except Exception as _e:
                logging.getLogger("oadson.api.whatsapp").warning(f"repo index skipped for WhatsApp zip: {_e}")

        text = caption or f"I sent a file: {file_name}"
        text += (
            f"\n\n[Attached file saved to your workspace at '{saved_path}' — "
            f"{len(raw_bytes)} bytes, {mime}. Use @document_reader for PDF/TXT/PY/JSON/MD, "
            "or @terminal (e.g. unzip, cat, ls) for anything else, including archives.]"
        )

    elif type_message in ("audioMessage", "pttMessage"):
        # Forwarded audio: fileMessageData can be empty — try multiple fields.
        file_data = (
            message_data.get("fileMessageData")
            or message_data.get("extendedTextMessageData")
            or {}
        )
        caption = file_data.get("caption", "").strip()
        if caption and not text:
            text = caption

        # FIX 2026-08-09: the owner sends other people's audio notes to
        # OADSON specifically to get a transcript — that's "forwarded"
        # audio, not the owner speaking to OADSON directly. Previously
        # both cases were handled identically: transcribe + let the
        # model decide what to do. Now they're distinct:
        #   - forwarded=True  → pure transcription job, no AI response
        #     needed beyond handing back the text. Add "FORWARDED AUDIO:"
        #     prefix so the model knows NOT to treat it as a command.
        #   - forwarded=False → voice note from the owner himself, treat
        #     as a real message with full orchestrator context.
        # GreenAPI sets this flag in the webhook when a message has been
        # forwarded (directly in messageData for newer API versions, or
        # inside fileMessageData for older ones).
        is_forwarded = (
            message_data.get("forwarded")
            or file_data.get("forwarded")
            or message_data.get("isForwarded")
            or False
        )

        fallback_url = (
            file_data.get("downloadUrl")
            or file_data.get("url")
            or message_data.get("downloadUrl", "")
        )

        raw_bytes, mime = await _download_file(chat_id, msg_id, fallback_url)
        if not raw_bytes:
            await send_whatsapp_reply(
                chat_id,
                "I couldn't download that voice note — it may have expired or there was a "
                "network issue. Try sending it again.",
                quoted_message_id=msg_id
            )
            return {"status": "ok", "reason": "audio_download_failed"}

        image_data = base64.b64encode(raw_bytes).decode()
        image_mime = mime or "audio/ogg"

        audio_size_kb = len(raw_bytes) // 1024
        audio_note = f"[Voice note received, {audio_size_kb}KB]"

        if is_forwarded:
            # Forwarded = someone else's audio, transcribe only.
            # Prefix tells the model not to treat this as a personal
            # command from the owner — just transcribe what was said and
            # return it cleanly.
            if not text:
                text = f"{audio_note} FORWARDED AUDIO: Transcribe this exactly as spoken. This is audio from someone else being sent for transcription — return just the transcript, no extra commentary."
            else:
                text = f"{audio_note} FORWARDED AUDIO: {text} — Transcribe this exactly as spoken."
        else:
            # Own voice note = direct message from the owner, full treatment.
            if not text:
                text = f"{audio_note} Transcribe and summarise this audio."
            else:
                text = f"{audio_note} {text}"

        try:
            attachment_url = await save_inline(db, raw_bytes, f"{msg_id or 'voice'}.ogg", image_mime, bucket="whatsapp")
            attachment_type = image_mime
            attachment_filename = f"{msg_id or 'voice'}.ogg"
        except Exception as e:
            logger.warning(f"Could not save WhatsApp voice note inline: {e}")

    if not text and not image_data:
        return {"status": "ignored", "reason": "empty message"}

    from bootstrap import get
    orchestrator = get("orchestrator")

    # ── MULTI-IMAGE BATCHING ──
    if image_data:
        key = chat_id
        if key not in _media_buffer:
            _media_buffer[key] = []
        _media_buffer[key].append({
            "image_data": image_data, "image_mime": image_mime, "caption": text,
            "attachment_url": attachment_url, "attachment_type": attachment_type,
            "attachment_filename": attachment_filename,
        })

        # Reset the batch timer — wait for possible additional images
        old_timer = _batch_timers.pop(key, None)
        if old_timer:
            old_timer.cancel()

        loop = asyncio.get_event_loop()
        handle = loop.call_later(
            BATCH_WINDOW_SECS,
            lambda: asyncio.ensure_future(_flush_media_batch(key, user, orchestrator, db))
        )
        _batch_timers[key] = handle
        return {"status": "buffering"}

    # ── PURE TEXT — process immediately ──
    # Was a single fixed session_id derived from chat_id forever — no
    # way for /new to actually start a fresh conversation, since every
    # message just landed back in the same one regardless. Now uses the
    # real active-session pointer /new and /delete actually manipulate.
    session_id = await _get_active_session_id(db, chat_id, user_id)
    _wa.mark("session resolved", session=session_id, chars_in=len(text))
    typing_task = asyncio.create_task(send_typing(chat_id))

    try:
        _wa.mark("orchestrator starting")
        result = await orchestrator.run(
            objective=text,
            user=user,
            session_id=session_id,
            surface="whatsapp",
            attachment_url=attachment_url,
            attachment_type=attachment_type,
            attachment_filename=attachment_filename,
        )
        typing_task.cancel()
        response_text = result.get("response", "")
        _wa.mark(
            "orchestrator returned",
            model=result.get("model_used"),
            skill=result.get("skill_triggered"),
            steps=len(result.get("steps") or []),
            chars_out=len(response_text or ""),
            error=bool(result.get("error")),
        )
        # FIX 2026-08-09 (caught live — screenshots showed OADSON's
        # replies quoting its OWN earlier message instead of the
        # user's new one): this was passing quoted_stanza_id, which is
        # the ID of whatever OLDER message the user themselves quoted —
        # i.e. the thing being referenced, not the thing being replied
        # to right now. OADSON's own reply now quotes msg_id (the
        # user's CURRENT incoming message, already extracted way above
        # for dedup purposes), so the reply visually attaches to the
        # question that was actually just asked. quoted_stanza_id/
        # quoted_text are still used above to feed the MODEL context
        # about what the user was referencing — that part was correct,
        # only the outgoing quote TARGET was wrong.
        #
        # Also, deliberately no longer conditional on "only when the
        # user quoted something" — msg_id exists on every message, and
        # always anchoring the reply to what it's answering is the
        # actual point of this feature, not just a special case for
        # quoted messages specifically.
        await send_whatsapp_reply(chat_id, response_text, quoted_message_id=msg_id)
        _wa.mark("reply sent")

        skill_output = result.get("skill_output")
        if skill_output:
            await _deliver_skill_attachment(chat_id, skill_output, quoted_message_id=msg_id)
            _wa.mark("attachment delivered")

        await db["whatsapp_log"].insert_one({
            "from": sender_phone, "chat_id": chat_id, "type": type_message,
            "message": text, "response": response_text,
            "timestamp": datetime.now(timezone.utc),
        })

    except Exception as e:
        typing_task.cancel()
        _wa.mark("FAILED", error=type(e).__name__)
        logger.error(f"WhatsApp webhook error: {e}", exc_info=True)
        await send_whatsapp_reply(chat_id, f"Error: {str(e)[:200]}")

    _wa.mark("webhook done")
    return {"status": "ok"}


async def _flush_media_batch(chat_id: str, user: dict, orchestrator, db) -> None:
    """Called when batch window expires — process all buffered images as ONE request."""
    items = _media_buffer.pop(chat_id, [])
    _batch_timers.pop(chat_id, None)

    if not items:
        return

    caption = next((i["caption"] for i in items if i.get("caption")), "")
    count = len(items)
    text = caption or (f"Describe these {count} images." if count > 1 else "Describe this image.")

    # ADDED 2026-09-19 — same per-stage timing as the text path. The batch
    # has no single message id, so it is tagged by the first image's
    # attachment filename (which is derived from its msg_id).
    _wa = _WATimer((items[0].get("attachment_filename") or "").split(".")[0])
    _wa.mark("image batch flushing", images=count)

    # Real fix — previously only the first image ever reached Gemini
    # (core/orchestrator.py's run() only accepted a single image_data/
    # image_mime pair), with a text note asking the model to tell the
    # user the rest were skipped. Now all of them go through via
    # extra_images — Gemini's contents API takes multiple inline_data
    # parts in one turn natively, this was never actually a Gemini
    # limitation.
    primary = items[0]
    rest = items[1:]

    # The durable attachment shown on the website is still just the
    # first image for now (api/sessions.py/chat.html only render one
    # attachment per message) — a reasonable simplification, not a
    # silent data loss like the old "only analyze the first" behavior
    # was, since Gemini now genuinely sees every image.

    session_id = await _get_active_session_id(db, chat_id, str(user["_id"]))
    # REUSE the typing loop already started at image receipt (see
    # whatsapp_webhook's imageMessage branch) instead of starting a
    # second, independent one — both would ping Green API in parallel
    # otherwise, and only ONE of the two ever gets cancelled below by a
    # locally-scoped variable that has no idea the other one exists.
    # Falls back to starting one here only if, for whatever reason,
    # nothing is currently tracked for this chat (e.g. a future caller
    # of _flush_media_batch that didn't go through the imageMessage
    # receipt path).
    typing_task = _early_typing_tasks.pop(chat_id, None)
    if typing_task is None or typing_task.done():
        typing_task = asyncio.create_task(send_typing(chat_id))

    try:
        _wa.mark("orchestrator starting", session=session_id)
        result = await orchestrator.run(
            objective=text,
            user=user,
            session_id=session_id,
            surface="whatsapp",
            image_data=primary["image_data"],
            image_mime=primary["image_mime"],
            extra_images=[{"data": i["image_data"], "mime": i["image_mime"]} for i in rest],
            attachment_url=primary.get("attachment_url"),
            attachment_type=primary.get("attachment_type"),
            attachment_filename=primary.get("attachment_filename"),
        )
        typing_task.cancel()
        response_text = result.get("response", "")
        _wa.mark(
            "orchestrator returned",
            model=result.get("model_used"),
            skill=result.get("skill_triggered"),
            chars_out=len(response_text or ""),
            error=bool(result.get("error")),
        )
        await send_whatsapp_reply(chat_id, response_text)
        _wa.mark("reply sent")

        skill_output = result.get("skill_output")
        if skill_output:
            await _deliver_skill_attachment(chat_id, skill_output)
            _wa.mark("attachment delivered")

        await db["whatsapp_log"].insert_one({
            "chat_id": chat_id, "type": "imageMessage", "message": text,
            "response": response_text, "timestamp": datetime.now(timezone.utc),
        })

    except Exception as e:
        typing_task.cancel()
        _wa.mark("FAILED", error=type(e).__name__)
        logger.error(f"WhatsApp batch flush error: {e}", exc_info=True)
        await send_whatsapp_reply(chat_id, f"Error: {str(e)[:200]}")


# ── OUTBOUND (manual send) ──

class WhatsAppOutbound(BaseModel):
    to: str
    message: str


@router.post("/whatsapp/send")
async def send_whatsapp(body: WhatsAppOutbound, db=Depends(get_db)):
    if not await _get_whatsapp_enabled(db):
        raise HTTPException(503, "WhatsApp bridge is disabled.")
    await send_whatsapp_reply(body.to, body.message)
    return {"status": "sent", "to": body.to}
