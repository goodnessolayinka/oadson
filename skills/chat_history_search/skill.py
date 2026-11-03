"""
skills/chat_history_search/skill.py — date-range chat history search +
summarization. Added 2026-08-27.

"I need it to search chat history using timestamps recorded in
MongoDB... what were we talking about last month... or last week
Saturday... filters it out by date and uses context compressor to
summarize it."

Deliberately does NOT parse "last month" / "last week Saturday" itself
— the model already gets told the actual current date/time on every
turn (see core/orchestrator.py's system prompt), so it can compute real
start_date/end_date bounds far more reliably than any hand-rolled NLP
date parser here could. This skill only ever takes already-resolved
ISO date strings — see manifest.json's parameter descriptions, which
tell the model exactly that.

Reuses memory/context_compressor.py's actual summarization primitive
(summarize_message_range) rather than reimplementing anything — that
module's whole job is "turn a list of messages into a good summary",
and a date-filtered set of messages is exactly that same job, just
sourced differently (across however many sessions the range spans,
instead of "everything older than one session's recent window").
"""

from datetime import datetime, timezone

# A full month of a heavily-used chat could realistically span several
# hundred messages across many sessions — feeding all of them into one
# summarization prompt risks an enormous, slow, expensive call for no
# real benefit (a summary doesn't get meaningfully better past a certain
# amount of source material; it just gets more expensive to produce).
# Capped and, when the cap is actually hit, HONESTLY disclosed via the
# truncated/message_count/summarized_count fields below rather than
# silently summarizing a subset and pretending it was the whole range.
MAX_MESSAGES_PER_SUMMARY = 250


def _parse_date(s: str, end_of_day: bool = False) -> datetime:
    """
    Accepts either a bare 'YYYY-MM-DD' or a full ISO datetime string.
    A bare date passed as end_date means "through the end of that
    calendar day", not midnight at its start — otherwise a range like
    2026-07-01..2026-07-31 would silently exclude essentially all of
    July 31st itself (everything after midnight that day).
    """
    s = s.strip()
    if len(s) == 10:
        dt = datetime.strptime(s, "%Y-%m-%d")
        if end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return dt.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    action = args.get("action") or "search_by_date"

    if action != "search_by_date":
        return {"status": "error", "reason": f"Unknown action '{action}'. Use: search_by_date"}

    user_id = payload.get("user_id")
    db = payload.get("db")
    if not user_id or db is None:
        return {"status": "error", "reason": "user_id/db not available"}

    start_raw, end_raw = args.get("start_date"), args.get("end_date")
    if not start_raw or not end_raw:
        return {
            "status": "error",
            "reason": "start_date and end_date are required (ISO format, e.g. '2026-07-01', or "
                      "a full ISO datetime). Compute these yourself from the current date you "
                      "already have — e.g. 'last month' means the 1st through the last day of "
                      "the previous calendar month; 'last week Saturday' means the most recent "
                      "Saturday before the current week.",
        }

    try:
        start_dt = _parse_date(start_raw, end_of_day=False)
        end_dt = _parse_date(end_raw, end_of_day=True)
    except ValueError as e:
        return {"status": "error", "reason": f"Could not parse dates: {e}"}

    if start_dt > end_dt:
        return {"status": "error", "reason": "start_date is after end_date"}

    # Messages are stored nested inside each session document's own
    # "messages" array (see core/response_pipeline.py's $push), so a
    # date range spanning multiple conversations needs to reach across
    # every session document, not just one — a plain find() can only
    # filter which DOCUMENTS match, not which array ELEMENTS inside a
    # matched document come back. Unwinding turns each message into its
    # own row so it can be matched/sorted on messages.time directly.
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$project": {"session_id": 1, "name": 1, "messages": 1}},
        {"$unwind": "$messages"},
        {"$match": {"messages.time": {"$gte": start_dt, "$lte": end_dt}}},
        {"$sort": {"messages.time": 1}},
        {"$project": {
            "_id": 0,
            "session_id": 1,
            "session_name": "$name",
            "role": "$messages.role",
            "content": "$messages.content",
            "time": "$messages.time",
        }},
    ]

    matched = [doc async for doc in db["ai_sessions"].aggregate(pipeline)]

    if not matched:
        return {
            "status": "success",
            "message_count": 0,
            "summary": None,
            "date_range": {"start": start_dt.date().isoformat(), "end": end_dt.date().isoformat()},
            "note": "No messages found in this date range.",
        }

    total_matched = len(matched)
    truncated = total_matched > MAX_MESSAGES_PER_SUMMARY
    # Most-recent-first relevance when a cap is needed — the tail end of
    # a date range is usually the more relevant part when the range is
    # too large to summarize whole.
    to_summarize = matched[-MAX_MESSAGES_PER_SUMMARY:] if truncated else matched

    session_names = sorted({m.get("session_name") or m["session_id"] for m in matched})

    from memory.context_compressor import summarize_message_range
    summary_text = await summarize_message_range(to_summarize)

    return {
        "status": "success",
        "message_count": total_matched,
        "summarized_count": len(to_summarize),
        "truncated": truncated,
        "sessions_touched": session_names,
        "date_range": {"start": start_dt.date().isoformat(), "end": end_dt.date().isoformat()},
        "summary": summary_text,
    }
