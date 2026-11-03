"""
Owner side of the worker link. try_remote() runs before every skill.

Returns None when the skill should run here, as before. Returns a result in the
same shape the skill executor returns when the job ran on a worker.

Rules, in order:
- WORKER_ENABLED is not "true", WORKER_URLS is empty, or the skill is not on the
  worker allowlist: run here.
- Worker unreachable (network error or 5xx): run here. Allowlisted skills are
  cheap and stateless, so this is safe.
- Worker refuses the request (401, 409, 422): NOT a fallback. It is a
  configuration problem, so the result says so and names the fix.
- Worker accepted: wait for its signed callback, then return the outcome.
"""
import hashlib
import logging
import json
import os
import uuid

import httpx

from core import remote_jobs as _rj
from core.worker_allowlist import ALLOWED_SKILLS, NO_FALLBACK

logger = logging.getLogger("oadson.worker_client")

# Skills that run on the owner when the worker cannot run them (unreachable,
# or terminal turned off there). Every fallback is logged.
FALLBACK_OK = {"terminal"}

# When the worker says it is low on memory (503), workspace commands run here too,
# so the user is not blocked while the worker restarts. Unreachable is different:
# github still says "try again", as before.
# After a worker is unreachable, skip it for this long, then try it again.
WORKER_COOLDOWN_S = 60
_DOWN_UNTIL: dict = {}  # url -> monotonic time the cooldown ends


def _is_down(url: str) -> bool:
    import time as _t
    return _DOWN_UNTIL.get(url, 0.0) > _t.monotonic()


def _mark_down(url: str) -> None:
    import time as _t
    _DOWN_UNTIL[url] = _t.monotonic() + WORKER_COOLDOWN_S


from core.worker_contracts.signing import sign

JOB_TIMEOUT_S = 120
PATH = "/internal/v1/jobs"


def enabled() -> bool:
    return os.getenv("WORKER_ENABLED", "false").strip().lower() == "true"


def worker_urls() -> list:
    return [u.strip().rstrip("/") for u in os.getenv("WORKER_URLS", "").split(",") if u.strip()]


def routable(skill: str) -> bool:
    if skill in ALLOWED_SKILLS:
        return True
    if skill == "terminal":  # off unless the owner and the worker both opt in
        return os.getenv("WORKER_ROUTE_TERMINAL", "false").strip().lower() == "true"
    return False


def choose_worker(user_id: str, skill: str, urls: list | None = None):
    """The same user always reaches the same worker, so their workspace stays in one place."""
    urls = worker_urls() if urls is None else urls
    if not urls or not routable(skill):
        return None
    h = int(hashlib.sha256(str(user_id).encode()).hexdigest(), 16)
    return urls[h % len(urls)]


def _offline(skill: str):
    if skill in FALLBACK_OK:
        logger.warning("%s: worker not reachable, running on the owner", skill)
        return None
    if skill in NO_FALLBACK:
        return {"status": "error", "reason": "the worker is offline",
                "explanation": f"{skill} runs on the worker, which is not reachable right now. "
                               "Try again in a minute."}
    return None


def _to_skill_result(rec: dict) -> dict:
    state = rec.get("state")
    if state == "done":
        res = rec.get("result")
        return res if isinstance(res, dict) and res else {"status": "success"}
    if state == "blocked":
        return {"status": "blocked", "reason": rec.get("error") or "blocked",
                "explanation": rec.get("explanation")}
    return {"status": "error", "reason": rec.get("error") or state,
            "explanation": rec.get("explanation")}


