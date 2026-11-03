# Google Tasks

## What It Does
Creates, lists, and updates tasks in Google Tasks. Organizes tasks by list, sets due dates, and marks items complete. Syncs with Google Calendar's task sidebar.

## When to Use It
- User says "add to my tasks", "what tasks do I have", "mark X as done"
- User wants to track to-dos with due dates
- User managing multiple task lists (work, personal, project)

## When NOT to Use It
- User wants quick local notes → use note_taker
- User wants calendar events → use google_calendar
- User hasn't logged in with Google OAuth

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `list` | pending tasks in the default list. |
| `task_lists` | all of the user's task lists. |
| `create` | add a new task (requires title; list_id optional, defaults to '@default'). |
| `complete` | mark a task done (requires task_id; list_id optional, defaults to '@default'). |

## Output Format
```json
{
  "tasks": [
    {
      "task_id": "abc123",
      "title": "Push OADSON V2 to Railway",
      "status": "needsAction",
      "due_date": "2026-06-16",
      "list_name": "Development",
      "created_at": "2026-06-14T10:00:00Z"
    }
  ]
}
```

## Constraints
- Requires Google OAuth token
- Status: needsAction | completed (binary — no in-progress state)
- Due dates without times default to midnight in user's timezone
- Cannot create subtasks via OADSON
- Up to 100 tasks returned per list

## Examples
- "Add 'fix skill detection bug' to my dev tasks"
- "What am I behind on this week?"
- "Complete my morning routine checklist"
- "Create a task list for example-app launch prep"
