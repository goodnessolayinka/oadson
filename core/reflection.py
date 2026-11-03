"""
core/reflection.py — OADSON V3 Reflection Memory.

Records structured failure reflections to the memory vault so future
turns and future sessions know a skill has been flaky instead of
rediscovering it from scratch every time.

UPGRADED per EDD v1.0 (July 2026):

  Before: stored a single flat string ("terminal (default) has failed 2x...")
          under a generic key. Passive — recorded but never read back into
          context. Error type was never classified. stderr was never stored.

  After:
    - Structured record with error_type classification, stderr, command,
      resolution tracking (resolved: bool), and session_id.
    - Error type is classified from stderr patterns:
        network_blocked, permission_denied, timeout, not_found,
        syntax_error, unknown
    - Reflections stored under reflections:{skill}:{id} namespace in the
      memory vault — queryable, not just appendable.
    - get_recent_reflections() allows the orchestrator to inject recent
      failure context into the model prompt before a retry (Phase 2 of
      the active reflection loop).
    - update_reflection_resolution() marks a reflection resolved=True
      when a subsequent attempt succeeds — closes the loop.
    - Deliberate threshold still applies: a single failure doesn't write
      a reflection. FAILURE_THRESHOLD consecutive failures do.
"""

import logging
import uuid
from datetime import datetime, timezone
from memory.vault import MemoryVault

logger = logging.getLogger("oadson.reflection")

FAILURE_THRESHOLD = 2

# Error type classifier — maps stderr patterns to typed categories
_ERROR_TYPE_PATTERNS = [
    ("network_blocked",    ["connection refused", "network unreachable", "name or service not known",
                            "could not resolve host", "errno 111", "errno 101", "no route to host",
                            "connection timed out", "failed to establish"]),
    ("permission_denied",  ["permission denied", "errno 13", "access denied", "operation not permitted"]),
    ("not_found",          ["no such file or directory", "command not found", "modulenotfounderror",
                            "importerror", "no module named", "errno 2"]),
    ("syntax_error",       ["syntaxerror", "indentationerror", "invalid syntax", "traceback"]),
    ("timeout",            ["timeout", "timed out", "deadline exceeded"]),
]


def _classify_error(stderr: str, stdout: str, reason: str) -> str:
    """Classify an error into a typed category from available text."""
    combined = " ".join([
        (stderr or ""), (stdout or ""), (reason or "")
    ]).lower()
    for error_type, patterns in _ERROR_TYPE_PATTERNS:
        if any(p in combined for p in patterns):
            return error_type
    return "unknown"


async def track_and_maybe_reflect(
    ctx: dict, db, skill_name: str, args: dict, exec_result: dict, confidence: dict,
) -> None:
    """
    Call right after scoring confidence for a skill result. Tracks
    repetition in Mongo (survives across requests) and writes a
    structured reflection once FAILURE_THRESHOLD is crossed.

    Never raises — reflection is best-effort telemetry, not part of the
    actual decision path, and must never break the agent loop.
    """
    try:
        user_id = ctx.get("user_id")
        if not user_id or db is None:
            return

        action = (args or {}).get("action", "")
        tracking_key = f"{skill_name}:{action}"
        is_failure = (
            (exec_result or {}).get("status") in ("error", "timeout", "blocked")
            or confidence.get("label") == "low"
        )

        counters = db["skill_failure_counts"]

        if not is_failure:
            await counters.delete_one({"user_id": user_id, "tracking_key": tracking_key})
            return

        doc = await counters.find_one_and_update(
            {"user_id": user_id, "tracking_key": tracking_key},
            {"$inc": {"count": 1}, "$set": {"skill": skill_name, "action": action}},
            upsert=True,
            return_document=True,
        )
        count = doc["count"] if doc else 1

        if count < FAILURE_THRESHOLD:
            return

        # Build structured reflection
        stderr  = (exec_result or {}).get("stderr") or ""
        stdout  = (exec_result or {}).get("stdout") or ""
        reason  = (exec_result or {}).get("reason") or (exec_result or {}).get("error") or ""
        command = (exec_result or {}).get("command") or (args or {}).get("query") or ""

        error_type = _classify_error(stderr, stdout, reason)
        # Override with "network_blocked" if skill itself reported it
        if (exec_result or {}).get("status") == "blocked":
            error_type = "network_blocked"

        reflection_id = f"reflection_{skill_name}_{action or 'default'}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        record = {
            "reflection_id": reflection_id,
            "skill_name": skill_name,
            "action": action or "default",
            "command": command,
            "error_type": error_type,
            "stdout": stdout[:500] if stdout else "",
            "stderr": stderr[:500] if stderr else "",
            "reason": reason[:500] if reason else "",
            "confidence_reasons": confidence.get("reasons", []),
            "failure_count": count,
            "resolved": False,
            "resolution": None,
            "session_id": ctx.get("session_id", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        vault = MemoryVault(db, user_id)
        await vault.store(reflection_id, record, category="reflection")
        logger.info(f"Reflection recorded: {reflection_id} (error_type={error_type}, count={count})")

    except Exception as e:
        logger.warning(f"Reflection tracking failed (non-fatal) for {skill_name}: {e}")


async def get_recent_reflections(db, user_id: str, skill_name: str, limit: int = 3) -> list[dict]:
    """
    Retrieve recent unresolved reflections for a skill — used by the
    orchestrator to inject failure context into the model prompt before
    retrying, so the model knows what went wrong last time and can
    self-correct instead of repeating the same broken call.

    Returns a list of dicts, newest first. Returns [] on any error.
    """
    try:
        if not db or not user_id:
            return []
        vault = MemoryVault(db, user_id)
        all_reflections = await vault.list(category="reflection")
        skill_reflections = [
            r for r in all_reflections
            if isinstance(r.get("value"), dict)
            and r["value"].get("skill_name") == skill_name
            and not r["value"].get("resolved", False)
        ]
        # Sort by timestamp descending
        skill_reflections.sort(
            key=lambda r: r["value"].get("timestamp", ""),
            reverse=True,
        )
        return [r["value"] for r in skill_reflections[:limit]]
    except Exception as e:
        logger.warning(f"get_recent_reflections failed for {skill_name}: {e}")
        return []


async def update_reflection_resolution(
    db, user_id: str, skill_name: str, resolution: str,
) -> None:
    """
    Mark the most recent unresolved reflection for a skill as resolved.
    Called by the orchestrator when a skill succeeds after prior failures,
    to close the reflection loop.
    """
    try:
        if not db or not user_id:
            return
        vault = MemoryVault(db, user_id)
        all_reflections = await vault.list(category="reflection")
        unresolved = [
            r for r in all_reflections
            if isinstance(r.get("value"), dict)
            and r["value"].get("skill_name") == skill_name
            and not r["value"].get("resolved", False)
        ]
        if not unresolved:
            return
        # Update the most recent one
        unresolved.sort(key=lambda r: r["value"].get("timestamp", ""), reverse=True)
        target = unresolved[0]
        updated = {**target["value"], "resolved": True, "resolution": resolution}
        await vault.store(target["key"], updated, category="reflection")
        logger.info(f"Reflection resolved: {target['key']}")
    except Exception as e:
        logger.warning(f"update_reflection_resolution failed for {skill_name}: {e}")
