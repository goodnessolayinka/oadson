# GitHub

## What It Does
Interacts with GitHub repositories via API. Covers reading repo metadata,
listing/reading files, committing files, creating repos — and the two
workspace actions that replace all per-file API loops:

| Action | When to use |
|---|---|
| `list_repos` | Show a user's repositories |
| `repo_info` | Details about one repo |
| `commits` | Recent commits |
| `issues` | Open/closed issues |
| `search` | Search public repos |
| `list_files` | Directory listing (quick one-off look only) |
| `read_file` | Read ONE file (quick one-off look only) |
| `write_file` | Write ONE file, ONE commit (single-file edits only) |
| `create_repo` | Create a new repository |
| **`clone_to_workspace`** | **Download whole repo → terminal sandbox. USE THIS for any multi-file work.** |
| **`push_workspace`** | **Push ALL sandbox changes in ONE commit. Always after clone+terminal edits.** |

## Action Reference — required params for every action

| Action | Required | Optional |
|---|---|---|
| `list_repos` | `username` | — |
| `repo_info` | `owner`, `repo` | — |
| `commits` | `owner`, `repo` | — |
| `issues` | `owner`, `repo` | `state` (`'open'`/`'closed'`/`'all'` — defaults to `'open'`) |
| `search` | `query` | — (searches public repos across all of GitHub, not just the token owner's) |
| `list_files` | `owner`, `repo` | `path` (subdirectory — omit for repo root), `ref` (branch/tag/SHA — omit for default branch) |
| `read_file` | `owner`, `repo`, `path` | `ref` |
| `write_file` | `owner`, `repo`, `path`, `content` | `commit_message`, `branch` — single-file only, see below |
| `create_repo` | `name` | `description`, `private` (bool, defaults `False` — see the note on "always private" below), `org` |
| `clone_to_workspace` | `owner`, `repo` | `ref` |
| `push_workspace` | `owner`, `repo`, `commit_message` | `branch`, `paths` (limit to specific files/dirs — omit to push everything changed) |

`list_repos`/`repo_info`/`commits`/`issues`/`search` are all read-only,
lightweight, single-API-call lookups — safe to reach for anytime you
need repo metadata or history without touching any files.

## The Right Pattern for Repo Work

**For any task involving reading multiple files, editing code, or pushing changes:**

```
Step 1:  github  action='clone_to_workspace'  owner='x'  repo='y'
         → Downloads whole repo into /tmp/oadson/{user_id}/repos/x__y/
         → Returns file tree so you know the structure immediately

Step 2+: terminal  query='cat repos/x__y/core/orchestrator.py'
         terminal  query='grep -r "def run" repos/x__y/skills/'
         terminal  query='sed -i "s/old/new/g" repos/x__y/skills/github/skill.py'
         terminal  query='cat > repos/x__y/skills/new/skill.py << EOF ...'
         terminal  query='python3 -c "import ast; ast.parse(...)"'
         → Unlimited terminal steps, no GitHub quota consumed

Final:   github  action='push_workspace'  owner='x'  repo='y'
                 commit_message='fix: ...'
         → ONE commit, ALL changed files, atomic
```

This is **3 GitHub steps** regardless of how many files you touch.

## What NOT to Do

**Never loop `read_file` to explore a repo** — that burns one step and one
GitHub API call per file. `clone_to_workspace` gets the whole repo in one
step; `@terminal cat` reads any file for free after that.

**Never loop `write_file` to push multiple files** — that creates one commit
per file and burns the step budget. `push_workspace` handles any number of
files in one commit via the Git Trees API.

**Only use `read_file`/`write_file` when:**
- You need exactly one file and the repo is NOT cloned
- The task is genuinely one-file and one-shot

## Action Reference

### clone_to_workspace
Downloads the repo as a GitHub zipball, strips the generated prefix
directory, extracts into `/tmp/oadson/{user_id}/repos/{owner}__{repo}/`.
Returns the full file tree. If already cloned this session, wipes and
re-clones for a clean state.

**IMPORTANT: zipballs do not include `.git/`.** After cloning:
- `git log`, `git status`, `git diff` → will FAIL (no git history)
- For commit history → use `github action='commits'` instead
- For file edits → use `@website_developer action='edit'` (HTML/CSS/JS),
  `@backend_developer action='edit'` (FastAPI/Python), or `@terminal`
  for anything neither covers — then `push_workspace`
- Never run git commands on a cloned workspace

Required: `owner`, `repo`
Optional: `ref` (branch/tag/SHA — defaults to default branch)

### push_workspace
Reads files from the sandbox and pushes all of them in ONE commit using
GitHub's Git Trees API (get HEAD → create blobs → create tree → create
commit → update ref). Single commit SHA returned.

