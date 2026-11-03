"""Plans from the owner, cancel by plan, plan progress, secret rotation, shared-instance waiting."""
import asyncio
import base64
import io
import json
import pathlib
import sys
import tempfile
import zipfile

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api import worker_callback  # noqa: E402
from core import approvals as ap  # noqa: E402
from core import remote_jobs as rjmod  # noqa: E402
from core import worker_client as wc  # noqa: E402
from core import workspace_pointer as wp  # noqa: E402
from core.worker_contracts.signing import sign, verify  # noqa: E402

SECRET = "owner-v2-secret-value-long"


def setup_module(_):
    import os
    os.environ.update({"WORKER_ENABLED": "true", "WORKER_URLS": "http://127.0.0.1:9",
                       "INTERNAL_SECRET": SECRET})
    os.environ.pop("INTERNAL_SECRET_NEXT", None)


class FakeColl:
    def __init__(self):
        self.docs = {}

    async def update_one(self, flt, upd, upsert=False):
        doc = self.docs.setdefault(flt["_id"], {"_id": flt["_id"]})
        doc.update(upd.get("$set", {}))

    async def find_one(self, flt):
        doc = self.docs.get(flt["_id"])
        return dict(doc) if doc else None


class FakeDB(dict):
    def __missing__(self, key):
        self[key] = FakeColl()
        return self[key]


def _client():
    app = FastAPI()
    app.include_router(worker_callback.router)
    return TestClient(app)


def _signed(client, path, obj, secret=SECRET):
    body = json.dumps(obj).encode()
    return client.post(path, content=body, headers={**sign(secret, "POST", path, body),
                                                    "Content-Type": "application/json"})


def test_submit_plan_sends_a_signed_plan_and_records_it():
    seen = {}

    def handler(req):
        body = json.loads(req.content)
        seen["path"] = req.url.path
        seen["ok"] = verify(SECRET, "POST", req.url.path, req.content,
                            req.headers["X-Oadson-Timestamp"], req.headers["X-Oadson-Signature"])[0]
        seen["plan"] = body
        return httpx.Response(202, json={"plan_id": body["plan_id"], "state": "running"})

    res = asyncio.run(wc.submit_plan("u1", [{"skill": "echo", "action": "say", "args": {}}],
                                     False, 2.0, transport=httpx.MockTransport(handler)))
    assert res["status"] == "started" and seen["path"] == "/internal/v1/plans" and seen["ok"]
    assert seen["plan"]["max_hours"] == 2.0
    assert rjmod.jobs.get(res["plan_id"])["kind"] == "plan"


def test_stop_cancels_a_plan_on_its_own_path():
    async def scenario():
        await rjmod.jobs.register("pl-cancel-1", "stop-plan-user", "plan", "run",
                                  worker_url="https://w.example", kind="plan")
        seen = []

        def handler(req):
            seen.append(req.url.path)
            return httpx.Response(200, json={"status": "cancelling"})

        n = await wc.cancel_user_jobs("stop-plan-user", transport=httpx.MockTransport(handler))
        return n, seen

    n, seen = asyncio.run(scenario())
    assert n == 1 and seen == ["/internal/v1/plans/pl-cancel-1/cancel"]


def test_plan_progress_is_recorded_when_signed():
    client = _client()
    r = _signed(client, "/internal/v1/plans/pg1/progress", {"plan_id": "pg1", "state": "running", "step": 1})
    assert r.status_code == 200 and rjmod.jobs.get("pg1")["progress"]["step"] == 1
    bad = client.post("/internal/v1/plans/pg1/progress", json={"plan_id": "pg1"})
    assert bad.status_code == 401


def test_callbacks_signed_with_the_next_secret_are_accepted_during_rotation(monkeypatch):
    monkeypatch.setenv("INTERNAL_SECRET_NEXT", "next-secret-value-long-enough")
    client = _client()
    ok = _signed(client, "/internal/v1/jobs/rot1/result", {"job_id": "rot1", "state": "done"},
                 secret="next-secret-value-long-enough")
    assert ok.status_code == 200
    rogue = _signed(client, "/internal/v1/jobs/rot2/result", {"job_id": "rot2", "state": "done"},
                    secret="random-attacker-secret")
    assert rogue.status_code == 401
    monkeypatch.delenv("INTERNAL_SECRET_NEXT")


def test_a_waiter_sees_a_result_recorded_by_another_instance():
    db = FakeDB()
    a, b = rjmod.RemoteJobs(), rjmod.RemoteJobs()
    a.configure(db)
    b.configure(db)

    async def scenario():
        await a.register("mi-1", "u1", "calculator", "add", worker_url="https://w.example")

        async def answer_later():
            await asyncio.sleep(1.5)
            await b.record_result("mi-1", {"job_id": "mi-1", "state": "done",
                                           "result": {"status": "success", "value": 7}})

        t = asyncio.create_task(answer_later())
        rec = await a.wait("mi-1", timeout_s=10)
        await t
        return rec

    rec = asyncio.run(scenario())
    assert rec["state"] == "done" and rec["result"]["value"] == 7


def test_workspace_pointer_survives_a_restart_through_the_db():
    db = FakeDB()
    rjmod.jobs.configure(db)
    try:
        asyncio.run(wp.set_active("ptr-user", "upload:shop.zip"))
        wp._mem.clear()
        assert asyncio.run(wp.get_active("ptr-user")) == "upload:shop.zip"
    finally:
        rjmod.jobs.configure(None)


def test_a_risky_plan_is_not_held_for_approval():
    import skills.plans.skill as plans
    steps = [{"skill": "github", "action": "push_workspace", "args": {"repo": "o/r"}}]
    res = asyncio.run(plans.run({"user_id": "u9", "args": {"action": "submit", "steps": steps,
                                                            "max_hours": 2}}))
    assert res["status"] != "needs_approval"           # approval was removed on purpose


def test_owner_can_also_index_an_archive_locally(monkeypatch):
    import skills.github.skill as gh
    import core.oadson_home as oh
    home = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(oh, "get_user_home", lambda uid: home / uid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("svc/billing.py", "def charge_card(c):\n    return c\n")
    res = asyncio.run(gh.run({"user_id": "own1", "args": {
        "action": "index_archive", "name": "svc.zip",
        "archive_b64": base64.b64encode(buf.getvalue()).decode()}}))
    assert res["status"] == "success"
    found = asyncio.run(gh.run({"user_id": "own1", "args": {
        "action": "search_code", "repo": "upload:svc.zip", "query": "charge"}}))
    assert "charge_card" in found["snippets"]
