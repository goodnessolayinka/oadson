import asyncio
import pathlib
import sys
import tempfile

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import approvals as ap  # noqa: E402
from core import worker_client as wc  # noqa: E402


# ---- approvals ----

def test_no_action_needs_approval_any_more():
    assert not ap.needs_approval("github", "push_workspace")
    assert not ap.needs_approval("github", "write_file")
    assert not ap.needs_approval("github", "create_repo")
    assert not ap.needs_approval("github", "search")
    assert not ap.needs_approval("calculator", "add")


def test_approval_flow_is_user_only_single_use_and_argument_bound():
    args = {"action": "push_workspace", "repo": "o/r", "message": "fix"}
    code = ap.request("alice", "github", "push_workspace", args)
    assert ap.consume("alice", "github", "push_workspace", args, code) is False     # not approved yet
    assert ap.approve("mallory", code) is False                                    # someone else's chat
    assert ap.approve("alice", code) is True
    changed = {**args, "message": "something else"}
    assert ap.consume("alice", "github", "push_workspace", changed, code) is False  # args changed
    assert ap.consume("alice", "github", "push_workspace", args, code) is True
    assert ap.consume("alice", "github", "push_workspace", args, code) is False     # single use


def test_approval_text_is_recognised_only_in_the_exact_form():
    assert ap.parse_approve("approve 1a2b3c4d") == "1a2b3c4d"
    assert ap.parse_approve("  APPROVE 1A2B3C4D  ") == "1a2b3c4d"
    assert ap.parse_approve("please approve 1a2b3c4d now") is None
    assert ap.parse_approve("approve xyz") is None


def test_approval_becomes_a_note_for_the_model_once():
    code = ap.request("bob", "github", "write_file", {"repo": "o/r"})
    ap.approve("bob", code)
    note = ap.pop_notice("bob")
    assert note and code in note and "exactly the same arguments" in note
    assert ap.pop_notice("bob") is None


# ---- routing ----

def test_terminal_needs_the_owner_flag(monkeypatch):
    monkeypatch.delenv("WORKER_ROUTE_TERMINAL", raising=False)
    assert wc.routable("terminal") is False
    monkeypatch.setenv("WORKER_ROUTE_TERMINAL", "true")
    assert wc.routable("terminal") is True
    assert wc.routable("not_a_skill") is False


@pytest.fixture
def worker_on(monkeypatch):
    monkeypatch.setenv("WORKER_ENABLED", "true")
    monkeypatch.setenv("WORKER_URLS", "https://worker.example")
    monkeypatch.setenv("INTERNAL_SECRET", "owner-cap-test-secret-value")


def _down(request):
    raise httpx.ConnectError("worker is down")


def test_heavy_skill_does_not_fall_back_when_the_worker_is_down(worker_on):
    res = asyncio.run(wc.try_remote("browser", {"user_id": "u9", "args": {"action": "navigate"}},
                                    transport=httpx.MockTransport(_down)))
    assert res["status"] == "error" and "offline" in res["reason"]


def test_cheap_skill_falls_back_to_running_here_when_the_worker_is_down(worker_on):
    res = asyncio.run(wc.try_remote("whois", {"user_id": "u9", "args": {"action": "lookup"}},
                                    transport=httpx.MockTransport(_down)))
    assert res is None


# ---- GitHub search_code (local, against a repo already in the workspace) ----

def test_search_code_finds_text_in_a_cloned_repo(monkeypatch):
    home = pathlib.Path(tempfile.mkdtemp())
    repo = home / "workspace" / "repos" / "acme__shop"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "refunds.py").write_text("def issue_refund(order):\n    return order\n")
    import skills.github.skill as gh
    import core.oadson_home as oh
    monkeypatch.setattr(oh, "get_user_home", lambda uid: home)
    res = asyncio.run(gh.run({"user_id": "u1", "args": {"action": "search_code",
                                                        "repo": "acme/shop", "query": "refund"}}))
    assert res["status"] == "success" and res["files_indexed"] == 1
    assert "issue_refund" in res["snippets"]


def test_search_code_says_so_when_the_repo_is_not_cloned(monkeypatch):
    import skills.github.skill as gh
    import core.oadson_home as oh
    monkeypatch.setattr(oh, "get_user_home", lambda uid: pathlib.Path(tempfile.mkdtemp()))
    res = asyncio.run(gh.run({"user_id": "u1", "args": {"action": "search_code",
                                                        "repo": "acme/none", "query": "x"}}))
    assert res["status"] == "error" and "clone" in res["explanation"].lower()
