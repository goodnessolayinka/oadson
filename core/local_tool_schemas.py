"""
core/local_tool_schemas.py — Gemini function-declaration schemas for the
terminal surface's LOCAL tools (executed on the caller's own machine via
oadson_pkg's executor.py, never on this server).

ADDED 2026-08-23 — replaces the local_execution=True prompt-parsed-JSON
approach with real native Gemini function-calling for the terminal
surface. Previously local_execution disabled tools_for_call entirely
(see core/tool_loop.py's gating comment history) because native
function-calling assumed whoever received a functionCall could execute
it immediately, in-process — true for server skills, never true for
local tools, which can only run on the caller's device. That's not
actually a hard API limitation: Gemini's function-calling contract only
requires that a functionCall eventually gets a matching functionResponse
somewhere in the conversation history — it doesn't require synchronous,
same-process execution. So the fix isn't "give up on native calling for
terminal," it's "let the loop hand a local functionCall back to the
client instead of trying to execute it here" (see tool_loop.py's
LOCAL_TOOL_NAMES check) while still executing server skills immediately,
in-process, exactly like web/app already does.

These schemas exist ONLY so Gemini can offer these tools as genuine,
API-enforced options (exact names, schema-validated arguments) — this
server never calls the functions these describe. Argument names verified
directly against oadson_pkg/oadson/executor.py's actual args.get(...)
calls, not guessed — a schema drifting from what the client actually
reads would silently break tool calls with no visible error on either
side.

LOCAL_TOOL_NAMES is the authoritative set tool_loop.py checks against to
decide "hand this back to the client" vs "execute it here" — keep it in
sync with this dict's keys; a name present in one but not the other is
a bug, not a variant.
"""

