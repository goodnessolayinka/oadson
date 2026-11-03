# Workspace Skill

Index GitHub repositories into MongoDB and work with them without cloning.

## What it does
- Indexes any accessible GitHub repo into MongoDB via the GitHub API
- Lets OADSON read, search, and reference any file in the indexed repo
- Stores suggested patches/edits for files
- Zero filesystem dependency — everything lives in MongoDB

## Trigger phrases
- "index my repo", "index your-github-user/OADSON_V3"
- "read file main.py from OADSON_V3"
- "search for bootstrap in my repo"
- "show my indexed repos", "list workspace repos"
- "suggest a patch for", "store this fix for"
- "show pending patches", "apply patch"
- "remove repo from workspace", "drop workspace"

## Actions

### index
Index a GitHub repo into MongoDB.
```
repo: owner/repo_name (required)
branch: branch name (optional, defaults to repo default)
max_files: max files to index (optional, default 200)
```

### repos
List all indexed repos in workspace.

### list
List all files in an indexed repo.
```
repo: owner/repo_name (required)
filter: path filter string (optional)
```

### read
Read a specific file from indexed repo.
```
repo: owner/repo_name (required)
path: file path e.g. main.py or api/chat.py (required)
```

### search
Search file contents across indexed repos.
```
query: search string (required)
repo: narrow to specific repo (optional)
```

### patch
Store a suggested edit for a file.
```
repo: owner/repo_name (required)
path: file path (required)
content: new file content (required)
description: what this patch does (optional)
```

### patches
List pending patches.
```
repo: owner/repo_name (optional, filters by repo)
```

### apply
Apply a patch to the workspace copy.
```
patch_id: patch ID from patches list (required)
```

### drop
Remove an indexed repo from workspace.
```
repo: owner/repo_name (required)
```

## Notes
- Requires GITHUB_TOKEN in env for private repos
- Binary files (images, zips, etc.) are skipped automatically
- File content is truncated at 8000 chars when reading
- Patches are stored in MongoDB — apply them manually to GitHub via git push
