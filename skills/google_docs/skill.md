# Google Docs

## What It Does
Reads and extracts content from Google Docs. Returns document text, comments, and revision info. Useful for reviewing documents, summarizing content, or finding specific information inside a doc.

## When to Use It
- User says "read my doc", "what does the document say", "summarize this Google Doc"
- User shares a Google Docs link or document ID
- User wants to review comments on a shared doc

## When NOT to Use It
- User wants to *edit* the document — read-only access
- User wants to create a new doc — use file_generation
- User wants Drive file listing → use google_drive
- No doc ID or link provided

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `list` | recent documents. |
| `search` | find documents by name. |
| `read` | get a specific document's text content. |

## Output Format
```json
{
  "doc_id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
  "title": "example-app Backend Spec v4",
  "last_modified": "2026-06-14",
  "owner": "owner@example.com",
  "content": "...",
  "comments": [
    {"author": "collaborator", "text": "Update the auth flow section", "resolved": false}
  ]
}
```

## Constraints
- Read-only — cannot edit or write to Google Docs
- Requires Google OAuth token
- Formatting (bold, headers) stripped to plain text
- Large documents truncated at 8000 characters — ask for specific section

## Examples
- "What does the example-app v4 spec say about session expiry?"
- "Read and summarize my OGA origin story doc"
- "Who commented on this document?"
