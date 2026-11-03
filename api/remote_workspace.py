"""
api/remote_workspace.py — OADSON V3 Remote Workspace Router.

Lets the terminal client offload slow, network-heavy build work (npm
install, pip install, git clone of something large, npm run build) onto
Render's infrastructure instead of the user's own phone connection —
built specifically for a reported real constraint: the user's mobile
network is a bottleneck for exactly this class of command, while
Render's outbound network is fast and effectively unmetered by
comparison. The user runs the command via OADSON as normal; it executes
here instead of locally, and the finished files come back as a
downloadable zip the CLI auto-extracts into the local repo.

HONESTY, up front, because this matters for how the user should treat
it: render.yaml declares NO persistent disk volume for this service (see
that file's own service block) — a workspace directory here survives
between messages in an active session, but is wiped on the next deploy,
a Render restart, or free-tier spin-down after inactivity. This is a
BUILD ACCELERATOR, not a replacement for git as the source of truth. The
user should always pull the finished result down before ending a
session, not treat a remote workspace as somewhere their only copy of
anything should live.

Workspace root: {OADSON_HOME_ROOT}/_remote_workspaces/<user_id>/<workspace_id>/
(see core/oadson_home.py — moved under the same root as the chat
sandbox for the 2026-08-28 workspace restructure, prefixed with an
underscore to keep it visually distinct from per-user chat workspaces
since remote build workspaces are keyed by a user-chosen name, not the
fixed inputs/workspace/outputs layout chat sessions use.)
One workspace per (user, workspace_id) pair — the id is a name the user
picks (e.g. "mavixal-parent-portal"), not auto-generated, so the SAME
workspace can be reused across multiple messages in a session (install
once, build many times) rather than a fresh checkout every command.
"""

import os
import shutil
import subprocess
import logging
import uuid
import re
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dependency import get_current_user
from core.oadson_home import OADSON_HOME_ROOT

logger = logging.getLogger("oadson.api.remote_workspace")
router = APIRouter()

WORKSPACE_ROOT = Path(OADSON_HOME_ROOT) / "_remote_workspaces"
COMMAND_TIMEOUT = 60 # seconds — generous for npm install / build steps, well beyond REQUEST_TIMEOUT's 90s
MAX_WORKSPACE_NAME_LEN = 64

# BUG FOUND AND FIXED 2026-08-28 — real incident: the whole Render
# instance ran out of memory and had to be restarted by hand, with even
# unrelated endpoints (GET /sessions, GET /messages) failing during it —
# not just this one. Root cause: this endpoint ran arbitrary
# user-driven shell commands (npm install, npm run build, git clone —
# real build tooling, genuinely memory-hungry) as a child process INSIDE
# THE SAME CONTAINER as the live API server, with zero resource limits.
# Render's OOM killer acts on the whole container, not per-process — so
# one heavy build command doesn't just slow this endpoint down, it can
# take the entire service down, API server included. Two separate
# memory risks were both wide open here, and both needed fixing, not
# just one:
#
#   1. The CHILD process itself using unbounded RAM (npm/webpack/a
#      build tool legitimately using hundreds of MB+) — fixed via
#      _limit_child_memory below, a hard OS-level ceiling via
#      RLIMIT_AS applied to the child before it execs.
#
#   2. The PARENT (this API server) accumulating unbounded memory just
#      from READING the child's output — subprocess.run(capture_
#      output=True) buffers stdout/stderr ENTIRELY in the calling
#      process's own memory before returning, with no size limit at
#      all. A command with no real memory footprint of its own but
#      that prints a huge amount of text (a verbose build, a runaway
#      loop, `find /` by mistake) would bypass fix #1 entirely — the
#      RLIMIT_AS on the CHILD doesn't protect the PARENT process from
#      a giant pipe read. Fixed via _run_with_output_cap below,
#      switching from a single blocking capture_output=True call to an
#      incremental read loop with a hard cap — the process gets killed
#      the moment its combined output crosses that cap, rather than
#      being allowed to keep writing into this server's own memory
#      indefinitely.
MAX_CHILD_MEMORY_BYTES = 100* 1024 * 1024   # 100MB — adjust to real headroom on your actual Render plan; leaves room for the API server's own baseline usage on a small instance
MAX_OUTPUT_BYTES = 5 * 1024 * 1024           # 5MB combined stdout+stderr — generous for real build logs, nowhere near enough to meaningfully threaten memory on its own


