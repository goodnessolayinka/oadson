"""
core/style_profile.py — "memory_manager, but for how you think and text."

On-demand, not automatic — nothing scans your messages in the background
on every turn. Two things happen only when the style_manager skill is
explicitly invoked:

  - learn: pulls YOUR OWN messages (role="user" only, never OADSON's own
    replies) across ALL your sessions, not just the current one — the
    whole point is patterns that show up across many chats, per your
    "several chats can save that" requirement. Only processes messages
    since the last learn call (a stored cursor), so repeat calls don't
    re-pay to reprocess the same history already analyzed — same
    quota-conscious principle as the thinking/planner calls.

  - write: generates content in your voice using the stored profile.
    Auto-bootstraps a profile on first use if none exists yet (so
    'write' works standalone), but does NOT re-learn on every write
    after that — refreshing is an explicit 'learn' call.

The profile is MERGED, not overwritten, on each learn call — the
existing profile is fed back into the prompt alongside only the new
messages, and Gemini is asked to keep confirmed patterns, add newly
noticed ones with real examples, adjust what's changed. This is what
makes it improve over time rather than reset each time.

Honest limitation: this only ever sees what you've typed TO OADSON —
a specific register (talking to an assistant). It may not fully capture
how you write a formal assignment or text a friend on WhatsApp if
those registers differ meaningfully from how you talk here. Treat the
first several 'write' outputs as a check on that assumption, not as
guaranteed-accurate from day one.
"""

import json
import logging
from datetime import datetime

import httpx

logger = logging.getLogger("oadson.style_profile")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_MESSAGES_PER_LEARN = 300  # cap per call — keeps prompt size/cost bounded

# Automatic background learning fires once this many NEW user messages
# have accumulated since the last pass — NOT on every single message.
# That would mean an extra Gemini call every turn, on top of the
# decision call and whatever thinking/planner calls are already enabled
# — a real, constant quota cost for a feature that's supposed to run
# without you even asking. Batching it this way means the learning is
# still fully automatic (no command needed, ever), just not paid for on
# every single turn.
AUTO_LEARN_MESSAGE_THRESHOLD = 15


def _as_datetime(value):
    """
    Normalizes a Mongo-stored 'time' value to a real datetime for
    comparison. Was a straight `msg_time <= since` — but 'time' fields
    across this codebase aren't consistently one type: usually a real
    datetime object (e.g. core/orchestrator.py's _persist_memory writes
    datetime.utcnow() directly), but not guaranteed everywhere, and this
    crashed the instant the two sides didn't match. Returns None if the
    value can't be interpreted as a time at all, rather than raising.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


async def _fetch_new_user_messages(db, user_id: str, since=None) -> list:
    """Pulls the user's own messages across ALL their sessions, oldest first."""
    cursor = db["ai_sessions"].find({"user_id": user_id}, {"messages": 1})
    since_dt = _as_datetime(since)

    all_messages = []
    async for doc in cursor:
        for m in doc.get("messages", []):
            if m.get("role") != "user":
                continue
            msg_time = m.get("time")
            msg_time_dt = _as_datetime(msg_time)
            if since_dt and msg_time_dt and msg_time_dt <= since_dt:
                continue
            content = m.get("content", "")
            if content:
                all_messages.append({"content": content, "time": msg_time})

    all_messages.sort(key=lambda m: _as_datetime(m.get("time")) or datetime.min)
    return all_messages[-MAX_MESSAGES_PER_LEARN:]


