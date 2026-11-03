# Google Calendar

## What It Does
Views and creates events in the user's Google Calendar. Retrieves upcoming events, checks free/busy time, and creates new meetings with attendees and reminders.

## When to Use It
- User asks "what's on my calendar", "do I have anything tomorrow"
- User wants to create a meeting or block time
- User checks availability for a specific day or week
- User asks about upcoming deadlines or events

## When NOT to Use It
- User hasn't logged in with Google OAuth — return auth error
- User wants task reminders → use google_tasks or reminder
- User wants to manage contacts — not supported

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `upcoming` | list next events. |
| `today` | list today's events. |
| `create` | add a new event (requires title, start, end). |

## Output Format
```json
{
  "events": [
    {
      "event_id": "abc123",
      "title": "example-app Code Review",
      "start": "2026-06-16T14:00:00+01:00",
      "end": "2026-06-16T15:00:00+01:00",
      "attendees": ["attendee@example.com"],
      "location": "Google Meet",
      "description": "..."
    }
  ]
}
```

## Constraints
- Requires valid Google OAuth token
- Times parsed from natural language — confirm timezone if ambiguous
- All times returned in user's configured timezone
- Cannot modify other users' calendars
- Recurring event creation requires specific syntax

## Examples
- "Block tomorrow morning for exam prep"
- "Show me all meetings this week"
- "Create a reminder for my university registration deadline"
