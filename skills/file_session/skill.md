# File Session Skill

Session-scoped file workspace. Upload files, OADSON reads and rewrites them, download results.

## What it does
- Stores files in MongoDB scoped to the current session
- OADSON can read, edit, and rewrite any file in the session
- Works with text files, code, configs, and extracted zip contents
- Zero filesystem — everything in MongoDB, survives within session

## Trigger phrases
- "list my files", "what files do I have"
- "read file.py", "show me the contents of main.py"
- "write this to skill.py", "save this as config.json"
- "delete file.py from session"
- "clear my session files", "drop all files"
- "summarize my files", "what files can you see"

## Actions

### list
List all files in current session.

### read
Read a specific file.
```
filename: exact filename (required)
```

### write
Write or overwrite a file in session.
```
filename: filename to write (required)
content: file content (required)
```

### delete
Delete a specific file from session.
```
filename: exact filename (required)
```

### drop
Clear all files in current session.

### summary
Get a preview summary of all files OADSON can see in session.

## Workflow example
1. Upload file via `/api/v3/files/upload`
2. Tell OADSON: "read skill.py from my session"
3. OADSON reads it, suggests fixes
4. OADSON writes the fixed version back: "write the fixed version to skill.py"
5. Download via `/api/v3/files` or read directly

## Notes
- Files are scoped to session_id — different sessions have different files
- Max file size: 5MB per file
- Max files per session: 50
- Content truncated at 8000 chars when reading large files
