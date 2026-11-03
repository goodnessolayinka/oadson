"""
core/proactive_agent.py — OADSON Autonomous Proactive Messaging.

OADSON decides on its own when to reach out to the owner on WhatsApp.
Nothing is hardcoded — no times, no templates. Gemini evaluates full context
every 30 minutes and decides whether to send, and what to say.

What Gemini considers:
  - Current time in Nigeria (WAT = UTC+1)
  - the owner's activity pattern (derived from message history timestamps)
  - Core memory — who he is, projects, exams, constraints
  - Weather for Osogbo + a sample city (real hourly data)
  - How long since the last proactive message
  - What the last proactive message was (don't repeat)
  - Recent conversation (don't re-say what was just covered)
  - Style profile (sounds like OADSON, not a scheduled bot)

The AI decides:
  - Whether now is a good moment to reach out
  - What's worth saying
  - How to say it naturally in his style

Hard limits (not AI-controlled — these are infra-level):
  - Never messages between 2AM-5AM WAT (hard sleep window)
  - Max 3 proactive messages per day
  - Minimum 3 hours between messages
  - Only messages during the owner's historically active hours
"""

import os
import asyncio
import logging
import json as _json
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger("oadson.proactive_agent")

CHECK_INTERVAL_SECS = 30 * 60   # evaluate every 30 minutes
MIN_GAP_SECS = 3 * 60 * 60      # at least 3h between proactive messages
MAX_PER_DAY = 3                  # hard daily ceiling
HARD_SLEEP_START = 2             # 2AM WAT — never message
HARD_SLEEP_END = 5               # 6AM WAT — earliest possible

PROACTIVE_KEY = "proactive_agent_meta"


def build_owner_chat_id(owner_number: str) -> str:
    """
    Turn a raw WHATSAPP_OWNER_NUMBER env value into a valid GreenAPI
    chat ID. Extracted into its own function on 2026-08-07 specifically
    so core/self_test.py can call the REAL code path directly, instead
    of a hand-copied re-implementation that could silently drift out of
    sync with this function and stop catching the exact regression it
    exists to catch (the "@c.us" corruption bug — see the fix note
    where this used to live inline, further down this file's history).
    """
    return f"{owner_number.lstrip('+').replace(' ', '').replace('-', '')}@c.us"


# ── DB helpers ──

async def _get_meta(db) -> dict:
    doc = await db["bridge_settings"].find_one({"_id": PROACTIVE_KEY})
    return doc or {}


async def _save_meta(db, data: dict) -> None:
    await db["bridge_settings"].update_one(
        {"_id": PROACTIVE_KEY},
        {"$set": data},
        upsert=True,
    )


# ── Time ──

def _now_wat() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=1)


# ── Activity window ──

async def _active_hours(db) -> list:
    """
    Derive which hours the owner is typically active from WhatsApp + session logs.
    Returns hours (WAT) sorted by frequency. Falls back if insufficient data.
    """
    try:
        since = datetime.now(timezone.utc) - timedelta(days=30)
        logs = await db["whatsapp_log"].find(
            {"timestamp": {"$gte": since}},
            {"timestamp": 1, "_id": 0},
        ).to_list(500)

        if len(logs) < 5:
            return [7, 8, 9, 10, 11, 18, 19, 20, 21, 22]

        counts: dict = {}
        for log in logs:
            ts = log.get("timestamp")
            if ts:
                h = (ts.replace(tzinfo=timezone.utc) + timedelta(hours=1)).hour
                counts[h] = counts.get(h, 0) + 1

        if not counts:
            return [7, 8, 9, 10, 11, 18, 19, 20, 21, 22]

        peak = max(counts.values())
        return sorted(h for h, c in counts.items() if c >= peak * 0.35)

    except Exception as e:
        logger.warning(f"Activity hours derivation failed: {e}")
        return [7, 8, 9, 10, 11, 18, 19, 20, 21, 22]


# ── Weather ──

