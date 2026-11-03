"""
Telegram Skill for OADSON V2.

Surfaces:
- OADSON can send messages/files to you via @Oadsonbot
- You can message @Oadsonbot and OADSON receives + responds
- Webhook in telegram_router.py handles incoming messages

Actions (called by Gemini):
- send_message     → send text to your Telegram
- send_file        → send a file/document
- send_photo       → send an image
- get_updates      → get recent messages sent to the bot
- get_chat_id      → get your chat ID (first time setup)
- set_webhook      → register the Railway webhook URL
"""

import os
import httpx
from typing import Optional


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
# Your personal Telegram chat ID — set this after first /start message
TELEGRAM_OWNER_CHAT_ID = os.getenv("TELEGRAM_OWNER_CHAT_ID", "")


class TelegramSkill:
    def __init__(self):
        self.token = TELEGRAM_BOT_TOKEN
        self.base = TELEGRAM_API
        self.owner_chat_id = TELEGRAM_OWNER_CHAT_ID

        if not self.token:
            self.error = "TELEGRAM_BOT_TOKEN not set in environment"
        elif not self.owner_chat_id:
            self.error = (
                "TELEGRAM_OWNER_CHAT_ID not set. "
                "Send /start to @Oadsonbot first, then ask OADSON to get_chat_id."
            )
        else:
            self.error = None

    async def send_message(self, text: str, chat_id: str = None, parse_mode: str = "Markdown") -> dict:
        """Send a text message to your Telegram."""
        target = chat_id or self.owner_chat_id
        if not target:
            return {"status": "error", "reason": "No chat_id. Send /start to @Oadsonbot first."}

        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(f"{self.base}/sendMessage", json={
                "chat_id": target,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            })

        data = res.json()
        if not data.get("ok"):
            return {"status": "error", "reason": data.get("description", "Telegram API error")}

        return {
            "status": "success",
            "message": "Message sent to your Telegram",
            "message_id": data["result"]["message_id"]
        }

    async def send_file(self, file_url: str, filename: str, caption: str = "") -> dict:
        """Send a file/document to your Telegram by URL."""
        target = self.owner_chat_id
        if not target:
            return {"status": "error", "reason": "No chat_id configured."}

        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(f"{self.base}/sendDocument", json={
                "chat_id": target,
                "document": file_url,
                "caption": caption or filename,
                "parse_mode": "Markdown"
            })

        data = res.json()
        if not data.get("ok"):
            # Try sending as URL in message if document send fails
            fallback = await self.send_message(
                f"📄 *{filename}*\n{caption}\n\n[Download]({file_url})"
            )
            return fallback

        return {
            "status": "success",
            "message": f"File '{filename}' sent to your Telegram"
        }

    async def send_photo(self, photo_url: str, caption: str = "") -> dict:
        """Send a photo to your Telegram by URL."""
        target = self.owner_chat_id
        if not target:
            return {"status": "error", "reason": "No chat_id configured."}

        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(f"{self.base}/sendPhoto", json={
                "chat_id": target,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "Markdown"
            })

        data = res.json()
        if not data.get("ok"):
            return {"status": "error", "reason": data.get("description", "Photo send failed")}

        return {"status": "success", "message": "Photo sent to your Telegram"}

    async def get_updates(self, limit: int = 10) -> dict:
        """Get recent messages sent to @Oadsonbot."""
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.get(f"{self.base}/getUpdates", params={
                "limit": limit,
                "allowed_updates": ["message"]
            })

        data = res.json()
        if not data.get("ok"):
            return {"status": "error", "reason": data.get("description")}

        updates = data.get("result", [])
        messages = []
        for u in updates:
            msg = u.get("message", {})
            if msg:
                messages.append({
                    "from": msg.get("from", {}).get("first_name", "Unknown"),
                    "username": msg.get("from", {}).get("username", ""),
                    "chat_id": msg.get("chat", {}).get("id"),
                    "text": msg.get("text", ""),
                    "date": msg.get("date")
                })

        return {
            "status": "success",
            "count": len(messages),
            "messages": messages
        }

    async def get_chat_id(self) -> dict:
        """
        Get your chat ID from the most recent /start message.
        Send /start to @Oadsonbot first, then call this.
        """
        result = await self.get_updates(limit=20)
        if result["status"] != "success":
            return result

        for msg in reversed(result["messages"]):
            if msg.get("text", "").startswith("/start"):
                chat_id = str(msg["chat_id"])
                return {
                    "status": "success",
                    "chat_id": chat_id,
                    "from": msg["from"],
                    "instruction": (
                        f"Add TELEGRAM_OWNER_CHAT_ID={chat_id} to Railway env vars. "
                        "OADSON will then be able to message you directly."
                    )
                }

        return {
            "status": "error",
            "reason": "No /start message found. Open Telegram, go to @Oadsonbot, tap Start."
        }

    async def set_webhook(self, webhook_url: str) -> dict:
        """Register the Render webhook so Telegram pushes messages to OADSON."""
        # Was "/api/v2/telegram/webhook" — a leftover from the old Railway
        # V2 backend. V3's telegram router is mounted at /api/v3 (see
        # main.py), so this was registering Telegram's webhook against a
        # URL this backend never serves, silently breaking the bridge for
        # anyone who ran this skill after the V2→V3 migration.
        full_url = webhook_url.rstrip("/") + "/api/v3/telegram/webhook"

        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(f"{self.base}/setWebhook", json={
                "url": full_url,
                "allowed_updates": ["message", "callback_query"]
            })

        data = res.json()
        if not data.get("ok"):
            return {"status": "error", "reason": data.get("description")}

        return {
            "status": "success",
            "message": f"Webhook set to {full_url}",
            "description": data.get("description", "")
        }

    async def delete_webhook(self) -> dict:
        """Remove webhook (switch back to polling mode)."""
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(f"{self.base}/deleteWebhook")
        data = res.json()
        return {"status": "success" if data.get("ok") else "error",
                "message": data.get("description", "")}


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    message = payload.get("message", "").lower().strip()

    skill = TelegramSkill()

    # Determine action
    action = (
        args.get("action")
        or _detect_action(message)
    )

    # Error check — but allow get_chat_id even without owner_chat_id
    if skill.error and action not in ("get_chat_id", "set_webhook", "get_updates"):
        return {"status": "error", "reason": skill.error}

    if action == "send_message":
        text = args.get("text") or args.get("message") or message
        return await skill.send_message(text)

    elif action == "send_file":
        file_url = args.get("file_url") or args.get("url") or args.get("download_url")
        filename = args.get("filename") or "file"
        caption = args.get("caption") or ""
        if not file_url:
            return {"status": "error", "reason": "file_url required"}
        return await skill.send_file(file_url, filename, caption)

    elif action == "send_photo":
        photo_url = args.get("photo_url") or args.get("url")
        caption = args.get("caption") or ""
        if not photo_url:
            return {"status": "error", "reason": "photo_url required"}
        return await skill.send_photo(photo_url, caption)

    elif action == "get_updates":
        return await skill.get_updates()

    elif action == "get_chat_id":
        return await skill.get_chat_id()

    elif action == "set_webhook":
        url = args.get("url") or os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
        if not url:
            return {"status": "error", "reason": "Provide webhook URL or set RAILWAY_PUBLIC_DOMAIN env var"}
        if not url.startswith("http"):
            url = "https://" + url
        return await skill.set_webhook(url)

    elif action == "delete_webhook":
        return await skill.delete_webhook()

    else:
        return {
            "status": "error",
            "reason": f"Unknown action: '{action}'. Available: send_message, send_file, send_photo, get_updates, get_chat_id, set_webhook"
        }


def _detect_action(message: str) -> str:
    """Detect intent from natural language."""
    if any(w in message for w in ["send", "message", "tell", "notify", "alert"]):
        if any(w in message for w in ["file", "document", "pdf", "zip"]):
            return "send_file"
        if any(w in message for w in ["photo", "image", "picture"]):
            return "send_photo"
        return "send_message"
    if any(w in message for w in ["read", "check", "unread", "updates", "messages"]):
        return "get_updates"
    if any(w in message for w in ["chat id", "my id", "who am i"]):
        return "get_chat_id"
    if "webhook" in message:
        return "set_webhook"
    return "send_message"
