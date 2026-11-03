import asyncio
import os
import pathlib
import sys

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import remote_jobs as rj  # noqa: E402
from core import worker_client as wc  # noqa: E402
from core.worker_contracts.signing import SIG_HEADER, TS_HEADER, verify  # noqa: E402

SECRET = "stop-test-secret-value"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_SECRET", SECRET)


def test_stop_cancels_only_this_users_unfinished_remote_jobs():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        ok, _ = verify(SECRET, "POST", request.url.path, request.content,
                       request.headers.get(TS_HEADER), request.headers.get(SIG_HEADER))
        seen.append((request.url.path, ok))
        return httpx.Response(200, json={"job_id": "x", "status": "cancelling"})

    async def scenario():
        await rj.jobs.register("st-1", "stop-user-1", "calculator", "add", worker_url="https://w.example")
        await rj.jobs.register("st-2", "stop-user-1", "whois", "lookup", worker_url="https://w.example")
        await rj.jobs.record_result("st-2", {"job_id": "st-2", "state": "done",
                                             "result": {"status": "success"}})
        await rj.jobs.register("st-3", "stop-other-user", "calculator", "add", worker_url="https://w.example")
        return await wc.cancel_user_jobs("stop-user-1", transport=httpx.MockTransport(handler))

    assert asyncio.run(scenario()) == 1
    assert seen == [("/internal/v1/jobs/st-1/cancel", True)]   # signed, and only the unfinished one


def test_no_secret_means_nothing_is_sent(monkeypatch):
    monkeypatch.delenv("INTERNAL_SECRET")
    rj.jobs._mem.pop("st-9", None)
    asyncio.run(rj.jobs.register("st-9", "stop-user-9", "calculator", "add", worker_url="https://w.example"))
    assert asyncio.run(wc.cancel_user_jobs("stop-user-9")) == 0
