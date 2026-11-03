# Terminal

## What It Does
Executes real shell commands inside a sandboxed directory at
`/tmp/oadson/{user_id}/`. Real stdout/stderr/return_code — never fabricated.
The exact command that ran is always in the result's `"command"` field so
you can verify it matches what you intended.

**This is the primary tool for all file editing, code generation, and
in-place repo work (reading/editing files inside an already-cloned
repo).** If a GitHub repo has been cloned via
`github action='clone_to_workspace'`, it lives at
`/tmp/oadson/{user_id}/repos/{owner}__{repo}/` (owner and repo are
ALWAYS lowercased when this folder is built — case doesn't matter on
GitHub itself, so match that here too: `repos/your-github-user__doxa-
bible-study-backend/`, not the exact casing you might remember typing
into `clone_to_workspace`. If a `cd` into this path fails with "No
such file or directory", check casing first — `ls -la repos/` shows
the real folder name — before assuming the repo wasn't actually
cloned) and every file in it is directly accessible here — no GitHub
API calls needed to read or edit.

**Getting a repo IN, and pushing changes back OUT, are both `@github`
jobs, never terminal git commands** — see "When NOT to Use It" below.
Terminal's job is everything that happens in between: reading, writing,
running, and editing the files once they're already there.

## CRITICAL: All paths must be relative to the sandbox home

The sandbox root is `/tmp/oadson/{user_id}/`. Every command runs from here.

**You cannot `cd` to an absolute path outside the sandbox.** These will be blocked:
```bash
cd /tmp/backend        # BLOCKED — /tmp is outside sandbox
cd /home/...           # BLOCKED
cd /opt/...            # BLOCKED
```

**Always use relative paths:**
```bash
# Work inside a repo already cloned via github action='clone_to_workspace'
# (never `git clone` directly in terminal — see "When NOT to Use It" below)
cd repos/backend && alembic upgrade head                     # ✅
```

pip install, curl, wget, npm install — all work. Render has real outbound internet.


- Reading, editing, creating, or deleting files in the workspace
- Running code: `python3 script.py`, `node index.js`
- Navigating cloned repos: `ls`, `find`, `grep`, `cat`, `head`, `diff`
- Editing files with `sed`, heredocs (`cat > file.py << 'EOF'`), or `echo`
- Running tests, linters, syntax checks
- Any multi-file operation — always faster here than N GitHub API calls

## When NOT to Use It — repo push/pull is ALWAYS a @github job

**Never run `git init`, `git clone`, `git remote add`, `git push`, `git
pull`, or `git config user.*` in terminal.** These are not just
discouraged — they're guaranteed to fail here, every time, because this
sandbox has no git author identity configured and no GitHub
authentication wired into terminal at all:

- `git init` / raw `git push` → "Author identity unknown ... Please tell
  me who you are" — there is no user.email/user.name to auto-detect, and
  terminal has no way to accept one from you or set it globally
- `git push` a second time → "fatal: remote origin already exists" —
  retrying with more git commands just compounds the broken state instead
  of fixing it
- Cloning to anywhere outside the sandbox root (e.g. a relative `../`
  path) → blocked outright, not a retryable error

If you hit ANY of the above, that is the signal you're in the wrong
tool — not a signal to try a different git flag or path. Stop and use
the matching `@github` action instead:

- You need to pull a repo down → `github action='clone_to_workspace'`
  (downloads it fully, no git identity or auth needed)
- You need to push changes back to GitHub → `github action='push_workspace'`
  after editing here (one commit via the Trees API — no git init, no
  remote, no auth handshake, none of the failure modes above are even
  possible through this path)
- You need to create a brand-new repo → `github action='create_repo'`,
  THEN `clone_to_workspace` on it if you need to add files locally first

## Sandbox Boundary
All commands run with `cwd=/tmp/oadson/{user_id}/`. You cannot `cd` outside
this directory or reference paths above it. Attempts are blocked with a
clear error — they do not silently succeed.