async def try_remote(skill: str, payload: dict, transport=None, wait_s: float | None = None):
    if not enabled():
        return None
    user_id = str(payload.get("user_id") or "")
    args = dict(payload.get("args") or {})
    action = str(args.pop("action", "") or "run")
    url = choose_worker(user_id, skill)
    if url is None:
        return None
    if _is_down(url):
        return _offline(skill)  # still in cooldown: cheap skills run here, others say try again

    secret = os.getenv("INTERNAL_SECRET", "")
    if not secret:
        return {"status": "error",
                "reason": "worker link is misconfigured: INTERNAL_SECRET is not set",
                "explanation": "Set INTERNAL_SECRET on the owner to the same value as on the worker."}

    job_id = uuid.uuid4().hex
    job = {"job_id": job_id, "idempotency_key": uuid.uuid4().hex, "user_id": user_id,
           "skill": skill, "action": action, "args": args, "timeout_s": JOB_TIMEOUT_S}
    body = json.dumps(job).encode()
    headers = {**sign(secret, "POST", PATH, body), "Content-Type": "application/json"}

    await _rj.jobs.register(job_id, user_id, skill, action, worker_url=url)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=15) as client:
            r = await client.post(url + PATH, content=body, headers=headers)
    except httpx.HTTPError:
        _rj.jobs.discard(job_id)
        _mark_down(url)
        try:
            from core import worker_restart
            await worker_restart.note_unreachable()
        except Exception as e:  # a restart problem must never break the user's request
            logger.warning("worker restart request failed: %s", e)
        return _offline(skill)  # unreachable: cheap skills run here, heavy ones say "try again"

    from core import worker_restart as _wr
    _wr.note_reachable()
    if r.status_code in (401, 409, 422):
        _rj.jobs.discard(job_id)
        return {"status": "error",
                "reason": f"worker refused the job ({r.status_code}): {r.text[:200]}",
                "explanation": "The worker rejected the request. For 401, check that "
                               "INTERNAL_SECRET is identical on both services."}
    if r.status_code == 503:
        # Low on memory: the worker refused this heavy skill. The worker itself is fine,
        # so it is NOT put in cooldown, and this job runs here.
        _rj.jobs.discard(job_id)
        logger.warning("%s: worker is low on memory, running on the owner", skill)
        return None
    if r.status_code != 202:
        _rj.jobs.discard(job_id)
        _mark_down(url)
        return _offline(skill)  # worker not ready

    rec = await _rj.jobs.wait(job_id, timeout_s=wait_s if wait_s is not None else JOB_TIMEOUT_S + 30)
    _rj.jobs.forget(job_id)
    if rec is None:
        return {"status": "error", "reason": "the worker did not send a result in time",
                "explanation": "The job may still be running on the worker. Nothing was re-run."}
    res = _to_skill_result(rec)
    if skill in FALLBACK_OK and res.get("status") == "blocked" \
            and "not enabled on this worker" in str(res.get("reason") or ""):
        logger.warning("%s: worker has it turned off (%s), running on the owner", skill, res.get("reason"))
        return None
    return res


def _worker_for_user(user_id: str, urls: list | None = None):
    urls = worker_urls() if urls is None else urls
    if not urls:
        return None
    h = int(hashlib.sha256(str(user_id).encode()).hexdigest(), 16)
    return urls[h % len(urls)]


async def submit_plan(user_id: str, steps: list, approved_by_user: bool = False,
                      max_hours: float = 12.0, transport=None) -> dict:
    """Start an approved plan on the worker. Plans never fall back to Render."""
    if not enabled():
        return {"status": "error", "reason": "the worker is not enabled, so plans cannot run",
                "explanation": "Turn on WORKER_ENABLED and set WORKER_URLS on Render."}
    url = _worker_for_user(str(user_id))
    secret = os.getenv("INTERNAL_SECRET", "")
    if not url or not secret:
        return {"status": "error", "reason": "the worker link is not configured",
                "explanation": "Set WORKER_URLS and INTERNAL_SECRET on Render."}
    plan_id = uuid.uuid4().hex
    plan = {"plan_id": plan_id, "idempotency_key": uuid.uuid4().hex, "user_id": str(user_id),
            "steps": steps, "max_hours": float(max_hours), "approved_by_user": bool(approved_by_user)}
    body = json.dumps(plan).encode()
    path = "/internal/v1/plans"
    headers = {**sign(secret, "POST", path, body), "Content-Type": "application/json"}
    await _rj.jobs.register(plan_id, str(user_id), "plan", "run", worker_url=url, kind="plan")
    try:
        async with httpx.AsyncClient(transport=transport, timeout=15) as client:
            r = await client.post(url + path, content=body, headers=headers)
    except httpx.HTTPError:
        _rj.jobs.discard(plan_id)
        return {"status": "error", "reason": "the worker is offline, so the plan did not start",
                "explanation": "Try again in a few minutes. Plans never run on Render."}
    if r.status_code == 202:
        return {"status": "started", "plan_id": plan_id, "state": "running",
                "explanation": "The plan runs on the worker and saves a checkpoint after every step. "
                               "You will get a notification when it finishes or needs a decision."}
    _rj.jobs.discard(plan_id)
    return {"status": "error", "reason": f"the worker refused the plan ({r.status_code}): {r.text[:200]}",
            "explanation": "Check the steps. Plans that change something outside the sandbox need approval."}


async def cancel_user_jobs(user_id: str, transport=None) -> int:
    """Stop button: ask each of this user's unfinished worker jobs to cancel. Returns how many accepted."""
    secret = os.getenv("INTERNAL_SECRET", "")
    jobs_ = _rj.jobs.active_for_user(str(user_id))
    if not secret or not jobs_:
        return 0
    cancelled = 0
    async with httpx.AsyncClient(transport=transport, timeout=10) as client:
        for rec in jobs_:
            if rec.get("kind") == "plan":
                path = f"/internal/v1/plans/{rec['job_id']}/cancel"
            else:
                path = f"/internal/v1/jobs/{rec['job_id']}/cancel"
            try:
                r = await client.post(rec["worker_url"] + path, content=b"",
                                      headers=sign(secret, "POST", path, b""))
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and r.json().get("status") == "cancelling":
                cancelled += 1
    return cancelled