Required: `owner`, `repo`, `commit_message`
Optional: `branch` (defaults to default branch), `paths` (list of specific
relative paths — omit to push everything non-ignored in the workspace)

Only call this AFTER `clone_to_workspace` has been called for this repo.
Only report success if returned `status` is `"success"` with a real
`commit_sha` — never describe a push as done without that.

### write_file
Single-file commit via Contents API. Fetches current blob SHA automatically
so it handles both create and update. Use for one-off single-file changes
when you don't need the full clone.

**Text files only — cannot push binary content (images, PDFs, zips) at
all.** `content` gets UTF-8 encoded internally; feeding it raw binary
bytes will corrupt them or fail outright. For an image or any other
binary file, use `push_workspace` instead — it already handles binary
correctly (falls back to a proper base64 blob for anything that isn't
valid UTF-8 text), as long as the file is actually sitting in the
sandbox first (an uploaded image is saved to the sandbox workspace
automatically — check the turn's objective for the saved path).

### create_repo
Creates a new GitHub repo under the token owner's account (or an org if
`org` is specified). `auto_init=True` by default so the default branch
exists immediately and `write_file`/`push_workspace` can write into it.

**If the user says "always private" / "make it private" at any point in
the conversation, pass `private=True` on every `create_repo` call for the
rest of the session** — this defaults to `False` and is NOT remembered
automatically between separate `create_repo` calls; each call needs its
own `private=True` if that's what's wanted. Don't wait for the user to
notice a repo came out public and ask you to fix it — get it right on
creation.

**If `create_repo` fails with "name already exists"**, that means a repo
with this name is already there — it does NOT mean creation is broken
and retrying with the same name will keep failing identically. Do one of,
in order of likely intent:
1. If the existing repo is what you actually want to push into, skip
   `create_repo` entirely and go straight to `clone_to_workspace` +
   `push_workspace` on it — no need to create what already exists.
2. Otherwise ask the user whether to reuse the existing repo, use a
   different name, or delete/recreate it (deletion isn't something this
   skill does — that has to happen on github.com or be confirmed
   explicitly before attempting via `repo_info`/manual API steps).
Never just retry `create_repo` with the identical name expecting a
different result.

## Output Examples

clone_to_workspace:
```json
{
  "status": "success",
  "repo": "your-github-user/OADSON_V3",
  "workspace": "/tmp/oadson/abc123/repos/your-github-user__OADSON_V3",
  "files_extracted": 87,
  "tree": [{"path": "main.py", "type": "file", "size": 4210}, ...]
}
```

push_workspace:
```json
{
  "status": "success",
  "repo": "your-github-user/OADSON_V3",
  "branch": "main",
  "commit_sha": "a4f3b9e",
  "commit_url": "https://github.com/your-github-user/OADSON_V3/commit/a4f3b9e",
  "files_pushed": 12,
  "pushed": ["skills/github/skill.py", "skills/github/skill.md", ...]
}
```

Any action returns `{"status": "error", "reason": "..."}` on failure —
report it as a failure, never as a success with caveats.

## Constraints
- Requires `GITHUB_TOKEN` env var with `repo` scope for private repos and writes
- Public repos are readable without a token
- `push_workspace` requires the repo to have at least one commit on the target
  branch (satisfied automatically by `create_repo` with `auto_init=True`)
- Files >1MB in the workspace are skipped by `push_workspace` (listed in
  `"skipped"` in the result)
- `write_file` caps at 300KB per file — for larger files use `push_workspace`
- `clone_to_workspace` and `push_workspace` require `user_id` to be available
  (always true when called from the orchestrator — only fails in testing)
