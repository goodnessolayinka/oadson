# code_index

## What It Does
Indexes OADSON's own codebase for semantic search, or searches it. Use
this to find where something is implemented, how a feature works, or to
locate relevant code before making a change — instead of grepping
around blindly or guessing file paths.

## When to Use It
- "Where is the Gmail token refresh logic?" / "how does X work in this
  codebase?" → `search`
- After making code changes, to keep the index current → `index`
- Checking what's currently indexed before relying on search results →
  `status`

## Calling Convention
This is an `action`-based skill:

| Action | What it does | Key params |
|---|---|---|
| `index` | (Re)build the embedding index | `root` (dir to index — defaults to OADSON's own deployed codebase), `project` (defaults to `'oadson'`) |
| `search` | Semantically search indexed code | `query` (required, natural language — e.g. `'where is the Gmail token refresh logic'`), `limit` (default 5) |
| `status` | See what's currently indexed | `project` |

## Constraints
- `search` results are only as fresh as the last `index` run — if the
  code changed since then, re-index before trusting results for
  something recently modified.
- `project` scopes everything — use consistently across `index`/`search`
  calls for the same target, or results will silently miss what's
  indexed under a different project name.