LOCAL_TOOL_DECLARATIONS = [
    {
        "name": "run_bash",
        "description": "Run a shell command on the user's own machine and return its output.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file's contents from the user's own machine.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file, relative to the current working directory."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Overwrite an existing file's contents on the user's own machine.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to overwrite."},
                "content": {"type": "string", "description": "The new full contents of the file."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "create_file",
        "description": "Create a new file with the given contents on the user's own machine.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path for the new file."},
                "content": {"type": "string", "description": "Contents to write to the new file."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "delete_file",
        "description": "Delete a file on the user's own machine.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to delete."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": "List the contents of a directory on the user's own machine.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to list. Defaults to the current directory."},
            },
        },
    },
    {
        "name": "search_files",
        "description": "Search for a text pattern across files on the user's own machine.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text or regex pattern to search for."},
                "path": {"type": "string", "description": "Directory to search within. Defaults to the current directory."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "git_status",
        "description": "Show the current git status and branch on the user's own machine.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "git_diff",
        "description": "Show the git diff for a path (or the whole repo) on the user's own machine.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to diff. Omit to diff the whole repo."},
            },
        },
    },
    {
        "name": "http_request",
        "description": "Make an HTTP request from the user's own machine (e.g. to test a local dev server).",
        "parameters": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "description": "HTTP method. Defaults to GET."},
                "url": {"type": "string", "description": "URL to request."},
                "headers": {"type": "object", "description": "Optional request headers."},
                "body": {"type": "string", "description": "Optional request body."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "syntax_check",
        "description": "Check a file on the user's own machine for syntax errors.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to check."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "test_endpoint",
        "description": "Hit an HTTP endpoint from the user's own machine and check the response, optionally against an expected status code.",
        "parameters": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "description": "HTTP method. Defaults to GET."},
                "url": {"type": "string", "description": "URL to test."},
                "headers": {"type": "object", "description": "Optional request headers."},
                "body": {"type": "string", "description": "Optional request body."},
                "expected_status": {"type": "integer", "description": "Optional expected HTTP status code to check against."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "run_tests",
        "description": "Run a test command on the user's own machine and return the results.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Test command to run, e.g. 'pytest' or 'npm test'."},
                "path": {"type": "string", "description": "Optional path to scope the test run to."},
            },
        },
    },
    {
        # ADDED — closes the "no git_commit/git_push" gap: the agent could
        # inspect git_status/git_diff but never close a unit of work.
        "name": "git_commit",
        "description": "Stage files and create a git commit on the user's own machine. Always confirms with the user first and shows the diff being committed.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message."},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths to stage. Use ['.'] to stage all tracked changes. Untracked files must be named explicitly.",
                },
                "allow_empty": {"type": "boolean", "description": "Allow a commit with no staged changes. Defaults to false."},
            },
            "required": ["message"],
        },
    },
    {
        "name": "git_push",
        "description": "Push the current branch to its remote on the user's own machine. Always confirms with the user first; never force-pushes without explicit force=true, and even then uses --force-with-lease for safety.",
        "parameters": {
            "type": "object",
            "properties": {
                "remote": {"type": "string", "description": "Remote name. Defaults to 'origin'."},
                "branch": {"type": "string", "description": "Branch name. Defaults to the current branch."},
                "force": {"type": "boolean", "description": "Force push (uses --force-with-lease). Defaults to false."},
                "set_upstream": {"type": "boolean", "description": "Set upstream tracking on first push of a new branch. Defaults to false."},
            },
        },
    },
    {
        "name": "git_log",
        "description": "Show recent commit history on the user's own machine. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "Number of commits to show. Defaults to 10, max 50."},
                "path": {"type": "string", "description": "Optional path to scope the log to."},
                "oneline": {"type": "boolean", "description": "Compact one-line format. Defaults to true."},
            },
        },
    },
    {
        # ADDED — closes the "no project-wide awareness" gap: previously the
        # only way to see the shape of a project was a chain of list_dir
        # calls, which burns MAX_ITERATIONS steps and still misses depth.
        "name": "project_tree",
        "description": "Return a compact directory tree of the project on the user's own machine, skipping common noise directories (.git, node_modules, __pycache__, etc). Read-only. Use this FIRST when asked to understand, review, or work across an unfamiliar project — it costs one call instead of a chain of list_dir calls.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Root path to start from. Defaults to the current directory."},
                "depth": {"type": "integer", "description": "Max depth to descend. Defaults to 3, max 5."},
                "exclude": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional directory/file names or glob patterns to skip, on top of the built-in defaults.",
                },
                "max_files": {"type": "integer", "description": "Max total files to include before truncating. Defaults to 200, max 500."},
            },
        },
    },
    {
        # ADDED — lets the terminal offload slow/network-heavy commands
        # (npm install, pip install, cloning something large, a build
        # that pulls dependencies) to the backend's own fast
        # infrastructure instead of the user's mobile connection. The
        # command actually runs server-side in a persistent per-user
        # workspace (api/remote_workspace.py) — this tool's job on the
        # terminal side is just to make that HTTP call and report the
        # result back, the same relationship http_request already has
        # to its own remote call.
        "name": "remote_bash",
        "description": "Run a shell command on the BACKEND's own fast infrastructure instead of the user's local machine, inside a persistent named workspace that survives across multiple calls in the same session (install once, build many times). Use this for anything network- or bandwidth-heavy -- npm install, pip install, cloning a large repository, a build step that downloads dependencies -- especially when the user has mentioned slow or unreliable local network. Do NOT use this for filesystem-only operations on the user's own project files (use run_bash for those) -- remote_bash operates in an isolated server-side directory, not the user's local project. The workspace has NO persistent disk -- it survives between messages but is wiped on a server restart/deploy, so always use remote_download to pull finished results down; never treat it as permanent storage.",
        "parameters": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "A name for the persistent remote workspace, e.g. 'mavixal-parent-portal'. Reuse the SAME name across calls for the same project to reuse its installed dependencies instead of starting fresh each time."},
                "command": {"type": "string", "description": "The shell command to run on the server."},
                "cwd": {"type": "string", "description": "Relative path inside the workspace to run the command from (e.g. a repo name after cloning). Defaults to the workspace root."},
            },
            "required": ["workspace_id", "command"],
        },
    },
    {
        "name": "remote_download",
        "description": "Download the result of remote_bash work as a zip and extract it directly into the user's local project -- makes remote work feel like it happened locally. Call this once remote_bash work is finished (e.g. after a build completes) to bring the output (or the whole workspace) down to the user's own machine. Always do this before ending a task that used remote_bash -- the remote workspace is not permanent storage.",
        "parameters": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Which remote workspace to download from -- must match a workspace_id used with remote_bash."},
                "remote_path": {"type": "string", "description": "Path inside the workspace to zip, e.g. 'dist' after a build, to avoid pulling all of node_modules down too. Defaults to the entire workspace."},
                "local_path": {"type": "string", "description": "Where to extract locally, relative to the current directory. Defaults to a NEW subfolder named after the workspace_id (e.g. './my-workspace/'), not the current directory itself -- pass '.' explicitly only if the user wants files extracted loose into their current directory."},
            },
            "required": ["workspace_id"],
        },
    },
    {
        # ADDED — was built earlier (executor.py's tool_write_files) but
        # never actually declared here, meaning Gemini could never call
        # it at all despite the local implementation existing. Found via
        # a name-set consistency check between executor.py's TOOLS dict
        # and this file while adding remote_bash/remote_download.
        "name": "write_files",
        "description": "Write multiple files in ONE atomic, single-confirm operation on the user's own machine. Use this instead of calling write_file multiple times whenever a change spans more than one file (e.g. renaming something across several call sites, adding a field to a model plus its serializer plus its test) — it costs one tool call and one confirmation instead of N separate ones, and applies all-or-nothing so a partial multi-file change can never be left half-applied.",
        "parameters": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                    "description": "List of files to write, each with a path and its full new content.",
                },
                "message": {"type": "string", "description": "Optional short description of what this batch of changes does, shown above the diffs."},
            },
            "required": ["files"],
        },
    },
    {
        # ADDED — same gap as write_files: built (executor.py's
        # tool_task_list) but never declared here. session_id is
        # deliberately NOT listed as a required parameter for the model
        # to fill in -- agent.py auto-injects the current session's ID
        # before dispatch, since the model has no reliable way to know
        # it and shouldn't need to track it.
        "name": "task_list",
        "description": "Create, update, or view a persistent, user-visible checklist for multi-step work. Use this for any task with 3+ distinct steps so the user can see progress unfold instead of just waiting for a final answer -- create the list up front with all known steps, then update each one's status as you complete it.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "view"],
                    "description": "create: start a new checklist (replaces any existing one for this session). update: change one task's status. view: see the current list.",
                },
                "tasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For action=create: the list of task descriptions, in order.",
                },
                "index": {"type": "integer", "description": "For action=update: 0-based index of the task to change."},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "blocked"],
                    "description": "For action=update: the task's new status.",
                },
                "note": {"type": "string", "description": "For action=update, optional: why a task is blocked, or any short note."},
            },
            "required": ["action"],
        },
    },
    {
        # ADDED 2026-08-29 — oadson_pkg client v1.0.7 shipped a real
        # code_index tool in executor.py (ast-based for Python, regex
        # heuristic for JS/TS/JSX/TSX) but it was never declared here,
        # so Gemini had no way to call it — confirmed live: asked to
        # "find symbol X" / "build a code index" and it fell back to
        # grepping or writing throwaway scripts instead, since
        # code_index didn't exist as a callable tool from its
        # perspective. Same class of gap as write_files/task_list above
        # — a real client-side tool, built and working, just missing
        # from this file. Argument names (action/root/symbol) and
        # result shapes below verified against the handoff spec that
        # accompanied this addition, matching this file's existing
        # policy of not guessing at what the client actually reads.
        #
        # RENAMED 2026-08-29, same day — declared here as "code_index"
        # verbatim, which collided head-on with an EXISTING server-side
        # skill of the exact same name (an embeddings-based semantic
        # search skill over OADSON's own backend codebase — a
        # completely different tool that happens to share a name).
        # Whatever merges LOCAL_TOOL_DECLARATIONS with the skill
        # registry's own declarations into the final tools=[...] payload
        # sent to Gemini never checked for cross-source name collisions,
        # so both got declared simultaneously — Gemini rejects two
        # function declarations sharing one name outright, which took
        # down EVERY model in the fallback chain identically (every
        # chat/stream request 400'd until this was caught) since the
        # collision happens before any provider-specific routing. Client
        # v1.0.9 renamed its dispatch key to "local_code_index" to
        # disambiguate; this entry's "name" must match that exactly, or
        # tool_loop.py's LOCAL_TOOL_NAMES-membership routing breaks the
        # handoff to the client even though nothing here would 400.
        "name": "local_code_index",
        "description": "Build or query a local symbol index (functions/classes with file+line location) for the current project on the user's own machine. Use action='build' once per session before using action='find', or after a large refactor. Use action='find' to jump to a symbol's definition instead of grepping for it -- grep also matches comments, strings, and call sites; this only returns actual definitions. Supports Python (exact, via ast) and JS/TS/JSX/TSX (heuristic, via regex on top-level declarations). Not to be confused with the separate 'code_index' skill, which does embeddings-based semantic search over OADSON's own backend codebase -- this tool operates on the END USER's local project instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["build", "find", "stats"],
                    "description": "build: scan the project and save an index. find: look up a symbol by name/substring. stats: summary of what's indexed.",
                },
                "root": {
                    "type": "string",
                    "description": "Root directory to index. Only used with action='build'. Defaults to '.'.",
                },
                "symbol": {
                    "type": "string",
                    "description": "Symbol name (or substring) to look up. Required with action='find'.",
                },
            },
            "required": ["action"],
        },
    },
]

LOCAL_TOOL_NAMES = frozenset(d["name"] for d in LOCAL_TOOL_DECLARATIONS)
