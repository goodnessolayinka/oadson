"""
Owner side of the worker's low-memory and outage handling.
No network: httpx.MockTransport stands in for the worker and for Render.
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("SPEND_LEDGER_PATH", os.path.join(tempfile.mkdtemp(), "ledger.json"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from core import worker_client as wc  # noqa: E402
from core import worker_restart  # noqa: E402

URL = "https://worker.example"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WORKER_ENABLED", "true")
    monkeypatch.setenv("WORKER_URLS", URL)
    monkeypatch.setenv("INTERNAL_SECRET", "routing-test-secret-value")
    monkeypatch.setenv("WORKER_ROUTE_TERMINAL", "true")
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    monkeypatch.delenv("RENDER_WORKER_SERVICE_ID", raising=False)


def _run(skill, transport, action="run"):
    return asyncio.run(wc.try_remote(skill, {"user_id": "u1", "args": {"action": action}},
                                     transport=transport, wait_s=1))


def _low_memory_worker(hits: list):
    def handler(request):
        hits.append(request.url.path)
        return httpx.Response(503, json={"status": "unavailable", "route": "owner",
                                         "reason": "worker is low on memory (30 MB free)"})
    return httpx.MockTransport(handler)


def _down_worker(hits: list):
    def handler(request):
        hits.append(request.url.path)
        raise httpx.ConnectError("worker is down")
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("skill,action", [("browser", "navigate"), ("github", "clone_to_workspace"),
                                          ("terminal", "run")])
def test_a_heavy_job_refused_for_memory_runs_on_the_owner(skill, action):
    assert _run(skill, _low_memory_worker([]), action=action) is None


def test_a_memory_refusal_does_not_put_the_worker_in_cooldown():
    hits = []
    t = _low_memory_worker(hits)
    _run("browser", t, action="navigate")
    _run("browser", t, action="navigate")
    assert len(hits) == 2  # the worker is still tried; only the heavy job moved


def test_an_unreachable_worker_keeps_the_old_rule_for_github():
    res = _run("github", _down_worker([]), action="list_files")
    assert res["status"] == "error" and "not reachable" in res["explanation"]


def test_after_an_unreachable_worker_the_next_calls_skip_it_during_cooldown():
    hits = []
    t = _down_worker(hits)
    _run("github", t, action="list_files")
    _run("github", t, action="list_files")
    assert len(hits) == 1


def test_render_is_asked_to_restart_once_per_outage(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "rnd_test")
    monkeypatch.setenv("RENDER_WORKER_SERVICE_ID", "srv-test")
    seen = []

    def render(request):
        seen.append(str(request.url))
        return httpx.Response(202)

    rt = httpx.MockTransport(render)
    assert asyncio.run(worker_restart.note_unreachable(transport=rt)) is True
    assert asyncio.run(worker_restart.note_unreachable(transport=rt)) is False  # same outage
    assert seen == ["https://api.render.com/v1/services/srv-test/restart"]


def test_a_new_outage_after_the_worker_answers_can_ask_again(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "rnd_test")
    monkeypatch.setenv("RENDER_WORKER_SERVICE_ID", "srv-test")
    seen = []

    def render(request):
        seen.append(1)
        return httpx.Response(202)

    rt = httpx.MockTransport(render)
    asyncio.run(worker_restart.note_unreachable(transport=rt))
    worker_restart.note_reachable()
    asyncio.run(worker_restart.note_unreachable(transport=rt))
    assert len(seen) == 2


def test_no_restart_is_asked_without_render_settings():
    assert asyncio.run(worker_restart.note_unreachable()) is False


def test_a_low_memory_answer_is_not_treated_as_an_outage(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "rnd_test")
    monkeypatch.setenv("RENDER_WORKER_SERVICE_ID", "srv-test")
    asked = []

    async def spy(*a, **k):
        asked.append(1)
        return True

    monkeypatch.setattr(worker_restart, "note_unreachable", spy)
    _run("browser", _low_memory_worker([]), action="navigate")
    assert asked == []
