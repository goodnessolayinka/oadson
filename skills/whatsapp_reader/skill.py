"""
whatsapp_reader — OADSON V3.

Read-only WhatsApp access via a SEPARATE Green API instance (SIM 1).
This is NOT the router instance. No webhook. No sending. No replying.

Pulls chat history on-demand so OADSON can summarize/understand
conversations when asked. Never writes back to WhatsApp.

Actions:
  chats          — list all chats on this instance
  history        — get message history for a specific chat
  summarize       — get history + hint for OADSON to summarize
"""

import os
import httpx
import logging

logger = logging.getLogger("oadson.skill.whatsapp_reader")


def _creds():
    instance_id = os.getenv("GREEN_API_INSTANCE_ID_2", "")
    token = os.getenv("GREEN_API_TOKEN_2", "")
    return instance_id, token


async def _get_chats(instance_id: str, token: str) -> list:
    url = f"https://api.green-api.com/waInstance{instance_id}/getChats/{token}"
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.get(url)
    res.raise_for_status()
    return res.json()


async def _get_chat_history(instance_id: str, token: str, chat_id: str, count: int = 20) -> list:
    url = f"https://api.green-api.com/waInstance{instance_id}/getChatHistory/{token}"
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.post(url, json={"chatId": chat_id, "count": count})
    res.raise_for_status()
    return res.json()


def _normalize_chat_id(raw: str) -> str:
    """Accepts a phone number or chatId, returns proper chatId format."""
    raw = raw.strip()
    if raw.endswith("@c.us") or raw.endswith("@g.us"):
        return raw
    digits = "".join(c for c in raw if c.isdigit())
    return f"{digits}@c.us"


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    action = args.get("action") or "chats"

    instance_id, token = _creds()
    if not instance_id or not token:
        return {
            "status": "error",
            "reason": "GREEN_API_INSTANCE_ID_2 / GREEN_API_TOKEN_2 not configured. This is read-only access to a separate WhatsApp number."
        }

    try:
        if action == "chats":
            # Was returning EVERY chat on the instance with no limit at
            # all — for a number with hundreds/thousands of contacts
            # built up over time, this alone could be the bulk of the
            # 293K-377K token requests that broke every single fallback
            # provider in the chain (Groq/NVIDIA/Mistral all cap between
            # 8K-262K tokens — nowhere close to what this could dump).
            # Hard-capped regardless of what's asked for.
            requested_limit = int(args.get("limit") or 50)
            limit = min(requested_limit, 100)
            chats = await _get_chats(instance_id, token)
            simplified = [
                {"chat_id": c.get("id"), "name": c.get("name", c.get("id"))}
                for c in chats
                if c.get("id", "").endswith("@c.us") or c.get("id", "").endswith("@g.us")
            ][:limit]
            total_available = len([
                c for c in chats
                if c.get("id", "").endswith("@c.us") or c.get("id", "").endswith("@g.us")
            ])
            return {
                "status": "success",
                "chats": simplified,
                "count": len(simplified),
                "total_available": total_available,
                "message": (
                    f"Showing {len(simplified)} of {total_available} chat(s) on this number."
                    + (f" {total_available - len(simplified)} more exist — ask for a specific "
                       "chat by name/number, or pass a higher 'limit' (max 100), rather than "
                       "requesting the full list." if total_available > len(simplified) else "")
                )
            }

        elif action in ("history", "summarize"):
            chat_ref = (args.get("chat_id") or args.get("phone") or args.get("name") or "").strip()
            if not chat_ref:
                return {"status": "error", "reason": f"{action} requires 'chat_id' or 'phone'"}

            count = min(int(args.get("count") or 20), 50)
            chat_id = _normalize_chat_id(chat_ref)

            history = await _get_chat_history(instance_id, token, chat_id, count=count)

            messages = []
            for msg in history:
                text = ""
                if msg.get("typeMessage") == "textMessage":
                    text = msg.get("textMessage", "")
                elif msg.get("typeMessage") == "extendedTextMessage":
                    text = msg.get("extendedTextMessage", {}).get("text", "")
                else:
                    text = f"[{msg.get('typeMessage', 'media')}]"

                # Cap individual message length — a single long forwarded
                # text or document caption could otherwise blow up
                # context on its own regardless of message count.
                if len(text) > 1000:
                    text = text[:1000] + "... [truncated]"

                messages.append({
                    "from": "me" if msg.get("fromMe") else "them",
                    "text": text,
                    "timestamp": msg.get("timestamp"),
                })

            result = {
                "status": "success",
                "chat_id": chat_id,
                "message_count": len(messages),
                "messages": messages,
            }

            if action == "summarize":
                result["instruction"] = (
                    "Summarize this WhatsApp conversation. This is READ-ONLY — "
                    "never suggest sending a reply through this number."
                )

            return result

        elif action == "recent_summary":
            limit_chats = min(int(args.get("limit_chats") or 5), 10)
            count_per_chat = min(int(args.get("count") or 10), 20)

            all_chats = await _get_chats(instance_id, token)
            real_chats = [
                c for c in all_chats
                if c.get("id", "").endswith("@c.us") or c.get("id", "").endswith("@g.us")
            ][:limit_chats]

            summary_data = []
            for c in real_chats:
                chat_id = c.get("id")
                name = c.get("name", chat_id)
                try:
                    history = await _get_chat_history(instance_id, token, chat_id, count=count_per_chat)
                    messages = []
                    for msg in history:
                        text = ""
                        if msg.get("typeMessage") == "textMessage":
                            text = msg.get("textMessage", "")
                        elif msg.get("typeMessage") == "extendedTextMessage":
                            text = msg.get("extendedTextMessage", {}).get("text", "")
                        else:
                            text = f"[{msg.get('typeMessage', 'media')}]"
                        if len(text) > 500:
                            text = text[:500] + "... [truncated]"
                        messages.append({
                            "from": "me" if msg.get("fromMe") else "them",
                            "text": text,
                        })
                    summary_data.append({
                        "chat_id": chat_id,
                        "name": name,
                        "messages": messages,
                    })
                except Exception as e:
                    summary_data.append({
                        "chat_id": chat_id,
                        "name": name,
                        "error": str(e),
                    })

            return {
                "status": "success",
                "chats_covered": len(summary_data),
                "data": summary_data,
                "instruction": (
                    f"Summarize each of these {len(summary_data)} conversations separately, "
                    "one short summary per contact/group. This is READ-ONLY — "
                    "never suggest sending a reply through this number."
                )
            }

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Use: chats, history, summarize, recent_summary"
            }

    except httpx.HTTPStatusError as e:
        return {"status": "error", "reason": f"Green API error: {e.response.status_code}"}
    except Exception as e:
        logger.error(f"whatsapp_reader error: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}
