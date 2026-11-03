"""
Context compressor — added 2026-08-20 alongside Trace mode work.

PROBLEM THIS FIXES:
core/orchestrator.py's _build_context does `session_doc["messages"][-20:]`
— a hard cutoff, not compression. Message 21 and everything before it is
simply invisible to the model on every single call, no exceptions, no
degradation curve. A session that grows past 20 messages doesn't get
fuzzier memory of its early parts — it gets NONE. This is what actually
happens today, silently, on every conversation once it crosses 20
messages, regardless of how important those earlier messages were.

WHAT THIS DOES:
Once a session's stored message count exceeds COMPRESS_THRESHOLD (100), the
older portion (everything before the recent verbatim window) gets
folded into a single rolling summary instead of dropped outright. The
recent window stays completely untouched, verbatim, full fidelity —
compression only ever applies to the part that was ALREADY going to be
invisible under the old cutoff, so this can only improve on today's
behavior, never make it worse. Nothing that was visible before becomes
less visible.

The summary itself is cached on the session document and only
regenerated when enough NEW messages have accumulated since the last
summary was built (SUMMARY_REFRESH_STRIDE) — not on every single turn,
which would mean a flash-lite call on every message once a session is
long. Most turns in a long session just reuse the cached summary as-is.

Uses gemini-3.5-flash-lite — same cheap-auxiliary-call model already
established in memory/vault.py's bootstrap_core_summary fallback path,
same x-goog-api-key header pattern (key in header, never URL — see the
2026-08-14 security note in vault.py this file matches).

NOT touched by this file: the recent-window slice size, Gemini's role-
alternation handling, or anything else in _build_context — this module
is called BY _build_context, it doesn't replace how contents get built,
just what raw_history contains before that logic runs.
"""

import os
import logging
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

# Compression kicks in once stored messages exceed this count. Chosen to
# sit above the existing -20 recent-window slice with real headroom —
# a session at exactly 21-30 messages barely benefits from compression
# (only a handful of messages would've been dropped anyway) so there's
# no point paying for a flash-lite call that early. 100 was specifically
# requested as the "starts mattering" point.
COMPRESS_THRESHOLD = 100

# How many messages stay verbatim, untouched, full fidelity — matches
# the existing -20 slice in _build_context so behavior for the recent
# window is unchanged; compression only ever touches what's OLDER than
# this, i.e. what was already being dropped entirely before this file
# existed.
RECENT_WINDOW = 20

# Don't regenerate the summary every single turn once a session is long
# — that would mean a flash-lite call on every message past the
# threshold. Only rebuild once this many NEW messages have accumulated
# past whatever was already summarized. Most turns in a long session
# just reuse the cached summary as-is.
SUMMARY_REFRESH_STRIDE = 10

# FIX 2026-08-23: the recent RECENT_WINDOW messages are sent verbatim,
# full fidelity — by design, that's the whole point of keeping them
# uncompressed. But "full fidelity" was never meant to mean "unbounded
# size" — a terminal session with tool results embedded directly into
# message content (see oadson_pkg's agent.py _build_payload, which had
# NO cap until this same fix landed there too) could and did produce
# individual messages of tens of thousands of characters each. Twenty
# such messages, still well under COMPRESS_THRESHOLD by MESSAGE COUNT,
# was enough to balloon a session to well over a million tokens —
# observed directly: different fallback providers reported the same
# request as anywhere from ~950K to ~2.3M tokens depending on tokenizer,
# and every one of them refused it. This caps any individual message
# within the verbatim window, independent of the count-based compression
# above — the two are complementary, not redundant: this protects
# against one giant message, the other protects against many messages.
PER_MESSAGE_MAX_CHARS = 6000

_FLASH_LITE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.5-flash-lite:generateContent"
)


def _cap_message_sizes(messages: list) -> list:
    """Truncate any individual message's content that exceeds
    PER_MESSAGE_MAX_CHARS, with a clear marker showing how much was cut.
    Applied only to the verbatim/recent window — the older portion is
    already being reduced to a whole-conversation summary regardless of
    individual message size, so this would be redundant there."""
    capped = []
    for m in messages:
        content = m.get("content") or ""
        if len(content) > PER_MESSAGE_MAX_CHARS:
            omitted = len(content) - PER_MESSAGE_MAX_CHARS
            m = {**m, "content": (
                content[:PER_MESSAGE_MAX_CHARS] +
                f"\n... [truncated by context_compressor — {omitted:,} more "
                f"characters omitted from this stored message]"
            )}
        capped.append(m)
    return capped


def _format_messages_for_summary(messages: list) -> str:
    lines = []
    for m in messages:
        role = "User" if m.get("role") == "user" else "OADSON"
        content = (m.get("content") or "").strip()
        if not content:
            continue
        # Bounded per-message to keep the summarization prompt itself
        # from becoming enormous on a session with very long individual
        # messages — this is the PROMPT going in, not what gets stored,
        # so being generous here doesn't cost persisted storage.
        lines.append(f"{role}: {content[:1000]}")
    return "\n".join(lines)