async def learn_style(db, user_id: str, api_key: str, model: str = "gemini-3.1-flash-lite", additional_text: str = None) -> dict:
    existing = await db["style_profiles"].find_one({"user_id": user_id})
    last_analyzed = existing.get("last_analyzed_time") if existing else None

    new_messages = await _fetch_new_user_messages(db, user_id, since=last_analyzed)

    # Previously this only ever pulled from the user's own OADSON message
    # history — there was no way to feed in text from anywhere else, so a
    # request like "learn my style from my WhatsApp messages" had nothing
    # to actually do: @whatsapp_reader could fetch the text, but
    # @style_manager had no parameter to receive it. additional_text is
    # that missing bridge — e.g. the raw chat content @whatsapp_reader
    # already fetched, passed straight through as extra material to
    # analyze alongside (or instead of) OADSON's own message history.
    if not new_messages and not additional_text:
        if existing:
            return {
                "status": "info",
                "message": "No new messages since the last learning pass — profile unchanged.",
                "profile": existing.get("profile"),
            }
        return {"status": "info", "message": "No messages found yet to learn from."}

    messages_text = "\n".join(f"- {m['content']}" for m in new_messages)
    if additional_text:
        # Bounded — this is free-text handed in by the model, not
        # something the user typed directly, so cap it generously rather
        # than trusting it's already a reasonable size.
        messages_text = f"{messages_text}\n\n(Additional supplied text)\n{additional_text[:8000]}".strip()

    existing_profile_text = (
        json.dumps(existing.get("profile"), indent=2) if existing else "None yet — this is the first pass."
    )

    prompt = f"""You're building a writing-style profile for a person, from their own chat messages.
Only analyze the messages shown below — they are all this person's own words.

EXISTING PROFILE (merge with this — keep confirmed patterns unless contradicted, add new ones):
{existing_profile_text}

NEW MESSAGES TO ANALYZE (this batch only — {len(new_messages)} messages{' plus supplied text' if additional_text else ''}):
{messages_text}

Output ONLY valid JSON (no markdown fences, no commentary):
{{
  "tone_summary": "2-4 sentences: overall register, formality, typical mood/energy",
  "languages": [
    {{"language": "Nigerian Pidgin", "example_phrases": ["...", "..."], "notes": "when/how they use it"}}
  ],
  "structural_patterns": ["real observed habits, e.g. rarely uses ending punctuation, short fragments"],
  "recurring_phrases": ["specific words/phrases they actually use often, pulled from the real messages"],
  "notes_for_writing_as_them": "practical guidance for someone drafting text in this person's voice"
}}

Only include a language entry (Pidgin, Yoruba, or otherwise) if there is
REAL evidence of it in the messages shown — never invent usage that isn't
actually there."""

    url = GEMINI_URL.format(model=model)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 1500, "temperature": 0.3},
    }

    try:
        # SECURITY FIX 2026-08-14: params={"key": api_key} still renders
        # as ?key=... on the wire — moved to x-goog-api-key header,
        # which was leaking into Render's httpx request logs.
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(url, headers={"x-goog-api-key": api_key}, json=payload)
        if res.status_code != 200:
            return {"status": "error", "reason": f"Learning call failed: {res.status_code}"}
        data = res.json()
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        profile = json.loads(text.strip())
    except Exception as e:
        return {"status": "error", "reason": f"Learning failed: {e}"}

    now = datetime.utcnow()
    total_analyzed = (existing.get("messages_analyzed_count", 0) if existing else 0) + len(new_messages)

    await db["style_profiles"].update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id": user_id,
            "profile": profile,
            "last_analyzed_time": now,
            "messages_analyzed_count": total_analyzed,
            "updated_at": now,
        }},
        upsert=True,
    )

    languages_found = [l.get("language") for l in profile.get("languages", []) if l.get("language")]
    return {
        "status": "success",
        "message": (
            f"Updated style profile from {len(new_messages)} new message(s) "
            f"({total_analyzed} analyzed total)."
            + (f" Noticed: {', '.join(languages_found)}." if languages_found else "")
        ),
        "profile": profile,
        "messages_analyzed_this_pass": len(new_messages),
        "messages_analyzed_total": total_analyzed,
    }


async def get_profile(db, user_id: str) -> dict:
    doc = await db["style_profiles"].find_one({"user_id": user_id})
    if not doc:
        return {"status": "info", "message": "No style profile yet — run action='learn' first."}
    return {
        "status": "success",
        "profile": doc.get("profile"),
        "messages_analyzed_total": doc.get("messages_analyzed_count", 0),
        "last_updated": doc.get("updated_at"),
    }


