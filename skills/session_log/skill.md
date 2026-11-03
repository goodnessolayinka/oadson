# Session Log

## What It Does
Retrieves conversation history from previous OADSON sessions. Returns message logs, session metadata, skill usage history, and timestamps. Useful for reviewing past conversations or continuing where you left off.

## When to Use It
- User says "show my chat history", "what did I discuss last session", "find the conversation about X"
- User wants to review a past decision or code snippet from a previous chat
- User wants to know which skills were triggered in past sessions

## When NOT to Use It
- Current session is still active — context is already in memory
- User wants memory vault entries → use memory_manager
- User wants usage metrics → use usage_analytics

## Calling Convention
This is an `action`-based skill:

| Action | What it does |
|---|---|
| `list` | Recent sessions |
| `search` | Keyword search across past session messages |
| `get` | Retrieve full messages for one specific `session_id` |

## Output Format
```json
{
  "sessions": [
    {
      "session_id": "oga_abc123",
      "title": "Backend Session 3 Fixes",
      "created_at": "2026-06-14T22:00:00Z",
      "message_count": 47,
      "skills_used": ["github", "bug_analyzer", "code_explainer"],
      "last_message_preview": "The Google token architecture is now fixed..."
    }
  ],
  "total": 12
}
```

## Constraints
- Sessions stored in MongoDB — persistent across devices
- Very old sessions may be archived (30+ days)
- Full message content requires specifying session_id
- Sessions with no messages not shown
- Message content searched by keyword — not semantic

## Examples
- "Retrieve the session where we fixed the skills_manager path bug"
- "What was the last thing we discussed about example-app?"
- "Show all sessions from this week"
