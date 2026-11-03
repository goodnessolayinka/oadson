# whatsapp_reader

## What It Does
Read-only access to WhatsApp — lists chats/contacts and reads message
history, for summarizing or answering questions about conversations.
**Can NEVER send messages** — strictly read-only, no exceptions.

## Finding a specific person
Call `action='chats'` first to get their `chat_id` by name, then use
that `chat_id` with `history` or `summarize`. Don't request a bigger
`limit` on `chats` hoping to find someone further down the list — if
they're not in the default results, ask the user for their name/number
instead.

## Calling Convention
This is an `action`-based skill:

| Action | What it does | Key params |
|---|---|---|
| `chats` | List conversations, most recent first (capped at 100) | `limit` (default 50, hard cap 100) |
| `history` | Raw messages for ONE chat (capped at 50 messages, 1000 chars each) | `chat_id` or `phone` (required), `count` (default 20, hard cap 50) |
| `summarize` | One chat's messages + a summarization instruction (same caps as `history`) | `chat_id` or `phone`, `count` |
| `recent_summary` | The N most recent chats across ALL contacts with their recent messages, for a "summarize my last few chats" style request — no `chat_id` needed | `limit_chats` (default 5, hard cap 10), `count` (default 10, hard cap 20) |

## Constraints
- **Read-only. Never sends, replies, or modifies anything on WhatsApp.**
- All message/chat counts are hard-capped (see table) — raw WhatsApp
  history can be hundreds of thousands of tokens, far more than any
  model can accept in one request. Don't try to work around the caps by
  calling repeatedly to accumulate more; if the capped result isn't
  enough, narrow the request (a specific chat, a specific person)
  instead of trying to widen it.
