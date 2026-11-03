# Reminder

## What It Does
Creates and manages time-based reminders stored in OADSON's database. Alerts the user at the specified time via the active surface. Supports one-time and recurring reminders.

## When to Use It
- User says "remind me", "set a reminder", "alert me at X"
- User wants a repeating notification (daily, weekly)
- User checking what reminders are active

## When NOT to Use It
- User wants a Google Calendar event → use google_calendar
- User wants a task with a due date → use google_tasks
- User wants a one-line note without a time → use note_taker

## Calling Convention
This is an `action`-based skill:

| Action | What it does |
|---|---|
| `set` | Create a new reminder |
| `check` | Check what's currently due |

## Output Format
```json
{
  "reminder_id": "rem_abc123",
  "title": "Push to GitHub",
  "scheduled_time": "2026-06-15T21:00:00+01:00",
  "recurrence": null,
  "status": "active",
  "created_at": "2026-06-14T18:30:00Z"
}
```

## Constraints
- Reminder delivery depends on user being active on OADSON at trigger time
- No push notification system yet (V3 feature)
- Recurring options: daily, weekly, weekdays
- Max 50 active reminders per user
- Timezone inferred from user's last known location or set timezone

## Examples
- "Daily reminder at 10pm: review what I built today"
- "Remind me 30 minutes before my next exam"
- "What reminders do I have set for this week?"