async def write_as_user(db, user_id: str, api_key: str, request: str, model: str = "gemini-3.1-flash-lite") -> dict:
    doc = await db["style_profiles"].find_one({"user_id": user_id})
    if not doc:
        # Auto-bootstrap once so 'write' works standalone on first use.
        # Refreshing afterward is always an explicit 'learn' call, never
        # automatic on every write — same quota principle as everywhere else.
        learn_result = await learn_style(db, user_id, api_key, model)
        if learn_result["status"] == "error":
            return learn_result
        doc = await db["style_profiles"].find_one({"user_id": user_id})
        if not doc:
            return {"status": "error", "reason": "Not enough message history yet to build a style profile."}

    profile_text = json.dumps(doc.get("profile", {}), indent=2)

    prompt = f"""Write the following in this specific person's actual voice — not a generic assistant voice.

THEIR STYLE PROFILE:
{profile_text}

WHAT TO WRITE:
{request}

Write ONLY the requested content, in their voice, using their real patterns
(including their actual Pidgin/Yoruba usage where it naturally fits — don't
force it if the content type doesn't call for it; a formal assignment likely
needs less of it than a WhatsApp text would). No preamble, no explanation of
what you did — just the content itself."""

    url = GEMINI_URL.format(model=model)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 2000, "temperature": 0.7},
    }

    try:
        # SECURITY FIX 2026-08-14: params={"key": api_key} still renders
        # as ?key=... on the wire — moved to x-goog-api-key header,
        # which was leaking into Render's httpx request logs.
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(url, headers={"x-goog-api-key": api_key}, json=payload)
        if res.status_code != 200:
            return {"status": "error", "reason": f"Generation failed: {res.status_code}"}
        data = res.json()
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except Exception as e:
        return {"status": "error", "reason": f"Generation failed: {e}"}

    return {
        "status": "success",
        "content": text,
        "message": f"Written in your style (based on {doc.get('messages_analyzed_count', 0)} messages analyzed).",
    }


async def reset_profile(db, user_id: str) -> dict:
    result = await db["style_profiles"].delete_one({"user_id": user_id})
    if result.deleted_count:
        return {"status": "success", "message": "Style profile cleared."}
    return {"status": "info", "message": "No style profile existed to clear."}


async def _count_new_user_messages(db, user_id: str, since=None) -> int:
    """Cheap count-only query — doesn't pull message content just to count."""
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$unwind": "$messages"},
        {"$match": {"messages.role": "user"}},
    ]
    if since:
        pipeline[-1]["$match"]["messages.time"] = {"$gt": since}
    pipeline.append({"$count": "count"})

    result = await db["ai_sessions"].aggregate(pipeline).to_list(length=1)
    return result[0]["count"] if result else 0


async def maybe_learn_in_background(db, user_id: str, api_key: str, model: str = "gemini-3.1-flash-lite") -> None:
    """
    THE actual "learns without consent" mechanism — call this as a
    fire-and-forget task (asyncio.create_task, never awaited) from the
    orchestrator after every turn. It's automatic in the sense that
    nothing has to ask for it, but only actually calls Gemini once
    AUTO_LEARN_MESSAGE_THRESHOLD new messages have piled up since the
    last pass, so it doesn't cost a Gemini call on every single turn.

    Never raises — this must never be allowed to affect the actual chat
    response it's riding alongside.
    """
    try:
        existing = await db["style_profiles"].find_one({"user_id": user_id})
        last_analyzed = existing.get("last_analyzed_time") if existing else None

        new_count = await _count_new_user_messages(db, user_id, since=last_analyzed)
        if new_count < AUTO_LEARN_MESSAGE_THRESHOLD:
            return

        result = await learn_style(db, user_id, api_key, model)
        logger.info(f"Auto-learned style profile for user {user_id}: {result.get('message')}")
    except Exception as e:
        logger.warning(f"Background style learning failed (non-fatal, never affects chat): {e}")