def _limit_child_memory():
    """
    Run as preexec_fn — executes in the forked child, before exec(),
    imposing a hard virtual-memory ceiling on THAT process (and anything
    it execs into) via the OS's own rlimit mechanism. Unix/Linux only —
    Render's containers are Linux, so this is safe here; guarded anyway
    in case this code ever runs somewhere it isn't available.
    """
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MAX_CHILD_MEMORY_BYTES, MAX_CHILD_MEMORY_BYTES))
    except Exception:
        pass  # best-effort — never let the limit-setting itself break a legitimate command


def _run_with_output_cap(command: str, cwd: str, timeout: int):
    """
    Replaces subprocess.run(..., capture_output=True) — that call
    reads the ENTIRE stdout+stderr into this process's own memory
    before ever returning, with no ceiling. This streams both pipes
    incrementally instead and kills the child the instant their
    combined size crosses MAX_OUTPUT_BYTES, so a command that just
    prints a huge amount of text can't grow THIS server's memory
    unboundedly even though it has no large memory footprint of its
    own (which _limit_child_memory's RLIMIT_AS wouldn't catch at all).
    Returns an object with the same .stdout/.stderr/.returncode shape
    subprocess.run's CompletedProcess has, so the caller below needs no
    other changes.
    """
    import selectors
    import time

    class _Result:
        def __init__(self, stdout, stderr, returncode):
            self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    proc = subprocess.Popen(
        command, shell=True, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, preexec_fn=_limit_child_memory,
    )

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, "stderr")

    out_chunks, err_chunks = [], []
    total_bytes = 0
    start = time.monotonic()
    output_capped = False

    while sel.get_map():
        if time.monotonic() - start > timeout:
            proc.kill()
            proc.wait()
            raise subprocess.TimeoutExpired(command, timeout)

        for key, _ in sel.select(timeout=1):
            line = key.fileobj.readline()
            if not line:
                sel.unregister(key.fileobj)
                continue
            total_bytes += len(line.encode("utf-8", errors="ignore"))
            (out_chunks if key.data == "stdout" else err_chunks).append(line)
            if total_bytes > MAX_OUTPUT_BYTES:
                # BUG FOUND AND FIXED 2026-08-28 (same fix, caught by
                # actually running this against a real flood of output
                # before shipping it — see test in the accompanying
                # message) — killing the process here is NOT enough on
                # its own: SIGKILL stops it from writing MORE data, but
                # does nothing about data already sitting in the pipe
                # buffer from before the kill. The original version kept
                # looping (for the rest of this `for`, and then again in
                # the outer `while`) and kept right on reading everything
                # already buffered — for a fast producer like `yes`,
                # that was the ENTIRE output, making the cap a complete
                # no-op in practice despite firing correctly. Breaking
                # out of both loops immediately, right here, means
                # whatever's been collected up to THIS exact line is the
                # final result — a few KB over the cap at most, not
                # megabytes.
                proc.kill()
                stdout = "".join(out_chunks)
                stderr = "".join(err_chunks) + (
                    f"\n\n[oadson] Output exceeded {MAX_OUTPUT_BYTES // (1024*1024)}MB — "
                    f"process was killed and output truncated to protect server memory."
                )
                proc.wait()
                class _Result:
                    def __init__(self, stdout, stderr, returncode):
                        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode
                return _Result(stdout, stderr, proc.returncode)

        if proc.poll() is not None and not sel.get_map():
            break

    proc.wait()
    stdout = "".join(out_chunks)
    stderr = "".join(err_chunks)
    return _Result(stdout, stderr, proc.returncode)


