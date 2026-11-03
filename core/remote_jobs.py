"""
Owner-side record of jobs and plans sent to workers, and the waiting that makes a
remote call look like a local one to the tool loop.

Several owner instances can run at once. The waiter registers an event on its own
instance, but the callback may land on any instance, so the waiter also polls the
shared Mongo record (remote_jobs). Whichever instance receives the result writes
it to Mongo, and the waiter sees it either way.
"""
import asyncio
import time

TERMINAL = {"done", "failed", "blocked", "cancelled", "interrupted", "paused"}
COLLECTION = "remote_jobs"
KEEP_S = 86400
POLL_S = 1.0


class RemoteJobs:
    def __init__(self):
        self.db = None
        self._mem: dict = {}
        self._events: dict = {}

    def configure(self, db) -> None:
        self.db = db

    async def _save(self, job_id: str, fields: dict) -> None:
        rec = self._mem.setdefault(job_id, {"job_id": job_id})
        rec.update(fields, updated=time.time())
        if self.db is not None:
            try:
                await self.db[COLLECTION].update_one({"_id": job_id}, {"$set": fields}, upsert=True)
            except Exception:
                pass  # the in-memory copy is kept; the next save writes again

    async def _refresh_from_db(self, job_id: str):
        if self.db is None:
            return None
        try:
            doc = await self.db[COLLECTION].find_one({"_id": job_id})
        except Exception:
            return None
        if doc:
            doc = dict(doc)
            doc.pop("_id", None)
            mem = self._mem.setdefault(job_id, {"job_id": job_id})
            if mem.get("state") in TERMINAL and doc.get("state") not in TERMINAL:
                return mem  # a finished job never goes back to queued (stale Mongo copy)
            mem.update(doc)
        return self._mem.get(job_id)

    def _prune(self) -> None:
        cutoff = time.time() - KEEP_S
        for job_id in [j for j, r in self._mem.items() if r.get("updated", 0) < cutoff]:
            self._mem.pop(job_id, None)

    async def register(self, job_id: str, user_id: str, skill: str, action: str,
                       worker_url: str | None = None, kind: str = "job") -> None:
        self._prune()
        self._events[job_id] = asyncio.Event()
        await self._save(job_id, {"user_id": user_id, "skill": skill, "action": action,
                                  "state": "queued", "created": time.time(),
                                  "worker_url": worker_url, "kind": kind})

    async def record_progress(self, job_id: str, payload: dict) -> None:
        await self._save(job_id, {"state": "running", "progress": payload})

    async def record_result(self, job_id: str, payload: dict) -> bool:
        state = payload.get("state")
        if state not in TERMINAL:
            raise ValueError(f"'{state}' is not a final state")
        await self._save(job_id, {
            "state": state,
            "result": payload.get("result"),
            "error": payload.get("error"),
            "explanation": payload.get("explanation"),
            "artifacts": payload.get("artifacts") or [],
        })
        event = self._events.get(job_id)
        if event is not None:
            event.set()
        return True

    def get(self, job_id: str):
        return self._mem.get(job_id)

    async def wait(self, job_id: str, timeout_s: float):
        """Returns the job's record only once it is finished. Never 'queued' or 'running'."""
        event = self._events.get(job_id)
        if event is None:
            rec = await self._refresh_from_db(job_id) or self._mem.get(job_id)
            return rec if rec and rec.get("state") in TERMINAL else None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                await asyncio.wait_for(event.wait(), timeout=min(POLL_S, max(0.01, deadline - time.time())))
                rec = self._mem.get(job_id)
                if rec and rec.get("state") in TERMINAL:
                    return rec
                event.clear()  # set but not finished: keep waiting, do not spin
            except asyncio.TimeoutError:
                rec = await self._refresh_from_db(job_id)
                if rec and rec.get("state") in TERMINAL:
                    return rec
        return None

    def discard(self, job_id: str) -> None:
        self._mem.pop(job_id, None)
        self._events.pop(job_id, None)

    def active_for_user(self, user_id: str) -> list:
        return [r for r in self._mem.values()
                if r.get("user_id") == user_id and r.get("worker_url")
                and r.get("state") not in TERMINAL]

    def forget(self, job_id: str) -> None:
        self._events.pop(job_id, None)


jobs = RemoteJobs()
