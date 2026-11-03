"""A finished job must stay finished. A stale 'queued' copy in Mongo must not undo it."""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import remote_jobs as rj  # noqa: E402


class StaleDB:
    """Mongo that still holds the 'queued' copy written when the job was registered."""

    def __getitem__(self, name):
        return self

    async def find_one(self, query):
        return {"_id": query["_id"], "state": "queued", "user_id": "u1"}

    async def update_one(self, *args, **kwargs):
        return None


def test_a_stale_queued_copy_does_not_undo_a_finished_job():
    jobs = rj.RemoteJobs()
    jobs.configure(StaleDB())

    async def scenario():
        await jobs.register("j1", "u1", "github", "clone_to_workspace")
        await jobs.record_result("j1", {"job_id": "j1", "state": "done", "result": {"status": "success"}})
        await jobs._refresh_from_db("j1")  # a poll reads the stale copy
        return jobs.get("j1")["state"]

    assert asyncio.run(scenario()) == "done"


def test_wait_never_returns_a_job_that_is_not_finished():
    jobs = rj.RemoteJobs()
    jobs.configure(StaleDB())

    async def scenario():
        await jobs.register("j2", "u1", "github", "clone_to_workspace")
        jobs.forget("j2")  # no waiter on this instance any more
        return await jobs.wait("j2", timeout_s=0.5)

    assert asyncio.run(scenario()) is None
