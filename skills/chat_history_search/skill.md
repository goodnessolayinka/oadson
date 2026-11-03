# Chat History Search

## What It Does
Searches across ALL of the user's past conversations (every session, not
just the current one) for messages within a specific date range, then
summarizes what was discussed using the same summarization mechanism
context_compressor already uses for long single sessions.

## When to Use It
- "What were we talking about last month?"
- "What did we discuss last Saturday?"
- "Summarize what we covered the week of [date]"
- Any request asking to recall/summarize a past time period, rather
  than something specific already visible in the current conversation

## When NOT to Use It
- The user is asking about something in the CURRENT conversation — you
  already have that in context, no need to search for it
- The user wants to search by TOPIC/keyword rather than a time period —
  this skill only filters by date, not content relevance

## How to Call It
You already know the actual current date/time from your own system
context on every turn — compute the real start_date/end_date yourself.
Never pass a relative phrase like "last month" literally as an
argument; this skill does no natural-language date parsing at all.

```
User: "What were we talking about last month?"
(if today is 2026-08-27)
→ call chat_history_search(action="search_by_date",
                            start_date="2026-07-01",
                            end_date="2026-07-31")

User: "What did we discuss last Saturday?"
(if today is 2026-08-27, a Thursday — the most recent Saturday was 2026-08-22)
→ call chat_history_search(action="search_by_date",
                            start_date="2026-08-22",
                            end_date="2026-08-22")
```

## Output Format
```json
{
  "status": "success",
  "message_count": 84,
  "summarized_count": 84,
  "truncated": false,
  "sessions_touched": ["Website redesign", "Trip planning"],
  "date_range": {"start": "2026-07-01", "end": "2026-07-31"},
  "summary": "..."
}
```
If nothing matched: `"message_count": 0, "summary": null`.

If the range was too large to summarize in full, `"truncated": true` and
`summarized_count` will be less than `message_count` — mention this to
the user rather than presenting a partial summary as if it were
complete (e.g. "covering the most recent 250 of 640 messages in that
range").

## Constraints
- Date-range only — no keyword/topic filtering within the range
- Very large ranges get capped to the most recent portion (see
  `truncated` in the output) rather than summarizing everything, to
  keep the summarization call itself reasonably sized
