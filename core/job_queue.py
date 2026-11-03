"""
core/job_queue.py — Phase 10, Group 4: Background Task Execution.

Designed around what's actually true on Render's free tier, not a generic
"background worker" assumption:

  - Free web services spin DOWN after 15 minutes with no inbound traffic,
    and cold-start (~30-60s) on the next request. A naive always-on
    asyncio loop would just stop existing the moment the process sleeps —
    it doesn't pause and resume, the whole process goes away.
  - Render's Background Worker service type (a real 24/7 process) is a
    SEPARATE PAID SERVICE, not something the existing free web service
    gets for free.
  - The filesystem is ephemeral — any local state is gone on
    restart/redeploy. So job state lives entirely in Mongo, never on disk.

Given that, this is deliberately NOT "start a worker and forget it." It's:
  1. Jobs persist in Mongo (`jobs` collection) — safe across restarts,
     safe if the process dies mid-job (job just sits at "running" and a
     future poll can detect + retry stale ones).
  2. An in-process asyncio loop processes jobs WHILE the service happens
     to be awake — started on app startup, and it simply stops existing
     when Render spins the service down. That's fine: jobs already in
     Mongo aren't lost, they just wait.
  3. A "piggyback" hook — call `kick_job_queue()` from any request path
     (cheap, fire-and-forget) so real traffic hitting the service also
     nudges pending jobs forward, not just the dedicated loop. This is
     the actual mechanism that makes jobs progress on a free tier that
     sleeps: your own usage (or an external keep-alive ping / your mobile
     app's periodic poll) is what wakes job processing, not a magic
     always-on process that doesn't exist on this tier.

Honest limitation, stated plainly: if nothing hits the service for a long
stretch (you close the app and don't reopen it, no keep-alive ping
configured), a queued job can sit pending for a while — potentially past
whatever time you expected it to run by. This is NOT true 24/7 background
execution. It's "keeps working while the app is warm, resumes safely
whenever it wakes up next." Getting actual 24/7 execution requires either
Render's paid Background Worker service type, or an external scheduled
ping (e.g. a free cron service hitting /health every 10 min) to keep the
free web service from sleeping in the first place.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta

logger = logging.getLogger("oadson.job_queue")

# If a job has been "running" longer than this with no update, assume the
# process that claimed it died mid-job (e.g. Render spun down or
# redeployed) and make it eligible to be claimed again.
STALE_RUNNING_TIMEOUT = timedelta(minutes=10)

MAX_ATTEMPTS = 3


async def enqueue_job(db, user_id: str, session_id: str, job_type: str, payload: dict) -> str:
    """Insert a pending job, return its id immediately (does not wait for it to run)."""
    job_id = str(uuid.uuid4())
    now = datetime.utcnow()
    await db["jobs"].insert_one({
        "job_id": job_id,
        "user_id": user_id,
        "session_id": session_id,
        "type": job_type,
        "payload": payload,
        "status": "pending",
        "result": None,
        "error": None,
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    })
    logger.info(f"Job enqueued: {job_id} ({job_type})")
    return job_id


async def get_job(db, user_id: str, job_id: str) -> dict | None:
    doc = await db["jobs"].find_one({"user_id": user_id, "job_id": job_id}, {"_id": 0})
    return doc


async def list_jobs(db, user_id: str, limit: int = 20) -> list:
    cursor = db["jobs"].find(
        {"user_id": user_id}, {"_id": 0},
    ).sort("created_at", -1).limit(limit)
    return [d async for d in cursor]


async def _claim_next_job(db) -> dict | None:
    """
    Atomically claim one pending (or stale-running) job. find_one_and_update
    with a status filter is atomic in Mongo, so two overlapping calls to
    this (e.g. the loop and a piggyback kick landing at the same instant)
    can't both claim the same job.
    """
    now = datetime.utcnow()
    stale_cutoff = now - STALE_RUNNING_TIMEOUT

    doc = await db["jobs"].find_one_and_update(
        {
            "$or": [
                {"status": "pending"},
                {"status": "running", "updated_at": {"$lt": stale_cutoff}, "attempts": {"$lt": MAX_ATTEMPTS}},
            ],
        },
        {"$set": {"status": "running", "updated_at": now}, "$inc": {"attempts": 1}},
        sort=[("created_at", 1)],
        return_document=True,
    )
    return doc


async def process_next_job(db, orchestrator) -> bool:
    """
    Claims and runs exactly one pending job, if any exist. Returns True if
    a job was processed (whether it succeeded or errored), False if the
    queue was empty — the caller (loop or piggyback hook) uses this to
    decide whether to immediately try for another or back off.
    """
    job = await _claim_next_job(db)
    if not job:
        return False

    job_id = job["job_id"]
    try:
        if job["type"] == "chat":
            user_doc = await db["users"].find_one({"_id": job["user_id"]}) or {"_id": job["user_id"]}
            result = await orchestrator.run(
                objective=job["payload"].get("message", ""),
                user=user_doc,
                session_id=job["session_id"],
                surface=job["payload"].get("surface", "background"),
                local_execution=False,
            )
            await db["jobs"].update_one(
                {"job_id": job_id},
                {"$set": {"status": "done", "result": result, "updated_at": datetime.utcnow()}},
            )
        else:
            await db["jobs"].update_one(
                {"job_id": job_id},
                {"$set": {
                    "status": "error",
                    "error": f"Unknown job type '{job['type']}'",
                    "updated_at": datetime.utcnow(),
                }},
            )
        logger.info(f"Job {job_id} finished.")
    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}", exc_info=True)
        # Leave attempts incremented — if under MAX_ATTEMPTS it'll be
        # picked up again once it goes stale; if it's maxed out, mark it
        # dead so it stops getting retried forever.
        current = await db["jobs"].find_one({"job_id": job_id})
        final_status = "error" if (current and current.get("attempts", 0) >= MAX_ATTEMPTS) else "pending"
        await db["jobs"].update_one(
            {"job_id": job_id},
            {"$set": {"status": final_status, "error": str(e), "updated_at": datetime.utcnow()}},
        )

    return True


_loop_task = None
_kick_lock = asyncio.Lock()


async def _worker_loop(db, orchestrator, idle_interval: int = 5):
    """
    Runs while the process is alive. Simply stops existing when Render
    spins the service down — that's fine, jobs are safe in Mongo, and
    this restarts fresh (via start_worker_loop, called on app startup)
    whenever the service wakes back up.
    """
    logger.info("Job worker loop started.")
    while True:
        try:
            processed = await process_next_job(db, orchestrator)
        except Exception as e:
            logger.error(f"Job worker loop error (continuing): {e}")
            processed = False
        await asyncio.sleep(0.5 if processed else idle_interval)


def start_worker_loop(db, orchestrator) -> None:
    """Call once on app startup (see bootstrap.py). Idempotent."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    _loop_task = asyncio.create_task(_worker_loop(db, orchestrator))


async def kick_job_queue(db, orchestrator) -> None:
    """
    Fire-and-forget nudge — call this from any request path (e.g. a
    lightweight dependency on high-traffic endpoints) so real usage also
    advances the queue, not just the dedicated loop. Cheap: does nothing
    if the queue's empty, processes at most one job per kick so it never
    turns an unrelated request into a long-blocking one.
    """
    if _kick_lock.locked():
        return  # a kick or the main loop is already mid-job, skip
    async with _kick_lock:
        try:
            await process_next_job(db, orchestrator)
        except Exception as e:
            logger.warning(f"Job queue kick failed (non-fatal): {e}")
