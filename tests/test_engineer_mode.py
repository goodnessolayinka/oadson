"""Engineer mode: commands, read-only tools, budget gate, the search-then-answer loop, and path safety."""
import asyncio
import datetime as dt
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SPEND_LEDGER_PATH", os.path.join(tempfile.mkdtemp(), "ledger.json"))

from core import engineer_mode as em  # noqa: E402
from core import spend_tracker  # noqa: E402
from core import workspace_pointer as wp  # noqa: E402


class FakeColl:
    def __init__(self):
        self.docs = {}
        self.updates = []

    async def insert_one(self, doc):
        self.docs[doc["_id"]] = dict(doc)

    async def update_one(self, flt, upd, upsert=False):
        self.updates.append((flt, upd))
        doc = self.docs.setdefault(flt.get("_id"), {"_id": flt.get("_id")})
        doc.update(upd.get("$set", {}))

    async def find_one(self, flt):
        doc = self.docs.get(flt.get("_id"))
        return dict(doc) if doc else None


class FakeDB(dict):
    def __missing__(self, key):
        self[key] = FakeColl()
        return self[key]


def test_commands_are_recognised_exactly():
    assert em.parse_command("/engineer on") == ("mode", "on")
    assert em.parse_command("  /ENGINEER OFF ") == ("mode", "off")
    assert em.parse_command("report: the stop button does nothing") == ("report", "the stop button does nothing")
    assert em.parse_command("hello there") is None
    assert em.parse_command("/engineer maybe") is None


def test_mode_is_stored_per_chat_and_read_back():
    em._mode_cache.clear()
    db = FakeDB()
    asyncio.run(em.apply_mode_command(db, "s-mode", "u1", "on"))
    em._mode_cache.clear()                        # simulate a restart
    assert asyncio.run(em.is_on(db, "s-mode")) is True
    asyncio.run(em.apply_mode_command(db, "s-mode", "u1", "off"))
    em._mode_cache.clear()
    assert asyncio.run(em.is_on(db, "s-mode")) is False


def test_engineer_has_only_read_only_tools():
    names = {t["function"]["name"] for t in em._tools()}
    assert names == {"search_code", "read_file", "repo_map"} == set(em.READ_ONLY_TOOLS)


def test_write_actions_are_refused_even_if_the_model_asks():
    res = asyncio.run(em._run_tool("u1", "push_workspace", {"repo": "x"}, "upload:x"))
    assert res["status"] == "error" and "not available" in res["reason"]


def test_budget_stops_before_any_model_call(monkeypatch):
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    cap = spend_tracker.CAPS["engineer"]["daily"]
    saved = (dict(spend_tracker._mirror["total"]), dict(spend_tracker._mirror["daily"]))
    try:
        spend_tracker._mirror["daily"][today] = {"engineer": cap}
        monkeypatch.setattr(em, "call_claude", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
        res = asyncio.run(em.run_engineer("u1", "s1", "anything"))
        assert res["paused"] is True and "resets at midnight UTC" in res["reply"]
    finally:
        spend_tracker._mirror["total"].clear()
        spend_tracker._mirror["total"].update(saved[0])
        spend_tracker._mirror["daily"].clear()
        spend_tracker._mirror["daily"].update(saved[1])


def test_engineer_searches_on_the_worker_then_answers(monkeypatch):
    async def active(uid):
        return "upload:my-project"
    monkeypatch.setattr(wp, "get_active", active)
    calls, seen, recorded = [], [], []

    async def fake_claude(provider, model_id, messages, tools):
        calls.append(model_id)
        if len(calls) == 1:
            return {"tool_call": {"name": "search_code", "args": {"query": "stop button"}}, "text": None,
                    "raw_message": {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "t1", "type": "function",
                         "function": {"name": "search_code", "arguments": "{}"}}]},
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        return {"tool_call": None, "text": "What I think is wrong: the stop button is not wired.",
                "raw_message": {"role": "assistant", "content": "x"},
                "usage": {"prompt_tokens": 20, "completion_tokens": 30}}

    async def fake_worker(uid, action, args):
        seen.append((action, args))
        return {"status": "success", "snippets": "chat.html:12 stop button"}

    async def fake_record(provider, model, usage):
        recorded.append(provider)
        return 0.0

    monkeypatch.setattr(em, "call_claude", fake_claude)
    monkeypatch.setattr(em, "_worker_call", fake_worker)
    monkeypatch.setattr(spend_tracker, "record", fake_record)
    res = asyncio.run(em.run_engineer("u1", "s1", "stop button does nothing"))
    assert res["reply"].startswith("What I think is wrong") and res["paused"] is False
    assert seen == [("search_code", {"repo": "upload:my-project", "query": "stop button"})]
    assert calls == [em.MODEL, em.MODEL]                       # Haiku 5.5 only
    assert recorded == ["anthropic", "engineer", "anthropic", "engineer"]


def test_reports_are_saved_as_open():
    db = FakeDB()
    rid = asyncio.run(em.save_report(db, "u1", "s1", "the send button is hidden", None))
    assert db["engineer_reports"].docs[rid]["status"] == "open"


def test_images_only_come_from_the_upload_host():
    assert asyncio.run(em.fetch_image_data_url("http://res.cloudinary.com/x.png")) is None
    assert asyncio.run(em.fetch_image_data_url("https://evil.example/x.png")) is None


def test_the_turn_is_stored_in_the_normal_session_shape():
    db = FakeDB()
    asyncio.run(em.persist_pair(db, "s9", "u1", "report: broken", "What I think is wrong: x"))
    flt, upd = db["ai_sessions"].updates[-1]
    msgs = upd["$push"]["messages"]["$each"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["model_used"].startswith("engineer")


def test_read_file_stays_inside_the_folder_and_numbers_lines(monkeypatch):
    import core.oadson_home as oh
    import skills.github.skill as gh
    home = pathlib.Path(tempfile.mkdtemp())
    folder = home / "u2" / "workspace" / "uploads" / "proj"
    (folder / "app").mkdir(parents=True)
    (folder / "app" / "ui.js").write_text("one\ntwo\nthree\nfour\n")
    (home / "u2" / "secret.txt").write_text("do not read")
    monkeypatch.setattr(oh, "get_user_home", lambda uid: home / uid)
    bad = asyncio.run(gh.run({"user_id": "u2", "args": {"action": "read_file", "repo": "upload:proj",
                                                         "path": "../secret.txt"}}))
    assert bad["status"] == "error"
    good = asyncio.run(gh.run({"user_id": "u2", "args": {"action": "read_file", "repo": "upload:proj",
                                                          "path": "app/ui.js", "start_line": 2, "max_lines": 2}}))
    assert good["status"] == "success" and good["content"] == "2: two\n3: three"
