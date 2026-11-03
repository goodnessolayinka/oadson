"""
core/oadson_home.py — OADSON V3 sandboxed workspace.

Replaces routers.oadson_home from V2.
Skills import from here: from core.oadson_home import get_user_home, is_safe_path

HARDENING 2026-08-28 — workspace restructure + restart resilience.

Root moved off /tmp. This was never actually a durability fix on its
own — Render's local disk (wherever it's mounted) is ephemeral either
way without an attached persistent Disk, and a plain rename doesn't
change that. What actually matters, and what this file now does:

  1. get_user_home() is UNCHANGED in meaning — it's still "the one
     root a skill works in" — every existing skill (terminal, github,
     document_reader, archive_creator, browser, dependency_audit,
     html_preview, backend_developer, website_developer,
     attachment_processor, whatsapp) keeps calling it exactly as
     before, zero code changes needed on their end. It now points at
     {ROOT}/{user_id}/workspace/ instead of {ROOT}/{user_id}/ directly
     — the same directory role (scratch space, cloned repos, builds),
     just under an explicit name instead of being the bare user
     folder, so it reads clearly next to its new siblings below.

  2. Two new sibling directories, opt-in via new helper functions —
     nothing existing has to adopt these, but new/updated call sites
     should:
       get_inputs_dir(user_id)  → {ROOT}/{user_id}/inputs/
         Where a file the user actually sends OADSON lands — chat
         uploads, WhatsApp media, pasted text. document_reader's
         fetch_upload and the sandbox-save helpers below now default
         here instead of dumping into the same flat folder repos get
         cloned into, which is exactly the "regardless of cloning or
         me sending it a file" mixing that caused confusion.
       get_outputs_dir(user_id) → {ROOT}/{user_id}/outputs/
         Where a finished deliverable goes — zips, generated docs,
         anything meant to be handed back to the user via a download
         link. archive_creator/file_generation-style skills should
         write here.

  3. Boot-generation tracking — the actual fix for "the disk got
     silently wiped by a restart mid-session and nothing noticed".
     Render's ephemeral disk (whatever path it's mounted at, /tmp or
     otherwise) does not survive a process restart, and this app
     restarts on every deploy — confirmed directly in production logs:
     a full bootstrap cycle landed in the middle of an active
     terminal/github session, and everything after it — cd into a
     just-cloned repo, git status, reading files — failed with
     "No such file or directory" / "not a git repository" for the
     rest of that session, burning 15+ retries because nothing told
     the model WHY. current_boot_id() returns a fresh id generated
     once per process start (see bootstrap.py); needs_reclone()
     compares that against a marker file written into each user's
     workspace the first time it's touched after boot. A mismatch (or
     a missing marker) means "this workspace is from a previous
     process — assume it's empty and reclone/refetch before trusting
     any path in it", which is exactly what the terminal and github
     skills now check before running anything that depends on prior
     state (see their own comments). Per explicit instruction: no
     warning message, no asking the user — just silently
     re-materialize what's needed and continue.
"""

import os
import uuid
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("oadson.oadson_home")

# Moved off /tmp per explicit request — functionally the same kind of
# ephemeral local disk either way on this Render instance (no
# persistent Disk attached — see render.yaml), but this path reads
# honestly as "OADSON's own project-local workspace" rather than
# implying general-purpose scratch space, and if a Render persistent
# Disk ever IS attached, pointing OADSON_HOME_ROOT at its mount path
# is the only change needed anywhere to make this genuinely durable
# across restarts — nothing else in this file or its callers changes.
#
# Named plainly "oadson" (not "oadson_workspace") — the workspace/
# subfolder underneath already carries that name per user
# (see get_user_home() below), sitting alongside its siblings
# inputs/ and outputs/, so putting "workspace" in the root too was
# redundant (.../oadson_workspace/{user_id}/workspace/...). This is
# just the root — everything under it is one user's inputs, workspace,
# or outputs.
OADSON_HOME_ROOT = os.getenv("OADSON_HOME_ROOT", "/opt/render/project/oadson")

# ── Boot generation tracking ──────────────────────────────────────────
# One id per process start. bootstrap.py calls current_boot_id() once
# at startup (see its own comment) purely to make sure it's generated
# before anything else can race it; every later call in this process
# gets the same value back.
_BOOT_ID = uuid.uuid4().hex[:12]


def current_boot_id() -> str:
    """The id for THIS running process. Same value for the whole process lifetime."""
    return _BOOT_ID


