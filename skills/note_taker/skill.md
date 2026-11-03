# Note Taker

## What It Does
Creates, retrieves, and searches local OADSON notes. Stores ideas, quick thoughts, and text snippets tied to the user's account. Notes persist in MongoDB across sessions.

## When to Use It
- User says "save this note", "note that", "what notes do I have on X"
- User wants a quick capture without opening another app
- User retrieving a previously saved thought or idea

## When NOT to Use It
- User wants Google Keep integration → use google_keep
- User wants to save a file → use file_generation
- User wants to manage tasks → use google_tasks or reminder

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `save` | create a note. |
| `get` | retrieve one by ID. |
| `list` | all your notes. |
| `search` | find by title/content/tag. |
| `update` | edit a note. |
| `delete` | remove a note (needs confirmation). |
| `add_tag` | tag an existing note. |

## Output Format
```json
{
  "notes": [
    {
      "note_id": "note_abc123",
      "title": "Auth Flow Idea",
      "content": "Consider moving token refresh to middleware...",
      "category": "dev",
      "tags": ["auth", "fastapi"],
      "created_at": "2026-06-13T22:30:00Z"
    }
  ],
  "total": 7
}
```

## Constraints
- Text-based only — no image or file notes
- Stored in OADSON's MongoDB (not Google Keep)
- Searchable by content, title, tags, and category
- No encryption — treat as internal notes, not secrets
- Max note size: 10,000 characters

## Examples
- "Note: product launch October 2026, first-year students"
- "Save this brainstorm — CBT Hub white-label strategy"
- "What notes do I have tagged 'security'?"
