# Google Drive

## What It Does
Browses, searches, and retrieves file metadata from Google Drive. Lists files and folders, shows sharing permissions, and fetches basic content from supported file types.

## When to Use It
- User says "find my file in Drive", "list files in my folder", "what's in Drive"
- User wants to check who has access to a file
- User searching for a document, spreadsheet, or presentation by name
- User wants recent uploads or shared files

## When NOT to Use It
- User wants to read a Google Doc → use google_docs
- User wants to upload files → use upload router (POST /api/v2/upload)
- User hasn't logged in with Google OAuth

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `list` | all files. |
| `recent` | recently modified. |
| `search` | find by query. |
| `info` | metadata only. |
| `read` | read full TEXT CONTENT of a file by file_id (Google Docs/Sheets/text/PDF). |
| `read_by_name` | search then read in one step — pass a name. |
| `read_url` | fetch and read ANY URL or Google Drive share link. |
| `list_folders` | all folders. |
| `list_folder` | contents of one folder (needs folder_id or name). |

## Output Format
```json
{
  "files": [
    {
      "file_id": "1BxiMVs...",
      "name": "example-app_Backend_Spec_v4.pdf",
      "type": "application/pdf",
      "size_kb": 245,
      "created_at": "2026-05-20",
      "modified_at": "2026-06-12",
      "shared_with": ["attendee@example.com"],
      "url": "https://drive.google.com/file/d/..."
    }
  ]
}
```

## Constraints
- Requires Google OAuth token
- File content available for Docs/Sheets/Slides, and PDF/text/csv/json
  (downloaded and read directly). Other binary types: metadata only.
- Folder traversal limited to 2 levels deep per query
- Shared drives (Team Drives) may require additional permissions

## Examples
- "Find the ClassFundz API doc in my Drive"
- "Show everything shared with my collaborator"
- "List my most recently modified files"
