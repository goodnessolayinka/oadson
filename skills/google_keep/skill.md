# Google Keep

## What It Does
Creates, retrieves, and lists notes in Google Keep. Supports titled notes, checklists, and labeled notes. Reads existing notes by keyword or label.

## When to Use It
- User says "save a note to Keep", "check my Keep notes", "what did I note about X"
- User wants to create a quick checklist
- User retrieving an idea they saved earlier

## When NOT to Use It
- User wants local OADSON notes → use note_taker
- User wants task management → use google_tasks
- User hasn't logged in with Google OAuth

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `list` | all notes. |
| `search` | find notes by title match. |
| `create` | add a new note (requires title and text). |

## Output Format
```json
{
  "notes": [
    {
      "note_id": "12345",
      "title": "example-app Ideas",
      "content": "Add flashcard mode. Partner with tutorial centers...",
      "labels": ["product", "example-app"],
      "color": "yellow",
      "created_at": "2026-06-01T08:30:00Z",
      "is_checklist": false
    }
  ]
}
```

## Constraints
- Requires Google OAuth token
- Read and create only — cannot delete notes via OADSON
- Image and audio notes: metadata only, no content extraction
- Labels must already exist in Keep to filter by them
- Max note size: 20,000 characters

## Examples
- "Note to self: fix the session_id index bug"
- "Show all my notes tagged 'cybersecurity'"
- "Create a Keep checklist for tomorrow's tasks"
