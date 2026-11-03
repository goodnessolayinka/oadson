"""
core/skill_executor.py — OADSON V3 Skill Executor.

Timeout enforcement. Retry logic. Normalised output.
Extracted so the orchestrator stays clean.

Nothing calls skills directly except this.

KEY DISTINCTION: two different kinds of "error":
  1. SKILL ERROR — the skill itself crashed, timed out, or isn't in the
     registry. skill_executor wraps these into a normalised error dict.
     These are retryable.
  2. COMMAND/ACTION ERROR — the skill ran fine, but the thing it did
     returned a non-success status (e.g. terminal ran `git clone` and
     the command exited non-zero; github returned 404). The skill's own
     result dict already contains full context (stdout, stderr, reason).
     These MUST pass straight through to the model — the model needs the
     actual error message to self-correct, not a stripped "unknown skill
     error".

Previously, case 2 was being intercepted and its error message discarded
because result.get("error") looked for the key "error" — but most skills
(terminal in particular) use "reason" as their error key. This meant
every terminal command that failed non-zero produced:
  status=error, error="unknown skill error"
and the model had nothing to work with. Fixed by passing all skill-
returned dicts through directly when status is anything other than a
runtime exception or timeout.
"""

import asyncio
import logging
from core.skill_registry import SkillRegistry

logger = logging.getLogger("oadson.skill_executor")

DEFAULT_TIMEOUT = 30     # seconds
DEFAULT_RETRIES = 2

# Statuses that the skill itself emits intentionally — pass them through
# to the model verbatim. These are NOT skill failures; they are results.
PASSTHROUGH_STATUSES = {"success", "info", "error", "timeout", "blocked"}


def _normalize_unknown_status(raw: str) -> str:
    """
    Maps an arbitrary skill-chosen status word onto the nearest real
    PASSTHROUGH_STATUSES value. Verified against every actual value
    found across the 13 skills this fix was written for: "boot",
    "resumed", "ok", "override_recorded" → success; "refused",
    "pending_confirmation", "pending" → blocked; "unavailable" → error;
    "completed", "applied", "warning", "empty", "checking" → success.
    Deliberately keyword-based rather than an exhaustive lookup table —
    this needs to handle status words from skills that don't exist yet
    too, not just the ones already found.
    """
    r = raw.lower()
    if any(k in r for k in ("error", "fail", "invalid", "unavailable")):
        return "error"
    if any(k in r for k in ("pending", "block", "wait", "confirm", "refuse", "denied")):
        return "blocked"
    if any(k in r for k in ("timeout", "expired")):
        return "timeout"
    return "success"


class SkillExecutor:

    def __init__(
        self,
        registry: SkillRegistry,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_RETRIES,
    ) -> None:
        self.registry = registry
        self.timeout = timeout
        self.max_retries = max_retries

    async def execute(self, name: str, payload: dict) -> dict:
        # WORKER LINK — an allowlisted skill runs on the worker when it is enabled
        # and reachable. Otherwise it runs here, exactly as before.
        from core import approvals
        args = dict(payload.get("args") or {})
        action = args.get("action")
        uid = str(payload.get("user_id") or "")
        if approvals.needs_approval(name, action):
            code = args.get("approval_id")
            if not (code and approvals.consume(uid, name, action, args, code)):
                req = approvals.request(uid, name, action, args)
                return {"status": "needs_approval", "approval_id": req,
                        "reason": f"'{action}' changes something outside the sandbox and needs your approval.",
                        "explanation": f"Reply 'approve {req}' to allow it. It expires in 30 minutes."}
        from core.worker_client import try_remote
        remote = await try_remote(name, payload)
        if remote is not None:
            return remote
        return await self._execute_local(name, payload)

    async def _execute_local(self, name: str, payload: dict) -> dict:
        """
        Execute a skill with timeout + retry.

        Returns the skill's own result dict when the skill ran (regardless
        of whether the action succeeded or failed — the model needs that
        full context). Only wraps into a normalised error dict when the
        skill itself couldn't run (registry miss, runtime exception, timeout).
        """
        if not self.registry.has(name):
            return {
                "skill": name,
                "status": "error",
                "data": None,
                "error": f"Skill '{name}' not in registry.",
                "attempts": 0,
            }

        last_error = ""
        attempt = 1
        for attempt in range(1, self.max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    self.registry.execute(name, payload),
                    timeout=self.timeout,
                )
                result["attempts"] = attempt

                # Pass through any status the skill itself returned.
                # "success" and "info" are clean; "error", "timeout", and
                # "blocked" are intentional skill responses the model must
                # see in full to self-correct — DO NOT strip or replace them.
                status = result.get("status")
                if status in PASSTHROUGH_STATUSES:
                    if status not in ("success", "info"):
                        # Log it for observability but still pass through.
                        reason = (
                            result.get("reason")
                            or result.get("error")
                            or result.get("message")
                            or "no reason provided"
                        )
                        logger.warning(
                            f"Skill '{name}' returned status={status} "
                            f"(attempt {attempt}): {reason}"
                        )
                    return result

                # REGRESSION FOUND AND RE-FIXED 2026-08-28 — this whole
                # block had been reverted back, word-for-word, to the
                # exact original buggy code ("Unknown status — treat as a
                # skill bug, retry.") that caused 13 skills (ilana +12
                # more, found by core/self_test.py's checks) to have
                # their entire real output silently discarded on every
                # call. See core/self_test.py's _test_all_skills_dry_run
                # for the full history/reasoning — restoring the same
                # fix here: retrying is worthless for this bug class
                # (a status-naming mismatch is deterministic — the exact
                # same "wrong" word comes back every retry), so any
                # non-empty string status is treated as a real, intended
                # result and normalized onto the nearest actual
                # PASSTHROUGH_STATUSES value instead of being discarded.
                if isinstance(status, str) and status.strip():
                    normalized = _normalize_unknown_status(status)
                    logger.warning(
                        f"Skill '{name}' returned non-standard status={status!r} "
                        f"(attempt {attempt}) — normalizing to '{normalized}' and passing "
                        f"through rather than discarding; original preserved as "
                        f"skill_status. Consider updating the skill to use one of "
                        f"{sorted(PASSTHROUGH_STATUSES)} directly."
                    )
                    result["skill_status"] = status
                    result["status"] = normalized
                    return result

                # Genuinely missing/empty/non-string status — this IS a
                # real skill bug worth retrying, since it suggests
                # something crashed or returned malformed data rather
                # than just using the wrong word for a real result.
                last_error = f"Missing or invalid status from skill '{name}': {status!r}"
                logger.warning(f"{last_error} (attempt {attempt})")
                if attempt < self.max_retries:
                    await asyncio.sleep(0.3)

            except asyncio.TimeoutError:
                last_error = f"Skill '{name}' timed out after {self.timeout}s."
                logger.warning(f"{last_error} Attempt {attempt}/{self.max_retries}")
                if attempt == self.max_retries:
                    break
                await asyncio.sleep(0.5)

            except Exception as e:
                last_error = str(e)
                logger.error(f"Skill '{name}' raised (attempt {attempt}): {e}", exc_info=True)
                break

        return {
            "skill": name,
            "status": "error",
            "data": None,
            "error": last_error,
            "attempts": attempt,
        }