async def _weather_context(openweather_key: str) -> str:
    """Fetch next 12h weather for Osogbo + a sample city as plain text for the prompt."""
    if not openweather_key:
        return ""

    async def _fetch(city: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                geo = await client.get(
                    "https://api.openweathermap.org/geo/1.0/direct",
                    params={"q": city, "limit": 1, "appid": openweather_key},
                )
                if geo.status_code != 200 or not geo.json():
                    return ""
                lat, lon = geo.json()[0]["lat"], geo.json()[0]["lon"]

                fc = await client.get(
                    "https://api.openweathermap.org/data/2.5/forecast",
                    params={"lat": lat, "lon": lon, "appid": openweather_key, "units": "metric"},
                )
                if fc.status_code != 200:
                    return ""

            now = datetime.now(timezone.utc)
            lines = [f"{city}:"]
            for item in fc.json().get("list", [])[:4]:
                try:
                    dt = datetime.strptime(item["dt_txt"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    h = int((dt - now).total_seconds() / 3600)
                    label = "Now" if h <= 1 else f"+{h}h"
                    m = item.get("main", {})
                    w = (item.get("weather") or [{}])[0]
                    wd = item.get("wind", {})
                    rain = round((item.get("pop") or 0) * 100)
                    lines.append(
                        f"  {label}: {m.get('temp')}°C feels {m.get('feels_like')}°C, "
                        f"{w.get('description')}, {rain}% rain, "
                        f"wind {wd.get('speed')}m/s {_wind_dir(wd.get('deg', 0))}"
                    )
                except Exception:
                    continue
            return "\n".join(lines)
        except Exception:
            return ""

    results = await asyncio.gather(_fetch("Osogbo"), _fetch("a sample city"))
    return "\n".join(r for r in results if r)


def _wind_dir(deg: float) -> str:
    dirs = ["N","NE","E","SE","S","SW","W","NW"]
    return dirs[int((deg + 22.5) / 45) % 8]


# ── Context collectors ──

async def _core_memory(db, user_id: str) -> str:
    try:
        from memory.vault import MemoryVault
        vault = MemoryVault(db, user_id)
        doc = await vault.get_core_summary()
        return vault.render_core_for_injection(doc) if doc else ""
    except Exception:
        return ""


async def _relevant_extended_memories(db, user_id: str, context_query: str) -> str:
    """
    Pull the top-5 most semantically relevant extended memories for today's
    proactive context. Uses cosine similarity against a query that combines
    the current date, time of day, and recent conversation signal.

    This surfaces things like upcoming deadlines, exam reminders, or project
    milestones that are specifically relevant RIGHT NOW — not just the most
    recently updated memories.

    Falls back to empty string gracefully if embeddings aren't available yet.
    """
    try:
        from memory.vault import MemoryVault
        vault = MemoryVault(db, user_id)
        results = await vault.search(
            context_query,
            limit=5,
            exclude_categories=["core_summary", "reflection"],
        )
        if not results:
            return ""
        # BUGFIX 2026-09-01: `results` already carries each memory's real
        # `updated_at` (search() never strips it — only _relevance,
        # embedding, and embedding_model get popped) but this loop threw
        # it away before ever building the line — every memory, whether
        # saved 3 minutes or 3 months ago, rendered as an identically-
        # shaped "- key: content" with NOTHING distinguishing its actual
        # age. Confirmed as the real cause of a proactive message treating
        # a stale `active_project_path`/`technical_project_context` entry
        # as current: the raw data existed correctly in Mongo the whole
        # time, but the text handed to the model carried no timestamp at
        # all for the model to judge staleness from — a prompt-level
        # "treat memory as historical" instruction can't fix data that
        # looks equally fresh no matter how old it actually is. Now each
        # line states real relative age computed from updated_at, so the
        # model has the actual signal instead of nothing to reason from.
        now = datetime.now(timezone.utc)
        lines = []
        for m in results:
            key = m.get("key", "")
            content = str(m.get("content", ""))[:200]
            age_str = "age unknown"
            updated = m.get("updated_at")
            if updated:
                try:
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    secs = (now - updated).total_seconds()
                    if secs < 3600:
                        age_str = f"{max(1, int(secs // 60))}m ago"
                    elif secs < 86400:
                        age_str = f"{int(secs // 3600)}h ago"
                    elif secs < 86400 * 30:
                        age_str = f"{int(secs // 86400)}d ago"
                    else:
                        age_str = f"{int(secs // (86400 * 30))}mo+ ago — likely stale, verify before treating as current"
                except Exception:
                    pass
            lines.append(f"- [{age_str}] {key}: {content}")
        return "\n".join(lines)
    except Exception:
        return ""


async def _style_profile(db, user_id: str) -> str:
    """
    CONSOLIDATED 2026-09-22 — was a lookup against a completely SEPARATE
    `style_profiles` collection (its own independent learn_style()/
    maybe_learn_in_background() pipeline in core/style_profile.py), while
    normal chat replies get their personalization from the general
    MemoryVault instead (see core/orchestrator.py's _build_system_prompt,
    Tier 1/2 memory injection — response_pipeline.py's auto-extraction
    writes things like "user_slang: Nigerian Pidgin" straight into THAT
    vault via vault.store(), not into style_profiles at all).
    Two independent systems both claiming to track "how the user talks"
    meant proactive messages and normal chat could genuinely drift apart
    in voice, since neither one ever saw what the other had learned —
    confirmed as the actual live symptom (proactive pings sounding
    noticeably different from regular chat replies) that prompted this
    fix, not a theoretical concern.

    Now uses the exact same MemoryVault.search() pattern as this file's
    own _relevant_extended_memories() right below (see its docstring),
    searching for style/tone/slang-relevant memories instead of querying
    a second, separately-maintained collection. One source of truth for
    "how does this user talk" — whatever chat has taught the vault is
    what proactive messages draw from too, and vice versa.

    style_profiles.py's learn_style()/maybe_learn_in_background()
    pipeline itself is UNTOUCHED by this change (still callable directly,
    e.g. if ever exposed as an explicit user-facing "teach me your style"
    action) — this only removes it as an INPUT to proactive message
    generation specifically, replacing it with the general vault.

    AGE-LABELED 2026-09-22 — matches _relevant_extended_memories' own
    fix right above (see its BUGFIX 2026-09-01 comment for the full
    story): search() results carry a real updated_at, and rendering a
    memory with no age signal at all means the model can't tell "learned
    this yesterday" from "learned this 8 months ago and it may no longer
    be accurate" — both would have looked identically fresh otherwise.
    Deliberately gentler wording than that function's "likely stale,
    verify before treating as current" for anything past a month: a
    forgotten PROJECT PATH going stale is a hard factual error if reused
    as current; someone's SLANG/TONE drifting slowly over months is a
    much softer kind of staleness (worth flagging, not worth treating as
    probably-wrong) — so old style entries get "long-observed" rather
    than a verify-before-use warning, since second-guessing a person's
    own established voice on every proactive message would be worse than
    occasionally being a little behind on a recent shift.
    """
    try:
        from memory.vault import MemoryVault
        vault = MemoryVault(db, user_id)
        results = await vault.search(
            "how the user talks, communication style, tone, slang, pidgin, "
            "phrasing patterns, preferred way of being addressed",
            limit=5,
            exclude_categories=["core_summary", "reflection"],
        )
        if not results:
            return ""
        now = datetime.now(timezone.utc)
        lines = []
        for r in results:
            key = r.get("key", "")
            content = str(r.get("content", "")).strip()
            if not content:
                continue
            age_str = "age unknown"
            updated = r.get("updated_at")
            if updated:
                try:
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    secs = (now - updated).total_seconds()
                    if secs < 3600:
                        age_str = f"{max(1, int(secs // 60))}m ago"
                    elif secs < 86400:
                        age_str = f"{int(secs // 3600)}h ago"
                    elif secs < 86400 * 30:
                        age_str = f"{int(secs // 86400)}d ago"
                    else:
                        age_str = f"{int(secs // (86400 * 30))}mo+ ago — long-observed pattern"
                except Exception:
                    pass
            label = f"{key}: {content}".strip(": ")
            lines.append(f"[{age_str}] {label}")
        return " | ".join(lines)
    except Exception:
        return ""


async def _cross_surface_context(db, user_id: str, owner_chat_id: str, last_checked_at=None, limit: int = 10) -> str:
    """
    REPLACES the old fixed-window version (see below for full history —
    kept for reference, unused). That version was itself already a fix
    for a real bug (single-thread scoping — see its own note preserved
    below), but it introduced a NEW ceiling of its own: `{"messages":
    {"$slice": -6}}` on each session plus `limit(8)` sessions overall.
    For a lightly-used account that's plenty. For someone who talks to
    OADSON constantly (confirmed real case, 2026-08-29: an overnight
    conversation about specific plans got completely cut out of this
    window — either sliced off the tail of its own session by later
    unrelated messages, or its whole session pushed out of the top-8 by
    other chats since — and the proactive agent, working from what
    survived that cut, told the user a plan was "cancelled" that it had
    never actually seen discussed), those two fixed ceilings reproduce
    almost exactly the kind of silent, undifferentiated data loss this
    file's sibling module (memory/context_compressor.py) already exists
    to solve for regular chat — see that module's own docstring, "message
    21 and everything before it is simply invisible... no degradation
    curve." Same failure shape here, just in the proactive agent instead
    of the chat context builder.

    FIX 2026-08-29: query the FULL conversation across every surface
    since the last proactive check (or the last 24h on the very first
    run, when there's no prior check to bound from) — no per-session
    slice, no session-count cap, via the same $unwind aggregation
    skills/chat_history_search/skill.py already uses for "what did we
    discuss last week" (proven pattern, not a new one). If that window
    is small, return it verbatim — cheap, same as today's behavior. If
    it's large (a heavy chat night), reduce it with
    memory.context_compressor.summarize_message_range — the SAME
    summarization primitive regular chat already relies on for long
    sessions, reused here rather than reimplemented, so "the model runs
    a plan by you overnight across a dozen exchanges" now survives into
    the proactive agent's context as an actual summary of what was
    discussed, not a truncated, disconnected tail end of it.

    Cost stays bounded: the summarization call only fires when the
    window is genuinely large (VERBATIM_CAP below), which in practice
    means at most a handful of times a day for even a very chatty user
    — most 30-minute cycles see a short or empty window and pay zero
    extra cost over the old version.
    """
    # Above this many messages in the window, summarize instead of
    # sending verbatim — keeps the common case (a normal gap between
    # checks) exactly as cheap as before, and still costs only ONE
    # flash-lite call even on a very heavy chat window, not one per
    # session the way naively summarizing per-session would.
    VERBATIM_CAP = 40
    # First-ever run (no last_checked_at yet) needs SOME bound rather
    # than "since the beginning of time" — 24h is a reasonable "what's
    # happened lately" default that matches this function's own purpose
    # (situational awareness for a check-in), not an attempt to replay
    # full history.
    FALLBACK_WINDOW = timedelta(hours=24)

    try:
        window_start = last_checked_at if last_checked_at else (datetime.now(timezone.utc) - FALLBACK_WINDOW)
        if window_start.tzinfo is None:
            window_start = window_start.replace(tzinfo=timezone.utc)

        # BUGFIX 2026-09-01: messages.time in ai_sessions is written as a
        # REAL BSON date (core/response_pipeline.py: `now =
        # datetime.utcnow()`, stored directly, never .isoformat()'d into
        # a string — confirmed by reading that write site directly, not
        # assumed). This query was comparing that real Date field against
        # a STRING (window_start.isoformat()) in $gte. MongoDB's BSON
        # type ordering places Date categorically ABOVE String regardless
        # of value — so `{dateField: {$gte: "any_string"}}` matches EVERY
        # document with that field set, no matter how old, because the
        # type comparison alone (Date > String) decides it before the
        # actual timestamps are ever considered. This is the literal
        # mechanism behind "the proactive agent always treats stored
        # conversation history as recent, even when it's genuinely old"
        # — not a fuzzy timing/prompting issue, a real type-mismatch bug
        # in the query that made the $gte filter a no-op.
        #
        # Fixed by comparing against a real datetime instead of a string
        # — matching the correct, already-established pattern used by
        # this same function's own wa_logs query a few lines below, and
        # by skills/chat_history_search/skill.py's search_by_date (same
        # $unwind-across-sessions shape, comparing against a real
        # datetime there too). Stripped back to naive-UTC to match what's
        # actually stored (datetime.utcnow(), no tzinfo) — comparing a
        # naive stored value against an aware query value risks its own
        # driver-dependent inconsistency, so both sides now use the same
        # naive-UTC convention this codebase already uses for writes.
        window_start_for_query = window_start.replace(tzinfo=None)

        # Same $unwind-across-all-sessions pattern as
        # skills/chat_history_search/skill.py's search_by_date — reaches
        # every message in every session for this user in the window,
        # not just the tail of whichever sessions happen to sort into a
        # fixed top-N.
        pipeline = [
            {"$match": {"user_id": user_id}},
            {"$project": {"session_id": 1, "surface_origin": 1, "messages": 1}},
            {"$unwind": "$messages"},
            {"$match": {"messages.time": {"$gte": window_start_for_query}}},
            {"$sort": {"messages.time": 1}},
            {"$project": {
                "_id": 0,
                "role": "$messages.role",
                "content": "$messages.content",
                "time": "$messages.time",
                "surface": {"$ifNull": ["$surface_origin", "web"]},
            }},
        ]
        matched = [doc async for doc in db["ai_sessions"].aggregate(pipeline)]

        # NORMALIZE 2026-09-01: `matched` now mixes real datetime objects
        # (ai_sessions.messages.time, straight from Mongo as a BSON date —
        # see the $gte fix above for why this is now correct) with
        # string timestamps (wa_logs below, built as ts_str). Sorting or
        # comparing across mixed datetime/str types raises TypeError in
        # Python 3 — normalize every "time" to a plain ISO string with a
        # 'T' separator right here, once, before either source is used
        # for anything else, rather than letting each downstream
        # consumer (sort key, "ago" calculation) guess at the type.
        for m in matched:
            t = m.get("time")
            if isinstance(t, datetime):
                m["time"] = t.isoformat()

        # whatsapp_log for this thread specifically — same as the old
        # version, kept as one additional input for messages that never
        # made it into a formal ai_sessions doc (see old function's own
        # note on why this source exists at all).
        wa_logs = await db["whatsapp_log"].find(
            {"chat_id": owner_chat_id, "timestamp": {"$gte": window_start}},
        ).sort("timestamp", 1).to_list(200)
        for m in wa_logs:
            ts = m.get("timestamp")
            ts_str = ts.replace(tzinfo=timezone.utc).isoformat() if ts else None
            if m.get("message"):
                matched.append({"role": "user", "content": m.get("message") or "", "time": ts_str, "surface": "whatsapp"})
            if m.get("response"):
                matched.append({"role": "assistant", "content": m.get("response") or "", "time": ts_str, "surface": "whatsapp"})

        if not matched:
            return "No recent messages."

        matched.sort(key=lambda m: m.get("time") or "")

        # Cheap dedup — same content within the window across both
        # sources (ai_sessions + whatsapp_log recording the same real
        # exchange) shouldn't appear twice.
        seen = set()
        deduped = []
        for m in matched:
            key = (m.get("role"), (m.get("content") or "")[:60])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(m)

        if len(deduped) <= VERBATIM_CAP:
            # Small window — same cheap, verbatim path as before.
            # Trimmed to the most recent `limit` for prompt size, but
            # from a window that was never sliced by session in the
            # first place, so this cap can only ever cut older parts of
            # an already-small window, never lose an entire earlier
            # session to a session-count ceiling the way the old
            # version could.
            recent = deduped[-limit:] if len(deduped) > limit else deduped
            now = datetime.now(timezone.utc)
            lines = []
            for m in recent:
                ago = ""
                if m.get("time"):
                    try:
                        # m["time"] is now always a plain string (see
                        # normalization above) — .replace("Z", "+00:00")
                        # handles the naive-UTC-stored-with-Z-suffix
                        # convention used elsewhere in this codebase (see
                        # core/response_pipeline.py); a naive isoformat
                        # string with neither suffix (from the BSON-date
                        # normalization just above, which has no tzinfo
                        # to begin with) also parses fine via
                        # fromisoformat and is then treated as UTC below.
                        ts = datetime.fromisoformat(str(m["time"]).replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        secs = (now - ts).total_seconds()
                        ago = f"{int(secs // 60)}m ago" if secs < 3600 else (f"{int(secs // 3600)}h ago" if secs < 86400 else f"{int(secs // 86400)}d ago")
                    except Exception:
                        pass
                who = "You" if m.get("role") == "user" else "OADSON"
                surface_tag = f" [{m['surface']}]" if m.get("surface") and m["surface"] != "whatsapp" else ""
                content = (m.get("content") or "")[:150]
                lines.append(f"[{ago}]{surface_tag} {who}: {content}")
            return "\n".join(lines)

        # Large window — summarize with the same primitive regular chat
        # uses for long sessions, instead of silently truncating.
        from memory.context_compressor import summarize_message_range
        summary = await summarize_message_range(deduped)
        if summary:
            return f"[Summary of {len(deduped)} messages since last check]\n{summary}"
        # Summarization unavailable (no API key, transient failure) —
        # degrade to the same verbatim-tail behavior as the old version
        # rather than returning nothing.
        recent = deduped[-limit:]
        lines = [f"[{'You' if m.get('role')=='user' else 'OADSON'}]: {(m.get('content') or '')[:150]}" for m in recent]
        return "\n".join(lines)

    except Exception as e:
        # FIX 2026-08-29: was logger.warning(f"...: {e}") with no
        # traceback — confirmed real problem: this swallowed whatever
        # actually broke on the live server (self-test showed the
        # SYMPTOM — an empty string reaching the caller — with zero
        # trace of the real exception anywhere, making it undiagnosable
        # from logs alone). exc_info=True gets the real stack trace into
        # the logs on the next occurrence instead of just a bare message.
        logger.warning(f"_cross_surface_context failed: {e}", exc_info=True)
        return ""


async def _cross_surface_context_OLD_fixed_window(db, user_id: str, owner_chat_id: str, limit: int = 10) -> str:
    """
    REPLACES _recent_convo (kept below, unused, for reference — see its
    own note) as the proactive agent's situational-awareness source.

    BUG FIXED 2026-08-22 — confirmed real report: the proactive agent
    kept repeating something the user had explicitly told it to stop
    doing, until deleting the WhatsApp chat that contained that
    correction "fixed" it. Root cause: _recent_convo only ever read the
    last 6 messages from whatsapp_log filtered to ONE owner_chat_id — so
    the agent's entire sense of "what have we recently talked about" was
    scoped to a single WhatsApp thread, with zero visibility into web
    sessions, other WhatsApp numbers, or anything older than 6 messages
    back on that one thread. A correction said on web, or said on
    WhatsApp but pushed out of the 6-message window by later small talk,
    was invisible to this function even though the user considered it
    still in force. core_memory/relevant_extended_memories (the OTHER
    two context sources this function's caller gathers) were already
    correctly user-scoped, not chat-scoped — this was the one narrow
    piece.

    This pulls recent messages from ai_sessions across EVERY surface
    (web, whatsapp, telegram, app — anywhere the user actually talks to
    OADSON) for this user_id, merges them with whatsapp_log's own record
    of this specific thread (kept as one input, not the only one — see
    module notes on why it's not simply dropped), sorts by true
    chronological recency across all of it, and returns the most recent
    `limit` exchanges as one unified view. "General knowledge across
    chats" in practice: the agent now sees what was actually said
    recently, regardless of which surface or which specific
    conversation it happened in — not just the one thread this
    particular check-in happens to be about to message on.
    """
    try:
        merged = []

        # Source 1: ai_sessions across ALL surfaces for this user —
        # the actual fix. Sorted by updated_at so we look at the most
        # recently active sessions first, not an arbitrary order.
        cursor = db["ai_sessions"].find(
            {"user_id": user_id},
            {"messages": {"$slice": -6}, "updated_at": 1, "surface_origin": 1},
        ).sort("updated_at", -1).limit(8)  # a handful of recent sessions, not every session ever
        async for sess in cursor:
            surface = sess.get("surface_origin", "web")
            for m in sess.get("messages", []):
                ts = m.get("time")
                # Stored times are ISO strings (see api/sessions.py's
                # _serialise_message) — parse defensively, skip on any
                # format surprise rather than let one bad timestamp break
                # ordering for everything else.
                parsed_ts = None
                if ts:
                    try:
                        parsed_ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    except Exception:
                        parsed_ts = None
                merged.append({
                    "ts": parsed_ts,
                    "role": m.get("role", "user"),
                    "content": (m.get("content") or "")[:150],
                    "surface": surface,
                })

        # Source 2: whatsapp_log for this specific thread — kept as ONE
        # input (not the only one anymore), since it may hold messages
        # that never made it into a formal ai_sessions doc.
        wa_logs = await db["whatsapp_log"].find(
            {"chat_id": owner_chat_id},
        ).sort("timestamp", -1).limit(6).to_list(6)
        for m in wa_logs:
            ts = m.get("timestamp")
            parsed_ts = ts.replace(tzinfo=timezone.utc) if ts else None
            if m.get("message"):
                merged.append({"ts": parsed_ts, "role": "user", "content": (m.get("message") or "")[:150], "surface": "whatsapp"})
            if m.get("response"):
                merged.append({"ts": parsed_ts, "role": "assistant", "content": (m.get("response") or "")[:150], "surface": "whatsapp"})

        if not merged:
            return "No recent messages."

        # Sort by true recency across every surface — entries with no
        # parseable timestamp sort as oldest rather than crashing or
        # skewing the order unpredictably.
        merged.sort(key=lambda e: e["ts"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

        # Cheap dedup — same content within a few minutes of each other
        # across two sources (ai_sessions + whatsapp_log both recording
        # the same real exchange) shouldn't show up twice.
        seen = set()
        deduped = []
        for e in merged:
            key = (e["role"], e["content"][:60])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(e)
            if len(deduped) >= limit:
                break

        now = datetime.now(timezone.utc)
        lines = []
        for e in reversed(deduped):  # oldest of the selected window first, most recent last
            ago = ""
            if e["ts"]:
                secs = (now - e["ts"]).total_seconds()
                if secs < 3600:
                    ago = f"{int(secs // 60)}m ago"
                elif secs < 86400:
                    ago = f"{int(secs // 3600)}h ago"
                else:
                    ago = f"{int(secs // 86400)}d ago"
            who = "You" if e["role"] == "user" else "OADSON"
            surface_tag = f" [{e['surface']}]" if e["surface"] != "whatsapp" else ""
            lines.append(f"[{ago}]{surface_tag} {who}: {e['content']}")
        return "\n".join(lines)
    except Exception:
        return ""


async def _recent_convo(db, owner_chat_id: str) -> str:
    # SUPERSEDED 2026-08-22 by _cross_surface_context (see that function's
    # own docstring for the full rationale — this was scoped to a single
    # WhatsApp chat_id, which is the actual bug that got fixed). Left
    # here, unused by the real call site below, only in case anything
    # else still imports it directly; safe to delete outright in a
    # future cleanup pass once confirmed nothing does.
    try:
        logs = await db["whatsapp_log"].find(
            {"chat_id": owner_chat_id},
        ).sort("timestamp", -1).limit(6).to_list(6)

        if not logs:
            return "No recent messages."

        now = datetime.now(timezone.utc)
        lines = []
        for m in reversed(logs):
            ts = m.get("timestamp")
            ago = ""
            if ts:
                secs = (now - ts.replace(tzinfo=timezone.utc)).total_seconds()
                if secs < 3600:
                    ago = f"{int(secs // 60)}m ago"
                elif secs < 86400:
                    ago = f"{int(secs // 3600)}h ago"
                else:
                    ago = f"{int(secs // 86400)}d ago"
            lines.append(f"[{ago}] You: {(m.get('message') or '')[:100]}")
            resp = (m.get("response") or "")[:100]
            if resp:
                lines.append(f"[{ago}] OADSON: {resp}")
        return "\n".join(lines)
    except Exception:
        return ""


async def _reminder_context(db, user_id: str, now_wat) -> str:
    """Pull due/upcoming reminders with human-readable time-until."""
    try:
        from bootstrap import get
        registry = get("skill_registry")
        if not registry.has("reminder"):
            return ""
        result = await registry.execute("reminder", {"args": {"action": "check"}, "user_id": user_id})
        data = result.get("data") or result
        reminders = data.get("due") or data.get("reminders") or []
        if not reminders:
            return ""
        lines = []
        now_utc = datetime.now(timezone.utc)
        for r in reminders[:5]:
            name = r.get("task_name") or str(r)
            deadline = r.get("deadline")
            if deadline:
                try:
                    if hasattr(deadline, "timestamp"):
                        dl = deadline.replace(tzinfo=timezone.utc) if deadline.tzinfo is None else deadline
                    else:
                        dl = datetime.fromisoformat(str(deadline)).replace(tzinfo=timezone.utc)
                    diff = dl - now_utc
                    hours = diff.total_seconds() / 3600
                    if hours < 0:
                        when = "OVERDUE"
                    elif hours < 1:
                        when = f"in {int(diff.total_seconds()/60)} mins"
                    elif hours < 24:
                        when = f"in {int(hours)}h"
                    else:
                        when = f"in {int(hours/24)}d ({dl.strftime('%A %d %b')})"
                    lines.append(f"- {name}: {when}")
                except Exception:
                    lines.append(f"- {name}: {deadline}")
            else:
                lines.append(f"- {name}")
        return "DUE/UPCOMING REMINDERS:\n" + "\n".join(lines)
    except Exception:
        return ""


async def _calendar_context(db, user_id: str, now_wat) -> str:
    """Pull upcoming calendar events with human-readable time-until."""
    try:
        from bootstrap import get
        registry = get("skill_registry")
        if not registry.has("google_calendar"):
            return ""
        result = await registry.execute("google_calendar", {"args": {"action": "upcoming", "max_results": 5}, "user_id": user_id})
        data = result.get("data") or result
        events = data.get("events") or []
        if not events:
            return ""
        lines = []
        now_utc = datetime.now(timezone.utc)
        for e in events[:5]:
            title = e.get("title") or e.get("summary") or "Event"
            start_raw = e.get("start", "")
            try:
                if "T" in str(start_raw):
                    start_dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=timezone.utc)
                    diff = start_dt - now_utc
                    hours = diff.total_seconds() / 3600
                    if hours < 0:
                        when = f"started {abs(int(hours))}h ago"
                    elif hours < 1:
                        when = f"in {int(diff.total_seconds()/60)} mins"
                    elif hours < 24:
                        when = f"today at {start_dt.strftime('%H:%M')}"
                    elif hours < 48:
                        when = f"tomorrow at {start_dt.strftime('%H:%M')}"
                    else:
                        when = start_dt.strftime("%A %d %b at %H:%M")
                    lines.append(f"- {title}: {when}")
                else:
                    # all-day event, just a date string
                    lines.append(f"- {title}: {start_raw}")
            except Exception:
                lines.append(f"- {title}: {start_raw}")
        return "UPCOMING CALENDAR:\n" + "\n".join(lines)
    except Exception:
        return ""


async def _unread_context(db, user_id: str) -> str:
    """Get unread Gmail count — not the full list, just the number."""
    try:
        from bootstrap import get
        registry = get("skill_registry")
        if not registry.has("gmail"):
            return ""
        result = await registry.execute("gmail", {"args": {"action": "count"}, "user_id": user_id})
        data = result.get("data") or result
        count = data.get("unread_count") or data.get("count") or 0
        if not count or int(count) == 0:
            return ""
        return f"UNREAD EMAILS: {count} unread in Gmail."
    except Exception:
        return ""


# ── The decision call ──

async def _decide(
    db,
    user_id: str,
    owner_chat_id: str,
    gemini_key: str,
    openweather_key: str,
    meta: dict,
) -> tuple:
    """
    Ask Gemini: should OADSON message the owner right now, and what should it say?
    Returns (should_send: bool, message: str, format: str).
    """
    now_wat = _now_wat()
    today_str = now_wat.strftime("%Y-%m-%d")
    daily_count = meta.get("daily_count", 0) if meta.get("daily_date") == today_str else 0

    last_sent = meta.get("last_sent_at")
    last_sent_str = "never"
    if last_sent:
        h = round((datetime.now(timezone.utc) - last_sent.replace(tzinfo=timezone.utc)).total_seconds() / 3600, 1)
        last_sent_str = f"{h}h ago"

    core, style, convo, weather, reminders, calendar, unread = await asyncio.gather(
        _core_memory(db, user_id),
        _style_profile(db, user_id),
        _cross_surface_context(db, user_id, owner_chat_id, last_checked_at=meta.get("last_checked_at")),
        _weather_context(openweather_key),
        _reminder_context(db, user_id, now_wat),
        _calendar_context(db, user_id, now_wat),
        _unread_context(db, user_id),
    )

    # Load proactive voice style override — set when user tells OADSON
    # in normal chat to change how it sounds in proactive messages.
    # Stored in proactive_config doc, falls back to butler-English default
    # defined in the prompt below if not set.
    _proactive_config = await db["proactive_config"].find_one({"user_id": user_id}) or {}
    style_voice_override = _proactive_config.get("voice_style", "")

    # Retrieve the top-5 most relevant extended memories for today's context.
    # Query combines day-of-week, time of day, and recent conversation signal
    # so OADSON finds memories that are specifically relevant RIGHT NOW.
    time_context_query = (
        f"{now_wat.strftime('%A')} morning {now_wat.strftime('%H:%M')} WAT. "
        f"Recent: {convo[:150] if convo else 'no recent conversation'}"
    )
    relevant_memories = await _relevant_extended_memories(db, user_id, time_context_query)

    last_msg = meta.get("last_message", "")

    # Build context block — only include what's actually available
    context_sections = []
    if weather:
        context_sections.append(f"WEATHER (next 12 hours):\n{weather}")
    if reminders:
        context_sections.append(reminders)
    if calendar:
        context_sections.append(calendar)
    if unread:
        context_sections.append(unread)
    context_block = "\n\n".join(context_sections) if context_sections else "No additional context available."

    # Birthday detection from core memory
    birthday_hint = ""
    if core and ("birthday" in core.lower() or "born" in core.lower()):
        now_str = now_wat.strftime("%d %B").lower()
        if now_str in core.lower() or now_wat.strftime("%m-%d") in core:
            birthday_hint = f"\n⚠️ TODAY IS {now_wat.strftime('%d %B')} — this may be the owner's birthday based on core memory. If so, acknowledge it naturally.\n"

    # Hour-of-day greeting hint
    hour = now_wat.hour
    if 5 <= hour < 12:
        time_of_day = "morning"
        greeting_hint = "Morning check-in is appropriate — can ask how he slept or flag the day ahead."
    elif 12 <= hour < 17:
        time_of_day = "afternoon"
        greeting_hint = "Afternoon — can check in on how the day's going if it's been a while."
    elif 17 <= hour < 21:
        time_of_day = "evening"
        greeting_hint = "Evening — good time to ask how his day went, especially if you haven't talked much today."
    else:
        time_of_day = "night"
        greeting_hint = "Late night — keep it brief, don't start a long conversation. Only message if something is actually time-sensitive."

    prompt = f"""You are OADSON — the owner's personal AI assistant. You can reach out to him on WhatsApp at any time, on your own initiative.

TODAY IS: {now_wat.strftime("%A, %d %B %Y")} — all dates/times below are relative to this. Do NOT reference past events as if they are upcoming.
CURRENT TIME: {now_wat.strftime("%H:%M WAT")} ({time_of_day})
Last time you messaged him unprompted: {last_sent_str}
Last proactive message you sent: "{last_msg[:120] if last_msg else 'none yet'}"
Proactive messages sent today: {daily_count}/{MAX_PER_DAY}
{birthday_hint}
WHO HE IS (your core memory):
{core or 'No core memory yet.'}

⚠️ CORE MEMORY IS A SNAPSHOT, NOT A LIVE STATUS FEED. It reflects what
was true when it was last saved (see the version/date on its own
header line) — NOT what the owner is doing, working on, or available
for right now, and NOT necessarily still-accurate technical facts (a
project's tech stack, an integration in progress, a tool choice) —
those can have changed or been superseded since the entry was saved. A
field like an active project path, a "currently working on X" note, or
any other state-sounding entry is HISTORICAL CONTEXT ONLY. Never infer
his current availability, mood, location, or what he's doing at this
exact moment from it, and never assert a specific technical detail
back to him as current fact ("you're building X with Y") on memory
alone — if you don't have a genuinely current signal (recent
conversation, calendar, an explicit recent message), say nothing about
his current state or project specifics rather than guessing from a
memory entry that could be days or weeks stale. This applies to every
section below, not just this one.

RELEVANT EXTENDED MEMORIES (semantically closest to right now):
{relevant_memories or 'None retrieved yet — core memory above is the primary source.'}
Same caution as above — a memory being semantically relevant to "right
now" means its WORDING matches the current moment, not that its
CONTENT is still current. A memory about a project, tool, or task is
context about something that was once true, not a live status check.

RECENT CONVERSATION:
{convo}

WHAT YOU KNOW RIGHT NOW:
{context_block}

HIS COMMUNICATION STYLE (how he talks to you in chat):
{style or "Direct and casual. Adapt to whatever tone he uses with you."}

VOICE NOTE STYLE (how YOU should sound when sending voice notes):
{style_voice_override or "Warm, composed, butler-English. Speak like a trusted personal assistant — clear diction, no pidgin, no slang. Natural and human, not robotic. Morning greetings should feel like a calm, well-spoken attendant setting the tone for his day."}

TIME-OF-DAY CONTEXT:
{greeting_hint}

YOUR DECISION:
Should you message the owner RIGHT NOW? Consider all of the following — pick the MOST relevant reason, not all of them:

1. GREETINGS/CHECK-INS: A genuine "how was your day" or "good morning" is valid — but ONLY if you haven't already done one today and it's an appropriate time. Sound like a friend, not a scheduled notification.
2. REMINDERS: Flag anything due soon (today or tomorrow). If overdue, mention it once.
3. CALENDAR: Mention upcoming events that are today or within 24 hours — not distant future ones.
4. EMAIL: Only flag unread count if it's notably high (15+). Don't mention it every time.
5. WEATHER: Only if it's relevant to something he might do today (going out, an event, etc.).
6. MEMORY-BASED: Birthday, deadline, milestone from his core memory that's relevant TODAY specifically — but ONLY a genuinely dated/recurring fact (a birthday, a stated deadline date). Never a project name, tool, status, or "currently working on" style entry from memory — those are historical snapshots, not live status, and referencing them as if they describe what's happening right now is a hallucination, not a real reason to reach out.

Rules:
- All dates/times you reference must be computed relative to today ({now_wat.strftime("%d %B %Y")}). Never say "upcoming" about something that already passed.
- Don't send noise. One good reason beats six weak ones.
- Keep it short, punchy, WhatsApp-native. Pidgin when it fits.
- Don't repeat last message topic unless something materially changed.
- Don't say "Hello" and then the actual message — just the message.
- CRITICAL: before sending, check the core memory and recent conversation
  above for any standing instruction to NOT bring up a topic, NOT check in
  about something, or reduce/stop a specific kind of message. If the owner
  has told you to stop doing something, that instruction stays in force
  indefinitely — it's not scoped to only the next few messages after it
  was said. Don't repeat something he corrected just because it's aged
  out of the last few messages you can see; a standing correction outranks
  every reason listed above, including a genuine one.

Respond with ONLY this JSON (no other text):
{{"send": true, "message": "the actual message", "format": "text"}}
or
{{"send": true, "message": "the actual message to speak", "format": "voice"}}
or
{{"send": false, "reason": "why not now"}}

Use "voice" format for warm, personal messages like morning greetings, check-ins, birthday wishes, or "how was your day" — the kind of thing that feels more human spoken than typed. Use "text" for information-dense messages like reminders, weather, email counts, or calendar events where the user needs to read and reference the content."""

    try:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-3.5-flash-lite:generateContent"
        )
        # SECURITY FIX 2026-08-14: key moved from URL to x-goog-api-key
        # header — was leaking into Render's httpx request logs.
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(url, json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 300, "temperature": 0.85},
            }, headers={"x-goog-api-key": gemini_key})

        if res.status_code != 200:
            # FIX 2026-08-29: was `return False, ""` — a bare 2-tuple
            # while every other return path here returns 3 (send, message,
            # format). Confirmed as a live production crash: a transient
            # Gemini 503 (high demand — seen in the same deploy's logs
            # minutes later, "context_compressor: flash-lite returned
            # 503") hit this exact branch, and the caller's
            # `should_send, message, msg_format = await _decide(...)`
            # unpack blew up with "not enough values to unpack (expected
            # 3, got 2)", 500-ing /engineer/test-proactive. The real
            # unattended 30-min proactive_loop calls _decide the same way
            # (see its own unpack below), so this wasn't just an
            # engineer-panel-only bug — a transient provider error at the
            # wrong moment could have crashed a real proactive check too.
            return False, "", "text"

        raw = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
        data = _json.loads(raw)

        if data.get("send") and data.get("message"):
            fmt = data.get("format", "text")
            return True, data["message"].strip(), fmt
        logger.info(f"Proactive: no message. Reason: {data.get('reason', '?')}")
        return False, "", "text"

    except Exception as e:
        logger.warning(f"Proactive decision call failed: {e}")
        return False, "", "text"


# ── Watchdog ──

async def _run_watchdog(db, owner_chat_id: str, send_fn) -> None:
    """
    Added 2026-08-07. Runs every cycle, independent of the AI-driven
    "should I say something nice" decision below it. This is the
    difference between "you have to notice something's wrong and ask
    OADSON to check" and "OADSON tells you before you notice" — every
    bug fixed in this file up to today was found the first way.

    Deliberately NOT AI-generated: an ops alert needs to be reliable
    and boring, not personality-matched. Checks:

      0. Is MongoDB itself reachable? Run FIRST, standalone, before
         anything that assumes bridge_settings is readable — because
         if THIS fails, nothing else below can persist cooldown state
         or even read watchdog_meta. No cooldown tracking is possible
         in that scenario (nowhere to store "already alerted"), so a
         genuine DB outage alerts every single cycle until fixed —
         intentional: repeated pings for "your database is down" is
         the correct behavior, not spam.

      1. Did the last proactive send actually succeed? (reads the same
         meta doc /engineer/proactive and system_status's
         'proactive_status' action read — if THIS watchdog message
         also fails to send, there's nothing left to do about it here,
         but at least it tried and logged why.)

      2. Skill error rate over the last 2 hours, from db.skill_executions
         (written by core/event_handlers.py on every single skill
         call). Flags any skill with >=3 calls and a >50% error rate —
         a threshold chosen to survive a couple of legitimate one-off
         failures (bad user input, a flaky external API) without
         alerting, while still catching "this skill is actually broken."

      (A third check — polling GreenAPI's getStateInstance to see whether
      the WhatsApp instance was still authorized — was REMOVED 2026-09-19
      at the owner's request.)

    Rate-limited independently per issue type (6h) so a stuck failure
    state doesn't spam a message every 30 minutes forever.
    """
    # ── Check 0: MongoDB itself reachable? Standalone — no cooldown
    # persistence possible if this is the thing that's actually down.
    #
    # FIX 2026-08-08 (same day, caught live via this exact alert firing
    # in production — the watchdog catching its own bug is a good sign
    # the alerting itself works): this used to call
    # db.client.admin.command("ping"), which assumes db is a raw
    # AsyncIOMotorDatabase. It's actually MultiClusterDB
    # (core/db_router.py) — a wrapper managing several underlying Mongo
    # clusters, no single '.client' attribute by design. It already has
    # exactly the right method for this: ping_all(), which checks EVERY
    # configured cluster individually rather than just one — strictly
    # better than what this was trying to do, since with 2+ clusters,
    # one can be down while the other's fine, and a single blanket
    # ping() would either mask that or false-alarm on a healthy setup.
    try:
        cluster_status = await db.ping_all()
        unhealthy = {name: status for name, status in cluster_status.items() if status != "ok"}
        if unhealthy:
            raise RuntimeError("; ".join(f"{name}: {status}" for name, status in unhealthy.items()))
    except Exception as e:
        logger.error(f"Watchdog: MongoDB cluster check failed: {e}")
        try:
            ok, detail, _ = await send_fn(owner_chat_id, f"⚠️ WATCHDOG: MongoDB cluster issue — {e}")
            logger.info(f"Watchdog DB-down alert {'sent' if ok else 'FAILED to send'}: {detail}")
        except Exception as send_e:
            logger.error(f"Watchdog: DB-down alert also failed to send: {send_e}")
        return  # nothing else below can run meaningfully without the DB

    try:
        watchdog_meta = (await db["bridge_settings"].find_one({"_id": "watchdog_meta"})) or {}
        last_alerts = watchdog_meta.get("last_alerts", {})
        now = datetime.now(timezone.utc)

        def _should_alert(issue_key: str, cooldown_hours: int = 6) -> bool:
            last = last_alerts.get(issue_key)
            if not last:
                return True
            last = last.replace(tzinfo=timezone.utc) if last.tzinfo is None else last
            return (now - last).total_seconds() > cooldown_hours * 3600

        alerts_to_send = []

        # ── Check 1: did the last proactive send fail? ──
        proactive_meta = await _get_meta(db)
        if proactive_meta.get("last_send_ok") is False and _should_alert("proactive_send_failed"):
            alerts_to_send.append(
                f"⚠️ WATCHDOG: last proactive WhatsApp send failed — {proactive_meta.get('last_error', 'unknown error')}"
            )
            last_alerts["proactive_send_failed"] = now

        # ── Check 2: skill error rates, last 2 hours ──
        cutoff = now - timedelta(hours=2)
        pipeline = [
            {"$match": {"timestamp": {"$gte": cutoff}}},
            {"$group": {
                "_id": "$skill",
                "total": {"$sum": 1},
                "errors": {"$sum": {"$cond": [{"$in": ["$status", ["error", "timeout", "blocked"]]}, 1, 0]}},
            }},
        ]
        broken_skills = []
        async for row in db["skill_executions"].aggregate(pipeline):
            skill_name = row["_id"]
            total, errors = row["total"], row["errors"]
            if skill_name and total >= 3 and (errors / total) > 0.5:
                broken_skills.append(f"{skill_name} ({errors}/{total} failed)")

        if broken_skills:
            issue_key = "skill_errors:" + ",".join(sorted(s.split(" (")[0] for s in broken_skills))
            if _should_alert(issue_key):
                alerts_to_send.append(
                    "⚠️ WATCHDOG: high error rate in the last 2h — " + "; ".join(broken_skills)
                )
                last_alerts[issue_key] = now

        # ── Check 3 (GreenAPI getStateInstance poll) REMOVED 2026-09-19 ──
        # Removed at the owner's request. The remaining checks above (DB
        # reachable, last proactive send failed, skill error rates) are
        # unchanged. /whatsapp/status in api/whatsapp.py still exposes the
        # instance state on demand if it's ever needed.

        # ── Send whatever fired, save cooldown state either way ──
        for alert_msg in alerts_to_send:
            ok, detail, _ = await send_fn(owner_chat_id, alert_msg)
            logger.info(f"Watchdog alert {'sent' if ok else 'FAILED to send'}: {alert_msg} ({detail})")

        if alerts_to_send:
            await db["bridge_settings"].update_one(
                {"_id": "watchdog_meta"},
                {"$set": {"last_alerts": last_alerts, "last_run_at": now}},
                upsert=True,
            )
        else:
            await db["bridge_settings"].update_one(
                {"_id": "watchdog_meta"},
                {"$set": {"last_run_at": now}},
                upsert=True,
            )
    except Exception as e:
        # Watchdog failing must never take down the main proactive
        # loop below it — log and move on.
        logger.error(f"Watchdog check itself failed: {e}", exc_info=True)


# ── Main loop ──

async def proactive_loop(db_getter, send_fn) -> None:
    """
    Background loop. Checks every 30 minutes.
    db_getter: callable → db
    send_fn: async (chat_id: str, text: str) → (ok: bool, detail: str, msg_id: str | None)
    """
    logger.info("🤖 Proactive agent started.")

    gemini_key = os.getenv("GEMINI_API_KEY", "")
    openweather_key = os.getenv("OPENWEATHER_API_KEY", "")
    owner_number = os.getenv("WHATSAPP_OWNER_NUMBER", "").strip()

    if not owner_number:
        logger.warning("WHATSAPP_OWNER_NUMBER not set — proactive agent disabled.")
        return
    if not gemini_key:
        logger.warning("GEMINI_API_KEY not set — proactive agent disabled.")
        return

    # strip country code formatting — see build_owner_chat_id() docstring
    # for why this is a real, separately-testable function rather than
    # inline string manipulation.
    owner_chat_id = build_owner_chat_id(owner_number)

    await asyncio.sleep(60)  # wait 1 minute after startup before first check

    while True:
        try:
            db = db_getter()

            user = await db["users"].find_one({})
            if not user:
                await asyncio.sleep(CHECK_INTERVAL_SECS)
                continue
            user_id = str(user["_id"])

            now_wat = _now_wat()
            now_utc = datetime.now(timezone.utc)
            today_str = now_wat.strftime("%Y-%m-%d")

            # Hard sleep window — never message
            if HARD_SLEEP_START <= now_wat.hour < HARD_SLEEP_END:
                logger.debug(f"Hard sleep window ({now_wat.hour}h WAT) — skipping.")
                await asyncio.sleep(CHECK_INTERVAL_SECS)
                continue

            # Watchdog runs every cycle regardless of daily cap / min
            # gap / active-hours — those gates exist to keep chatty
            # proactive messages from being annoying, not to suppress
            # "your bot is broken" alerts. Still respects hard sleep
            # above (no 3am pages for a non-emergency).
            await _run_watchdog(db, owner_chat_id, send_fn)

            meta = await _get_meta(db)

            # Daily cap
            daily_count = meta.get("daily_count", 0) if meta.get("daily_date") == today_str else 0
            if daily_count >= MAX_PER_DAY:
                logger.info(f"Daily cap reached ({daily_count}) — skipping.")
                await asyncio.sleep(CHECK_INTERVAL_SECS)
                continue

            # Minimum gap
            last_sent = meta.get("last_sent_at")
            if last_sent:
                elapsed = (now_utc - last_sent.replace(tzinfo=timezone.utc)).total_seconds()
                if elapsed < MIN_GAP_SECS:
                    await asyncio.sleep(CHECK_INTERVAL_SECS)
                    continue

            # Activity window
            active = await _active_hours(db)
            h = now_wat.hour
            in_window = any(abs(h - ah) <= 1 for ah in active)
            if not in_window:
                logger.debug(f"Hour {h} WAT outside active window {active} — skipping.")
                await asyncio.sleep(CHECK_INTERVAL_SECS)
                continue

            # Ask the AI
            should_send, message, msg_format = await _decide(
                db=db,
                user_id=user_id,
                owner_chat_id=owner_chat_id,
                gemini_key=gemini_key,
                openweather_key=openweather_key,
                meta=meta,
            )

            if should_send and message:
                sent_msg_id = None
                if msg_format == "voice":
                    ok, detail = await _send_whatsapp_voice(owner_chat_id, message, user_id)
                else:
                    ok, detail, sent_msg_id = await send_fn(owner_chat_id, message)
                if ok:
                    logger.info(f"Proactive message sent [{now_wat.strftime('%H:%M WAT')}] ({msg_format}): {message[:80]}")
                    meta_update = {
                        "last_sent_at": now_utc,
                        "last_message": message,
                        "last_format": msg_format,
                        "daily_count": daily_count + 1,
                        "daily_date": today_str,
                        "last_send_ok": True,
                        "last_error": None,
                        "last_checked_at": now_utc,
                    }
                    # Save the outbound message ID so incoming quotedMessage
                    # webhooks can be matched back to this specific proactive
                    # message. whatsapp.py checks quoted stanzaId against this
                    # to know "user replied to our proactive message" and injects
                    # proper context instead of treating it as a cold new message.
                    if sent_msg_id:
                        meta_update["last_sent_msg_id"] = sent_msg_id
                    await _save_meta(db, meta_update)
                else:
                    # FIX 2026-08-07: previously a failed send was
                    # logged and then completely forgotten — nothing
                    # persisted, so there was no way to tell "never
                    # decided to message" apart from "decided to, but
                    # delivery failed" without reading raw logs. Now
                    # both the failure and its reason are saved to the
                    # same meta doc @system_status reads from, so it's
                    # visible from chat instead of a log file.
                    logger.error(f"Proactive send FAILED [{now_wat.strftime('%H:%M WAT')}]: {detail}")
                    await _save_meta(db, {
                        "last_send_ok": False,
                        "last_error": detail,
                        "last_attempted_message": message,
                        "last_checked_at": now_utc,
                    })
            else:
                await _save_meta(db, {"last_checked_at": now_utc})

        except asyncio.CancelledError:
            logger.info("Proactive agent stopped.")
            break
        except Exception as e:
            logger.error(f"Proactive agent error: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL_SECS)


# ── WhatsApp send (standalone, no circular import from api/whatsapp.py) ──

async def _send_whatsapp(chat_id: str, text: str) -> tuple:
    """Send a WhatsApp text message via GreenAPI directly.
    Returns (ok, detail, sent_msg_id).
    sent_msg_id is the GreenAPI idMessage of the outbound message —
    saved to proactive_meta so incoming replies (quotedMessage with
    stanzaId matching this) can be recognised as replies to THIS
    specific proactive message and given the right context.
    """
    import os, httpx as _httpx
    instance_id = os.getenv("GREEN_API_INSTANCE_ID", "")
    token = os.getenv("GREEN_API_TOKEN", "")
    if not instance_id or not token:
        logger.warning("GreenAPI credentials not set — can't send proactive message.")
        return False, "GreenAPI credentials not configured", None
    url = f"https://api.green-api.com/waInstance{instance_id}/sendMessage/{token}"
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json={"chatId": chat_id, "message": text})
            if resp.status_code != 200:
                detail = f"GreenAPI returned HTTP {resp.status_code}: {resp.text[:200]}"
                logger.error(f"Proactive send failed: {detail}")
                return False, detail, None
            sent_msg_id = resp.json().get("idMessage")
            return True, "sent", sent_msg_id
    except Exception as e:
        logger.error(f"Proactive send failed: {e}")
        return False, str(e), None


async def _send_whatsapp_voice(chat_id: str, text: str, user_id: str, session_id: str = "proactive") -> tuple:
    """
    Generate a voice note via edge-tts and deliver it via GreenAPI sendFileByUrl.
    Falls back to text if edge-tts isn't installed or TTS fails — the message
    still gets through, just as text instead of audio.

    Uses the same voice_reply skill infrastructure so the voice is consistent
    with what the owner hears when he asks for voice notes manually — no
    separate TTS path to maintain.
    """
    import os, httpx as _httpx, base64
    from datetime import datetime as _dt
    from io import BytesIO

    instance_id = os.getenv("GREEN_API_INSTANCE_ID", "")
    token = os.getenv("GREEN_API_TOKEN", "")
    backend_url = os.getenv("BACKEND_URL", os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")

    if not instance_id or not token:
        return False, "GreenAPI credentials not configured"

    try:
        import edge_tts
        voice = "en-NG-EzinneNeural"
        communicate = edge_tts.Communicate(text, voice)
        buf = BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        audio_bytes = buf.getvalue()
        if not audio_bytes:
            raise ValueError("TTS produced no audio")
    except Exception as e:
        logger.warning(f"Proactive voice TTS failed ({e}) — falling back to text")
        return await _send_whatsapp(chat_id, text)

    # Store in file_sessions so it's accessible via the serve endpoint
    filename = f"proactive_{_dt.utcnow().strftime('%H%M%S')}.mp3"
    try:
        from bootstrap import get
        db = get("db")
        encoded = base64.b64encode(audio_bytes).decode()
        await db["file_sessions"].update_one(
            {"user_id": user_id, "session_id": session_id, "filename": filename},
            {"$set": {
                "user_id": user_id, "session_id": session_id,
                "filename": filename, "content": encoded,
                "mime_type": "audio/mpeg", "encoding": "base64",
                "uploaded_at": _dt.utcnow(),
            }},
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"Proactive voice: failed to store audio ({e}) — falling back to text")
        return await _send_whatsapp(chat_id, text)

    audio_url = f"{backend_url}/api/v3/files/serve/{session_id}/{filename}"

    try:
        async with _httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.green-api.com/waInstance{instance_id}/sendFileByUrl/{token}",
                json={"chatId": chat_id, "urlFile": audio_url, "fileName": filename},
            )
            if resp.status_code != 200:
                logger.warning(f"Proactive voice sendFileByUrl failed ({resp.status_code}) — falling back to text")
                return await _send_whatsapp(chat_id, text)
            return True, "voice_sent"
    except Exception as e:
        logger.warning(f"Proactive voice delivery failed ({e}) — falling back to text")
        return await _send_whatsapp(chat_id, text)


def start_proactive_agent(db_getter) -> None:
    """Call from bootstrap to launch the proactive agent as a background task."""
    import asyncio
    loop = asyncio.get_event_loop()
    loop.create_task(proactive_loop(db_getter, _send_whatsapp))
    logger.info("🤖 Proactive agent task queued.")
