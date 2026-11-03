import asyncio
import hashlib
import json
import os
import pathlib
import sys

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api import worker_callback  # noqa: E402
from core import remote_jobs as rj  # noqa: E402
from core import worker_client as wc  # noqa: E402
from core.worker_contracts.signing import sign  # noqa: E402

SECRET = "owner-worker-test-secret-value"


@pytest.fixture(autouse=True)
def _link_env(monkeypatch):
    monkeypatch.setenv("WORKER_ENABLED", "true")
    monkeypatch.setenv("WORKER_URLS", "https://worker-a.example")
    monkeypatch.setenv("INTERNAL_SECRET", SECRET)
    yield


def _job_from(request: httpx.Request) -> dict:
    return json.loads(request.content)


def _worker(answer=None, status=202, capture=None):
    """A fake worker. For an accepted job it sends the callback to the owner's store itself."""
    def handler(request: httpx.Request) -> httpx.Response:
        job = _job_from(request)
        if capture is not None:
            capture.append(job)
        if status != 202:
            return httpx.Response(status, text="refused")
        if answer:
            payload = {"job_id": job["job_id"], "state": answer,
                       "result": {"status": "success", "value": 42} if answer == "done" else None,
                       "error": None if answer == "done" else "boom",
                       "explanation": None if answer == "done" else "explained"}
            asyncio.get_running_loop().create_task(rj.jobs.record_result(job["job_id"], payload))
        return httpx.Response(202, json={"job_id": job["job_id"], "state": "queued"})
    return handler


def _run(skill="calculator", handler=None, wait_s=5):
    payload = {"user_id": "u1", "args": {"action": "add", "a": 1, "b": 2}}
    return asyncio.run(wc.try_remote(skill, payload,
                                     transport=httpx.MockTransport(handler), wait_s=wait_s))


# ---- pinned contracts: the owner's copy must be the worker's copy ----

def test_contracts_match_the_pinned_hashes():
    pins = json.loads((ROOT / "core/worker_contracts/PINNED.json").read_text())["sha256"]
    for rel, expected in pins.items():
        path = ROOT / "core/worker_contracts" / rel if not rel.startswith("..") else ROOT / "core" / rel[3:]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, f"{rel} changed; re-copy from the worker"


# ---- routing ----

def test_disabled_or_not_allowlisted_runs_locally(monkeypatch):
    monkeypatch.setenv("WORKER_ENABLED", "false")
    assert _run(handler=_worker(answer="done")) is None
    monkeypatch.setenv("WORKER_ENABLED", "true")
    assert _run(skill="terminal", handler=_worker(answer="done")) is None   # not on the allowlist


def test_one_user_always_reaches_the_same_worker():
    urls = ["https://a.example", "https://b.example", "https://c.example"]
    picks = {wc.choose_worker("user-42", "calculator", urls) for _ in range(20)}
    assert len(picks) == 1
    spread = {wc.choose_worker(f"user-{i}", "calculator", urls) for i in range(60)}
    assert spread == set(urls)


# ---- outcomes ----

def test_done_comes_back_as_the_skill_result():
    seen = []
    res = _run(handler=_worker(answer="done", capture=seen))
    assert res == {"status": "success", "value": 42}
    assert seen[0]["skill"] == "calculator" and seen[0]["action"] == "add"
    assert seen[0]["args"] == {"a": 1, "b": 2}           # action was moved out of args


def test_failed_comes_back_as_an_error_with_the_explanation():
    res = _run(handler=_worker(answer="failed"))
    assert res["status"] == "error" and res["reason"] == "boom" and res["explanation"] == "explained"


def test_blocked_comes_back_as_blocked():
    res = _run(handler=_worker(answer="blocked"))
    assert res["status"] == "blocked"


def test_unreachable_worker_falls_back_to_running_here():
    def down(request):
        raise httpx.ConnectError("worker is down")
    assert _run(handler=down) is None


def test_a_refused_job_is_an_error_not_a_silent_fallback():
    res = _run(handler=_worker(status=401))
    assert res["status"] == "error" and "INTERNAL_SECRET" in res["explanation"]


def test_no_answer_in_time_is_an_error_and_nothing_is_rerun():
    res = _run(handler=_worker(answer=None), wait_s=0.2)
    assert res["status"] == "error" and "did not send a result" in res["reason"]
    assert "Nothing was re-run" in res["explanation"]


def test_missing_secret_is_reported_not_run_silently(monkeypatch):
    monkeypatch.delenv("INTERNAL_SECRET")
    res = _run(handler=_worker(answer="done"))
    assert res["status"] == "error" and "INTERNAL_SECRET" in res["reason"]


# ---- the signed callback endpoint ----

def _callback_client():
    app = FastAPI()
    app.include_router(worker_callback.router)
    return TestClient(app)


def _signed_callback(client, job_id: str, payload: dict, secret: str = SECRET):
    path = f"/internal/v1/jobs/{job_id}/result"
    body = json.dumps(payload).encode()
    return client.post(path, content=body, headers={**sign(secret, "POST", path, body),
                                                    "Content-Type": "application/json"})


def test_signed_callback_is_recorded_and_wakes_the_waiter():
    client = _callback_client()
    payload = {"job_id": "cb1", "state": "done", "result": {"status": "success"},
               "error": None, "explanation": None, "artifacts": []}
    r = _signed_callback(client, "cb1", payload)
    assert r.status_code == 200
    assert rj.jobs.get("cb1")["state"] == "done"


def test_unsigned_or_wrong_secret_callbacks_are_refused():
    client = _callback_client()
    payload = {"job_id": "cb2", "state": "done"}
    assert client.post("/internal/v1/jobs/cb2/result", json=payload).status_code == 401
    assert _signed_callback(client, "cb2", payload, secret="wrong-secret-value").status_code == 401


def test_callback_with_mismatched_or_non_final_state_is_refused():
    client = _callback_client()
    assert _signed_callback(client, "cb3", {"job_id": "other", "state": "done"}).status_code == 422
    assert _signed_callback(client, "cb3", {"job_id": "cb3", "state": "queued"}).status_code == 422


def test_repeated_callback_is_harmless():
    client = _callback_client()
    payload = {"job_id": "cb4", "state": "done", "result": {"status": "success"}}
    assert _signed_callback(client, "cb4", payload).status_code == 200
    assert _signed_callback(client, "cb4", payload).status_code == 200
    assert rj.jobs.get("cb4")["state"] == "done"


# ---- terminal fallback: the owner runs it when the worker cannot ----

def test_terminal_falls_back_to_the_owner_when_the_worker_is_down(monkeypatch):
    monkeypatch.setenv("WORKER_ROUTE_TERMINAL", "true")
    def down(request):
        raise httpx.ConnectError("worker is down")
    assert _run(skill="terminal", handler=down) is None


def test_terminal_falls_back_when_the_worker_has_it_turned_off(monkeypatch):
    monkeypatch.setenv("WORKER_ROUTE_TERMINAL", "true")
    def off(request):
        job = _job_from(request)
        payload = {"job_id": job["job_id"], "state": "blocked", "result": None,
                   "error": "skill 'terminal' is not enabled on this worker", "explanation": "x"}
        asyncio.get_running_loop().create_task(rj.jobs.record_result(job["job_id"], payload))
        return httpx.Response(202, json={"job_id": job["job_id"], "state": "queued"})
    assert _run(skill="terminal", handler=off) is None


def test_github_still_does_not_fall_back(monkeypatch):
    def down(request):
        raise httpx.ConnectError("worker is down")
    res = _run(skill="github", handler=down)
    assert res["status"] == "error" and "not reachable" in res["explanation"]