async def compress_if_needed(session_doc: dict, db) -> tuple[list, str | None]:
    """
    Given a full session document, return (history_for_context, summary_text).

    history_for_context: the message list _build_context should actually
    use — either the full raw history unchanged (session under threshold,
    identical to today's behavior before this file existed) or the
    verbatim RECENT_WINDOW messages (compression active — the summary is
    returned separately, not spliced into this list, since it needs to
    be injected as its own labeled block, not disguised as a fake turn).

    summary_text: None if compression isn't active for this session yet,
    otherwise the rolling summary of everything older than the recent
    window.
    """
    all_messages = session_doc.get("messages", [])

    if len(all_messages) <= COMPRESS_THRESHOLD:
        # Under threshold — behave exactly as before this file existed,
        # EXCEPT for the size cap below (see _cap_message_sizes' own
        # docstring for why this couldn't just stay "exactly as before").
        return _cap_message_sizes(all_messages[-RECENT_WINDOW:]), None

    recent = _cap_message_sizes(all_messages[-RECENT_WINDOW:])
    older = all_messages[:-RECENT_WINDOW]

    cached = session_doc.get("context_summary") or {}
    cached_text = cached.get("text")
    cached_covering_count = cached.get("covering_count", 0)

    # Reuse the cached summary if it's still fresh enough — avoids a
    # flash-lite call on every single turn of a long-running session.
    if cached_text and (len(older) - cached_covering_count) < SUMMARY_REFRESH_STRIDE:
        return recent, cached_text

    summary_text = await _build_summary(older, cached_text)

    # Cache it on the session doc so the next several turns can reuse
    # it without recompressing. Best-effort — a failed write here just
    # means the next turn recomputes, not a correctness problem.
    try:
        session_id = session_doc.get("session_id")
        user_id = session_doc.get("user_id")
        if session_id and user_id and summary_text:
            await db["ai_sessions"].update_one(
                {"session_id": session_id, "user_id": user_id},
                {"$set": {
                    "context_summary": {
                        "text": summary_text,
                        "covering_count": len(older),
                        "updated_at": datetime.utcnow().isoformat(),
                    }
                }},
            )
    except Exception as e:
        logger.warning(f"context_compressor: failed to cache summary: {e}")

    return recent, summary_text


async def summarize_message_range(messages: list) -> str | None:
    """
    Public entry point for ANYTHING outside this module that needs a
    plain summary of an arbitrary list of messages — added 2026-08-27
    for skills/chat_history_search's date-range search ("what were we
    talking about last month"). Everything else in this file exists to
    summarize "the older portion of ONE session, possibly extending a
    prior summary" — this is deliberately the simpler, more general
    primitive underneath that: no session document, no caching, no
    rolling-extension logic, just "summarize this exact list of
    messages" — callers that need session-aware caching should use
    compress_if_needed instead; this is for one-off, already-collected
    message sets (e.g. everything across however many sessions fell
    inside a date range) that have no single owning session to cache
    against anyway.
    """
    capped = _cap_message_sizes(messages)
    return await _build_summary(capped, previous_summary=None)


async def _build_summary(older_messages: list, previous_summary: str | None) -> str | None:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.warning("context_compressor: no GEMINI_API_KEY, skipping compression this turn")
        return previous_summary  # degrade to stale summary rather than nothing

    convo_text = _format_messages_for_summary(older_messages)
    if not convo_text.strip():
        return previous_summary

    # Rolling summary: if one already exists, extend it rather than
    # re-summarizing the whole older portion from scratch every refresh
    # — cheaper, and avoids the summary drifting further from the
    # original text with each regeneration.
    if previous_summary:
        prompt = (
            "You maintain a running summary of an ongoing conversation between a "
            "user and an AI assistant called OADSON. Below is the EXISTING summary, "
            "followed by NEW messages that happened since it was written. Produce an "
            "UPDATED summary that folds the new messages in — preserve concrete "
            "facts, decisions made, names, numbers, and unresolved threads. Do not "
            "editorialize or add commentary. Prose, not bullets. Under 500 words.\n\n"
            f"EXISTING SUMMARY:\n{previous_summary}\n\n"
            f"NEW MESSAGES:\n{convo_text}\n\n"
            "Output ONLY the updated summary."
        )
    else:
        prompt = (
            "Summarize this conversation between a user and an AI assistant called "
            "OADSON. Preserve concrete facts, decisions made, names, numbers, and "
            "unresolved threads — anything that matters if the assistant needs to "
            "recall this later. Do not editorialize. Prose, not bullets. Under 500 "
            "words.\n\n"
            f"{convo_text}\n\n"
            "Output ONLY the summary."
        )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 900, "temperature": 0.2},
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                _FLASH_LITE_URL, json=payload, headers={"x-goog-api-key": api_key}
            )
        if res.status_code == 200:
            return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        logger.warning(f"context_compressor: flash-lite returned {res.status_code}")
    except Exception as e:
        logger.warning(f"context_compressor: summarization call failed: {e}")

    # Degrade to whatever we had before rather than losing the summary
    # entirely on a transient failure.
    return previous_summary