def _boot_marker_path(user_id: str) -> Path:
    return Path(OADSON_HOME_ROOT) / str(user_id) / ".boot_id"


def needs_reclone(user_id: str) -> bool:
    """
    True if this user's workspace was last touched by a DIFFERENT
    process than the one running right now — meaning the disk under it
    was very likely wiped (restart/redeploy) and anything that was
    there before (cloned repos, in-progress builds) should be treated
    as gone, not trusted.

    Callers that confirm this should reclone/refetch whatever they
    need and then call mark_fresh() — see terminal and github skills'
    own call sites for the pattern.
    """
    marker = _boot_marker_path(user_id)
    try:
        if not marker.exists():
            return True
        return marker.read_text().strip() != _BOOT_ID
    except Exception:
        # Can't tell — safer to assume it needs a reclone than to trust
        # a workspace we couldn't verify.
        return True


def mark_fresh(user_id: str) -> None:
    """Record that this user's workspace is now known-good for THIS boot."""
    try:
        marker = _boot_marker_path(user_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(_BOOT_ID)
    except Exception as e:
        logger.warning(f"Could not write boot marker for {user_id}: {e}")


# ── Directory roots ─────────────────────────────────────────────────

def get_user_home(user_id: str) -> Path:
    """
    The workspace root — same role this function has always had
    (scratch space, cloned repos under repos/, builds). Physically now
    {ROOT}/{user_id}/workspace/ — see module docstring point 1. Every
    existing caller keeps working unchanged; this is purely a path
    change under the hood.
    """
    home = Path(OADSON_HOME_ROOT) / str(user_id) / "workspace"
    home.mkdir(parents=True, exist_ok=True)
    return home


def get_inputs_dir(user_id: str) -> Path:
    """Where files the user sends OADSON land — uploads, WhatsApp media, pasted text."""
    d = Path(OADSON_HOME_ROOT) / str(user_id) / "inputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_outputs_dir(user_id: str) -> Path:
    """Where finished deliverables go — zips, generated docs, anything meant for a download link."""
    d = Path(OADSON_HOME_ROOT) / str(user_id) / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_safe_within(root: Path, path: str) -> bool:
    try:
        target = (root / path).resolve()
        return target.is_relative_to(root.resolve())
    except Exception:
        return False


def is_safe_path(user_id: str, path: str) -> bool:
    """Unchanged signature/behavior — checks containment within the workspace root (get_user_home)."""
    return _is_safe_within(get_user_home(user_id), path)


def is_safe_inputs_path(user_id: str, path: str) -> bool:
    return _is_safe_within(get_inputs_dir(user_id), path)


def is_safe_outputs_path(user_id: str, path: str) -> bool:
    return _is_safe_within(get_outputs_dir(user_id), path)


def save_to_sandbox(user_id: str, filename: str, content, area: str = "inputs") -> tuple:
    """
    Save content into the user's sandbox. Accepts either str (text) or
    bytes (binary); writes accordingly.

    area: "inputs" (default — a file the user sent OADSON: chat
    upload, WhatsApp media, pasted text) or "workspace" (dropped
    directly where terminal/github work, e.g. a file a skill is
    generating mid-task). Defaults to "inputs" now — previously
    everything landed in the same flat folder repos get cloned into
    (the exact "regardless of cloning or me sending it a file" mixing
    this was built to fix); existing callers that want the old flat
    behavior can pass area="workspace" explicitly.

    Shared by api/whatsapp.py's document handling and the web chat
    upload path (api/chat.py, api/chat_stream.py) — a file "attached"
    to a message previously only ever got a Cloudinary URL note in the
    prompt text, which the model can't fetch, so it either said it
    couldn't read the file or — worse — guessed a plausible-sounding
    description from the filename and general context instead of
    reading anything real. Saving here means @document_reader can
    genuinely open it.

    Returns (relative_path, None) on success, or (None, error_message).
    Never raises.
    """
    MAX_BYTES = 15 * 1024 * 1024  # 15MB — generous for source/text files
    try:
        size = len(content.encode("utf-8")) if isinstance(content, str) else len(content)
        if size > MAX_BYTES:
            return None, f"file too large ({size // (1024*1024)}MB, max 15MB)"

        safe_name = os.path.basename(filename) or "attachment"
        home = get_inputs_dir(user_id) if area == "inputs" else get_user_home(user_id)

        # Avoid silently clobbering an existing file with the same name.
        candidate = safe_name
        counter = 1
        while (home / candidate).exists():
            stem, dot, ext = safe_name.rpartition(".")
            candidate = f"{stem}_{counter}.{ext}" if dot else f"{safe_name}_{counter}"
            counter += 1

        is_safe = is_safe_inputs_path(user_id, candidate) if area == "inputs" else is_safe_path(user_id, candidate)
        if not is_safe:
            return None, "invalid filename"

        target = home / candidate
        if isinstance(content, str):
            target.write_text(content, encoding="utf-8")
        else:
            target.write_bytes(content)
        return candidate, None
    except Exception as e:
        logger.error(f"Failed to save {filename} to sandbox: {e}")
        return None, str(e)


# ── MEMORY-AWARE HEAVY-OPERATION GUARD 2026-08-28 ─────────────────────
# Added after auditing clone_to_workspace() for OOM risk on this app's
# 512MB box. Two pieces, deliberately kept separate:
#
#   1. memory_headroom_mb() / has_memory_headroom() — a live,
#      synchronous check (psutil.virtual_memory(), already a dependency
#      — see skills/system_status/skill.py) called RIGHT BEFORE a
#      memory-heavy step (buffering a downloaded zip, decompressing
#      it), not on a timer or in the background. This is the part that
#      actually prevents a crash: refuse the operation with a clear
#      reason before allocating, instead of allocating and finding out
#      from the OS's OOM killer. A periodic background poller would
#      only ever produce a historical chart, not stop the allocation
#      that's about to happen — this has to sit in the hot path of
#      whatever's about to eat memory to matter.
#
#   2. log_memory_event() — best-effort, fire-and-forget write to
#      db.memory_events for the "what was memory doing before it
#      crashed" question. Deliberately NOT written to local disk or
#      kept in a process-level list: either one dies with the process,
#      which is exactly the moment you'd want to inspect it after.
#      Deliberately NOT called on every request either — that would be
#      its own resource cost for a 512MB box (a write per request,
#      forever) for very little signal. Called only at the two points
#      that actually matter: when a heavy operation is ABOUT to run
#      (so there's a record even if the process dies mid-operation),
#      and when has_memory_headroom() actually blocks something (so a
#      refusal is diagnosable after the fact, not just a message the
#      user saw once). db.memory_events is a small capped collection
#      (see ensure_memory_events_capped()) — bounded by document count
#      AND total size, so this can never itself grow into a memory or
#      storage problem.

MEMORY_EVENTS_MAX_DOCS = 500
MEMORY_EVENTS_MAX_BYTES = 2 * 1024 * 1024  # 2MB cap for the whole collection


def _cgroup_memory_available_mb() -> Optional[float]:
    """
    FIX 2026-08-29 — real bug, confirmed live in production: this whole
    guard was built on psutil.virtual_memory(), which reads the
    PHYSICAL HOST's memory, not this container's real cgroup-enforced
    limit. On Render's shared infrastructure the host is a large
    machine (confirmed directly: a 512MB Render instance reported
    "18GB used of 30GB total, 12GB free" via the exact same psutil
    call this function used to make). That means has_memory_headroom()
    was checking headroom against a number that could never realistically
    run low — the ENTIRE point of this guard (refuse a heavy operation
    like a github clone before it OOMs the real 512MB container) could
    never actually trigger. Same underlying fix as
    skills/system_status/skill.py's _cgroup_memory_limits — reads the
    real cgroup v2 (or v1 fallback) memory limit/usage directly from
    /sys/fs/cgroup, which is what's actually enforced against this
    process. Kept as a separate small function here rather than
    importing across skills/core (skills should stay independently
    loadable — see skill_loader.py's by-path loading) even though the
    logic mirrors system_status's version.
    """
    try:
        v2_max, v2_current = "/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"
        if os.path.exists(v2_max) and os.path.exists(v2_current):
            with open(v2_max) as f:
                raw_limit = f.read().strip()
            if raw_limit != "max":
                with open(v2_current) as f:
                    used = int(f.read().strip())
                return round((int(raw_limit) - used) / (1024 * 1024), 1)
    except Exception:
        pass
    try:
        v1_limit, v1_usage = "/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes"
        if os.path.exists(v1_limit) and os.path.exists(v1_usage):
            with open(v1_limit) as f:
                limit = int(f.read().strip())
            if limit <= (1 << 62):  # rules out v1's "unlimited" sentinel
                with open(v1_usage) as f:
                    used = int(f.read().strip())
                return round((limit - used) / (1024 * 1024), 1)
    except Exception:
        pass
    return None


def memory_headroom_mb() -> Optional[float]:
    """
    MB of RAM still available right now for THIS container — prefers
    the real cgroup limit (see _cgroup_memory_available_mb) over
    psutil's host-wide reading, falling back to psutil only when no
    cgroup limit could be read at all (e.g. local dev outside a
    container, where "host" and "this process's budget" are the same
    thing anyway). Returns None if neither could be read — callers
    should treat None as "couldn't verify, proceed" rather than
    blocking on an unrelated failure (psutil not being importable
    shouldn't take down a clone).
    """
    cgroup_mb = _cgroup_memory_available_mb()
    if cgroup_mb is not None:
        return cgroup_mb
    try:
        import psutil
        return round(psutil.virtual_memory().available / (1024 * 1024), 1)
    except Exception as e:
        logger.warning(f"memory_headroom_mb: could not read memory info: {e}")
        return None


def has_memory_headroom(min_mb: float = 100) -> tuple[bool, Optional[float]]:
    """
    True if there's at least `min_mb` of RAM free right now. Returns
    (ok, headroom_mb) — headroom_mb is None if it couldn't be checked,
    in which case ok is True (fail open — see memory_headroom_mb()).

    Default of 100MB is deliberately conservative for a 512MB box: the
    app itself, the Mongo driver, and any other in-flight request are
    already using a chunk of that before this check even runs, so
    100MB free is closer to "genuinely tight" than "plenty of room" —
    callers doing something that could itself use tens of MB (a zip
    download/decompress) should treat a pass here as "safe enough to
    start", not "guaranteed to finish", and should still respect their
    own hard size caps (e.g. github.skill's MAX_ZIPBALL_BYTES) on top.
    """
    headroom = memory_headroom_mb()
    if headroom is None:
        return True, None
    return headroom >= min_mb, headroom


async def log_memory_event(db, event: str, user_id: str = None, detail: dict = None) -> None:
    """
    Best-effort. Never raises, never blocks the caller on failure — a
    logging call should not be able to break the operation it's
    logging. See module-level comment above for why this exists and
    why it's called only at heavy-operation start and refusal, not on
    every request.
    """
    if db is None:
        return
    try:
        headroom = memory_headroom_mb()
        await db["memory_events"].insert_one({
            "event": event,
            "user_id": user_id,
            "headroom_mb": headroom,
            "detail": detail or {},
            "boot_id": _BOOT_ID,
            "ts": __import__("datetime").datetime.utcnow(),
        })
    except Exception as e:
        logger.warning(f"log_memory_event failed (non-fatal): {e}")


async def ensure_memory_events_capped(db) -> None:
    """
    Call once at bootstrap (see bootstrap.py) to make db.memory_events
    a real MongoDB capped collection — bounded by both document count
    AND total byte size, enforced by MongoDB itself (oldest documents
    auto-evicted on insert once either limit is hit), so this can never
    grow unbounded regardless of how log_memory_event() gets called
    over the app's lifetime. No-ops quietly if the collection already
    exists (Mongo doesn't allow re-capping an existing collection, and
    that's fine — this only needs to run once, ever, per database).

    FIX 2026-08-29: `db` here is core.db_router.MultiClusterDB, not a
    raw Motor database — confirmed in production (
    "'MultiClusterDB' object has no attribute 'list_collection_names'",
    caught non-fatally, but memory_events was silently NOT getting
    capped as a result — every insert just landed in an ordinary
    uncapped collection instead). MultiClusterDB only implements
    __getitem__ (for db["collection"]) and command() — it deliberately
    doesn't proxy every raw database method (see its own docstring).
    Rather than extend that wrapper for one caller, go through the
    actual underlying Motor database that db["memory_events"] already
    resolves to — db["memory_events"].database is the real
    AsyncIOMotorDatabase Motor collections always expose, regardless of
    what wrapper handed the collection out, so this works whether `db`
    is a MultiClusterDB, a plain Motor database, or anything else with
    the same db["name"] convention, with no dependency on MultiClusterDB
    specifically.
    """
    if db is None:
        return
    try:
        real_db = db["memory_events"].database
        existing = await real_db.list_collection_names(filter={"name": "memory_events"})
        if existing:
            return
        await real_db.create_collection(
            "memory_events", capped=True, size=MEMORY_EVENTS_MAX_BYTES, max=MEMORY_EVENTS_MAX_DOCS,
        )
        logger.info(f"Created capped memory_events collection ({MEMORY_EVENTS_MAX_DOCS} docs / {MEMORY_EVENTS_MAX_BYTES} bytes max)")
    except Exception as e:
        logger.warning(f"ensure_memory_events_capped failed (non-fatal): {e}")