def _safe_workspace_id(raw: str) -> str:
    """
    Sanitize a user-chosen workspace name into a safe directory
    component — alphanumeric, dash, underscore only, so there is no
    path-traversal risk (`../../etc`) and no accidental collision with
    OS-reserved names. Truncated to a sane length.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "-", raw.strip())
    cleaned = cleaned.strip("-") or "default"
    return cleaned[:MAX_WORKSPACE_NAME_LEN]


def _workspace_dir(user_id: str, workspace_id: str) -> Path:
    safe_user = re.sub(r"[^a-zA-Z0-9_-]", "-", str(user_id))[:64]
    safe_ws = _safe_workspace_id(workspace_id)
    return WORKSPACE_ROOT / safe_user / safe_ws


class RunRequest(BaseModel):
    workspace_id: str
    command: str
    # Relative path INSIDE the workspace to run the command from — lets
    # the client cd into a cloned repo's subdirectory without needing a
    # separate "cd" round-trip. Defaults to the workspace root itself.
    cwd: str | None = None


class DownloadRequest(BaseModel):
    workspace_id: str
    # Relative path INSIDE the workspace to zip — defaults to the whole
    # workspace. Lets the client grab just e.g. "dist/" after a build
    # instead of the entire node_modules-laden tree.
    path: str | None = None


class ClearRequest(BaseModel):
    workspace_id: str


@router.post("/remote-workspace/run")
async def remote_workspace_run(
    body: RunRequest,
    user: dict = Depends(get_current_user),
):
    """
    Run a shell command inside a persistent per-user workspace on this
    server's own filesystem. Same result shape as the terminal's local
    run_bash tool (status/stdout/stderr/return_code) so agent.py's
    existing tool-result handling needs no special-casing.

    The workspace directory is created on first use for a given
    workspace_id and reused on every subsequent call — this is what
    makes "npm install once, npm run build many times" actually work
    without re-downloading node_modules every single command.
    """
    if not body.command or not body.command.strip():
        raise HTTPException(400, "command cannot be empty")

    user_id = str(user.get("_id") or user.get("id") or user.get("email") or "anonymous")
    ws_dir = _workspace_dir(user_id, body.workspace_id)
    ws_dir.mkdir(parents=True, exist_ok=True)

    run_cwd = ws_dir
    if body.cwd:
        # Resolve strictly INSIDE the workspace — reject any path that
        # would escape it via ../ traversal.
        candidate = (ws_dir / body.cwd).resolve()
        if not str(candidate).startswith(str(ws_dir.resolve())):
            raise HTTPException(400, "cwd must stay inside the workspace")
        candidate.mkdir(parents=True, exist_ok=True)
        run_cwd = candidate

    # Server-side GITHUB_TOKEN (already a declared env var — see
    # render.yaml) is injected the same way the CLI does it locally for
    # the user's own token, so `git clone https://github.com/...` works
    # here without the user needing to configure anything separately for
    # remote workspaces specifically.
    #
    # FIX: was using "x-access-token:<token>@github.com" — that username
    # convention is GitHub's documented format specifically for GitHub
    # App INSTALLATION tokens, not guaranteed for a classic Personal
    # Access Token. skills/github/skill.py's own already-working use of
    # this SAME GITHUB_TOKEN env var treats it as a classic PAT (used as
    # a REST API bearer header: "Authorization: token X") — so the git
    # auth format needs to match that token type, not a GitHub App's.
    # The universally-documented classic-PAT git format is the token
    # alone before the @, with no username field at all.
    command = body.command
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if github_token and "https://github.com/" in command:
        command = command.replace(
            "https://github.com/",
            f"https://{github_token}@github.com/",
        )

    # IDEMPOTENT CLONE FIX: a real, reported failure loop — the FIRST
    # remote_bash call for a given repo succeeds and creates the target
    # directory, but the model has no visibility into that (it only
    # sees the request it sent, not the workspace's actual state before
    # sending it). Every retry of the same "git clone ... && cd ... &&
    # npm install" chain then hits "fatal: destination path '...'
    # already exists and is not an empty directory" and dies before
    # ever reaching the npm install part — on a slow/unstable
    # connection, each failed round-trip costs a full Gemini decision
    # cycle (seen in practice: 50-90+ seconds per attempt with provider
    # fallback churn) before the model happens to notice and pivot to
    # just cd-ing into the existing directory instead. Detecting this
    # deterministically, server-side, before ever running the command,
    # removes that whole class of wasted slow round-trips rather than
    # relying on the model to self-correct under pressure.
    FLAGS_WITH_VALUE = {"--depth", "--branch", "-b", "--origin", "-o", "--config", "-c"}

    def _clone_target_dir(clone_segment: str) -> str | None:
        """
        Parse just the first shell segment (up to the first && or ;) and
        return the local directory name `git clone` would create, or
        None if this segment isn't a git clone command at all. Handles
        flags that take a following value (--depth 1, -b main, etc) so
        their value isn't mistaken for the repo URL or target dirname.
        """
        parts = clone_segment.strip().split()
        if len(parts) < 3 or parts[0] != "git" or parts[1] != "clone":
            return None

        positional = []
        i = 2
        while i < len(parts):
            p = parts[i]
            if p.startswith("-"):
                i += 2 if (p in FLAGS_WITH_VALUE and i + 1 < len(parts)) else 1
                continue
            positional.append(p)
            i += 1

        if not positional:
            return None
        url = positional[0]
        explicit_dir = positional[1] if len(positional) > 1 else None
        if explicit_dir:
            return explicit_dir
        name = url.rstrip("/").split("/")[-1]
        return name[:-4] if name.endswith(".git") else name

    _split = re.match(r'^(.*?)(\s*(?:&&|;)\s*)(.*)$', command, re.DOTALL)
    _first_segment, _rest = (_split.group(1), _split.group(3)) if _split else (command, "")
    _clone_target = _clone_target_dir(_first_segment)

    if _clone_target and (ws_dir / _clone_target).is_dir():
        if not _rest.strip():
            # The entire command was just the clone — nothing else to
            # run against the existing checkout, report success directly.
            return {
                "status": "success",
                "command": body.command,
                "stdout": f"'{_clone_target}' already exists in this workspace — reusing existing checkout, clone skipped.",
                "stderr": None,
                "return_code": 0,
                "workspace_id": _safe_workspace_id(body.workspace_id),
            }
        command = _rest  # run just the remainder of the chain against the existing directory

    try:
        result = _run_with_output_cap(command, str(run_cwd), COMMAND_TIMEOUT)
        safe_stdout = result.stdout
        safe_stderr = result.stderr or None
        if github_token:
            safe_stdout = safe_stdout.replace(github_token, "***")
            if safe_stderr:
                safe_stderr = safe_stderr.replace(github_token, "***")

        return {
            "status": "success" if result.returncode == 0 else "error",
            "command": body.command,  # original, unmodified — never the token-embedded version
            "stdout": safe_stdout,
            "stderr": safe_stderr,
            "return_code": result.returncode,
            "workspace_id": _safe_workspace_id(body.workspace_id),
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "command": body.command,
            "reason": f"Command exceeded {COMMAND_TIMEOUT}s",
        }
    except Exception as e:
        return {"status": "error", "command": body.command, "reason": str(e)}


@router.post("/remote-workspace/download")
async def remote_workspace_download(
    body: DownloadRequest,
    user: dict = Depends(get_current_user),
):
    """
    Zip the workspace (or a subpath inside it) and return the zip as a
    direct binary download — the CLI's remote_download tool saves this
    to disk and extracts it into the user's local repo.
    """
    from fastapi.responses import FileResponse

    user_id = str(user.get("_id") or user.get("id") or user.get("email") or "anonymous")
    ws_dir = _workspace_dir(user_id, body.workspace_id)

    if not ws_dir.exists():
        raise HTTPException(404, f"Workspace '{body.workspace_id}' does not exist")

    target_dir = ws_dir
    if body.path:
        candidate = (ws_dir / body.path).resolve()
        if not str(candidate).startswith(str(ws_dir.resolve())):
            raise HTTPException(400, "path must stay inside the workspace")
        if not candidate.exists():
            raise HTTPException(404, f"Path '{body.path}' not found in workspace")
        target_dir = candidate

    zip_base = f"/tmp/oadson_download_{uuid.uuid4().hex[:12]}"
    zip_path = shutil.make_archive(zip_base, "zip", root_dir=target_dir)

    filename = f"{_safe_workspace_id(body.workspace_id)}.zip"
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
        background=None,  # cleanup handled by /tmp's own lifecycle on this ephemeral filesystem
    )


@router.post("/remote-workspace/clear")
async def remote_workspace_clear(
    body: ClearRequest,
    user: dict = Depends(get_current_user),
):
    """Delete a workspace entirely. Explicit user action only — never automatic."""
    user_id = str(user.get("_id") or user.get("id") or user.get("email") or "anonymous")
    ws_dir = _workspace_dir(user_id, body.workspace_id)

    if not ws_dir.exists():
        return {"status": "success", "message": "Workspace did not exist — nothing to clear"}

    shutil.rmtree(ws_dir)
    return {"status": "success", "message": f"Workspace '{body.workspace_id}' cleared"}


@router.get("/remote-workspace/list")
async def remote_workspace_list(
    user: dict = Depends(get_current_user),
):
    """List the current user's active workspace names and their approximate disk usage."""
    user_id = str(user.get("_id") or user.get("id") or user.get("email") or "anonymous")
    safe_user = re.sub(r"[^a-zA-Z0-9_-]", "-", user_id)[:64]
    user_root = WORKSPACE_ROOT / safe_user

    if not user_root.exists():
        return {"workspaces": []}

    workspaces = []
    for entry in user_root.iterdir():
        if entry.is_dir():
            try:
                size_bytes = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            except Exception:
                size_bytes = 0
            workspaces.append({
                "workspace_id": entry.name,
                "size_mb": round(size_bytes / (1024 * 1024), 2),
                "modified_at": datetime.fromtimestamp(
                    entry.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            })

    return {"workspaces": workspaces}