## Workflow for Repo Work
```
1. github action='clone_to_workspace' owner='x' repo='y'
   → downloads whole repo into /tmp/oadson/{user_id}/repos/x__y/ in ONE step

2. terminal: ls repos/x__y/
3. terminal: cat repos/x__y/core/orchestrator.py
4. terminal: cat > repos/x__y/skills/new_skill/skill.py << 'EOF'
   ... file content ...
   EOF
5. terminal: python3 -c "import ast; ast.parse(open('repos/x__y/skills/new_skill/skill.py').read()); print('OK')"

6. github action='push_workspace' owner='x' repo='y' commit_message='...'
   → ONE commit, ALL changed files, done
```
This is 3 GitHub steps + N terminal steps — not N*2 GitHub steps.

## Actions
The default action is `run` (plain shell command via `query`) — that
covers everything documented above. Three more specific actions exist
for cases where `run` would be awkward or unsafe:

| Action | Use for | Key params |
|---|---|---|
| `run` (default) | Any shell command | `query` |
| `run_code` | Run a source-code snippet in a specific language — writes it to a real file first, so you don't have to hand-escape it for the shell yourself | `language`, `code`, `filename` (optional), `stdin` (optional) |
| `download_packages` | Download pip/npm/yarn/cargo packages and zip them, without a full install into the working tree | package manager + package list params |
| `clone_install_zip` | Clone a GitHub repo, run its install step, and zip the result — all in one step. **Use this instead of chaining `cd && npm install && zip` manually across several `run` calls.** | repo URL + install command |

## How to Pass Commands
Gemini passes the command via `args.query`. Write real shell syntax:

```
# List cloned repo
ls repos/your-github-user__OADSON_V3/

# Read a file
cat repos/your-github-user__OADSON_V3/core/orchestrator.py

# Edit a file inline
sed -i 's/old_string/new_string/g' repos/your-github-user__OADSON_V3/main.py

# Write a whole new file
cat > repos/your-github-user__OADSON_V3/skills/new_skill/skill.py << 'PYEOF'
async def run(payload):
    return {"status": "success", "message": "hello"}
PYEOF

# Run Python
python3 repos/your-github-user__OADSON_V3/main.py

# Find files
find repos/your-github-user__OADSON_V3/skills -name "skill.py"

# Grep across all skills
grep -r "action_name" repos/your-github-user__OADSON_V3/skills/
```

## Output Format
```json
{
  "status": "success",
  "command": "ls repos/x__y/",
  "cwd": "/tmp/oadson/{user_id}",
  "stdout": "core/\nskills/\nmain.py\n",
  "stderr": null,
  "return_code": 0
}
```
On failure, `status` is `"error"` and `stderr`/`return_code` tell you exactly
what went wrong. Never interpret a non-zero return code as success.

## Constraints
- 30-second timeout per command — chain long operations with `&&` or split
  across steps
- **Real outbound internet access — `pip install`, `npm install`, `curl`,
  `wget` all genuinely work.** (An earlier version of this doc said the
  opposite — that was a misdiagnosis of an unrelated bug in how errors
  were being passed through, not an actual network restriction;
  confirmed fixed and verified against the sandbox's real behavior.)
- **Git commands are the ONE exception to the above — never run `git
  clone`, `git init`, `git push`, `git pull`, `git remote`, or any other
  git command in terminal, full stop, no exceptions.** This is not
  about network access (network access is real and unrelated to this
  rule) and it is not "not the first choice" — it is never the right
  choice. See "When NOT to Use It" above for exactly why (no git author
  identity configured here, no GitHub auth wired into terminal — every
  one of these WILL fail, it is not a preference being expressed here).
  `github action='clone_to_workspace'` and `github action='push_workspace'`
  are the only correct way to get a repo in or push changes out, always.
- No persistent processes — each command is a fresh subprocess
- Filesystem is **ephemeral**: survives the current Render dyno session,
  wiped on redeploy. For durable storage push to GitHub via
  `github action='push_workspace'` when done.
