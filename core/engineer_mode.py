"""
Engineer mode: a read-only diagnostic mode inside OADSON.

Turn it on with `/engineer on` (it stays on for this chat until `/engineer off`), or
send one message starting with `report:` to ask about a problem once. Attach a
screenshot if it helps. Engineer reads the project's code on the worker and replies
in the same chat with what it thinks is wrong and a proposed change written as text.
It changes nothing: it cannot commit, push, merge, deploy, or touch personal data.

Worker in mind: every code read (search, map, read_file) runs on the worker. If the
worker is offline, Engineer says so. It never reads the repo on Render as a fallback.

Model: Claude Haiku 5.5 only. Its spend has its own cap (ENGINEER_DAILY_CAP_USD and
ENGINEER_TOTAL_CAP_USD), and it is also counted in the normal Anthropic totals.
"""
import base64
import datetime as _dt
import json
import os
import re
import uuid
from urllib.parse import urlparse

import httpx

from core import spend_tracker, worker_client, workspace_pointer
from core.anthropic_native import ClaudeQuota, call_claude

MODEL = "claude-haiku-5-5"
MAX_STEPS = 6
TOOL_RESULT_CHARS = 6000
IMAGE_MAX_BYTES = 4 * 1024 * 1024
IMAGE_HOSTS = ("res.cloudinary.com",)
RESERVE_USD = 0.05
READ_ONLY_TOOLS = ("search_code", "read_file", "repo_map")
COLLECTION_SESSIONS = "engineer_sessions"
COLLECTION_REPORTS = "engineer_reports"
PAUSED_MSG = ("Engineer is paused: today's Engineer budget is used up, and it resets at midnight UTC. "
              "Your report is saved.")
DISABLED_MSG = "Engineer mode is switched off on this server."

_mode_cache: dict = {}
_ON = re.compile(r"^\s*/engineer\s+(on|off|status)\s*$", re.IGNORECASE)
_REPORT = re.compile(r"^\s*report:\s*(.+)$", re.IGNORECASE | re.DOTALL)

SYSTEM = """You are Engineer, a diagnostic mode inside OADSON. The owner of OADSON is the person you are talking to. They see a problem, tell you about it, and decide what happens next. Your job is to work out what is wrong from their report and the code, and to explain it clearly.

Rules:
- You cannot change anything. Your tools are read-only: search_code, read_file, and repo_map. They search and read the project folder on the worker.
- Cite only file paths and line numbers that a tool returned. Never invent names.
- Say what you checked and what you could not check. You cannot run the app, the tests, or the website.
- If a screenshot is attached, describe what in it matters to the problem.
- If no project folder is active, say so, and ask the owner to upload the project zip.

Reply in plain sentences with these labels, in this order:
What I think is wrong: one to three sentences.
Where: file paths and line ranges from the tools.
Proposed change: a short diff as text, or a description if a diff is not needed. Do not apply it.
How to check it: steps the owner can take to confirm the fix.
Not verified: what you could not check."""


def parse_command(text):
    text = text or ""
    m = _ON.match(text)
    if m:
        return ("mode", m.group(1).lower())
    m = _REPORT.match(text)
    if m:
        return ("report", m.group(1).strip())
    return None


def enabled() -> bool:
    return os.getenv("ENGINEER_ENABLED", "true").strip().lower() == "true"


async def is_on(db, session_id: str) -> bool:
    if session_id in _mode_cache:
        return _mode_cache[session_id]
    if db is None:
        return False
    try:
        doc = await db[COLLECTION_SESSIONS].find_one({"_id": session_id})
    except Exception:
        return False
    on = bool(doc and doc.get("on"))
    _mode_cache[session_id] = on
    return on


async def apply_mode_command(db, session_id: str, user_id: str, action: str) -> str:
    if not enabled():
        return DISABLED_MSG
    if action == "status":
        on = await is_on(db, session_id)
        return "Engineer mode is on for this chat." if on else "Engineer mode is off for this chat."
    on = action == "on"
    _mode_cache[session_id] = on
    if db is not None:
        try:
            await db[COLLECTION_SESSIONS].update_one(
                {"_id": session_id},
                {"$set": {"on": on, "user_id": user_id, "updated": _dt.datetime.utcnow()}},
                upsert=True)
        except Exception:
            pass
    if on:
        return ("Engineer mode is on for this chat. Describe the problem, or attach a screenshot. "
                "I can read the code and suggest a fix, but I won't change anything. "
                "Send /engineer off to leave this mode.")
    return "Engineer mode is off for this chat."


async def save_report(db, user_id: str, session_id: str, text: str, image_url):
    rid = uuid.uuid4().hex[:12]
    doc = {"_id": rid, "user_id": str(user_id), "session_id": session_id, "text": text[:4000],
           "image_url": image_url, "status": "open", "created": _dt.datetime.utcnow()}
    if db is not None:
        try:
            await db[COLLECTION_REPORTS].insert_one(doc)
        except Exception:
            pass
    return rid


def _tools() -> list:
    return [
        {"type": "function", "function": {
            "name": "search_code",
            "description": "Search the project folder on the worker. Returns the most relevant snippets with file names and line ranges.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
                "repo": {"type": "string", "description": "Folder, e.g. upload:my-project. Optional."}},
                "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Read a file, or part of it, from the project folder on the worker. Numbers each line.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "max_lines": {"type": "integer"},
                "repo": {"type": "string", "description": "Folder. Optional."}},
                "required": ["path"]}}},
        {"type": "function", "function": {
            "name": "repo_map",
            "description": "List the files and top-level symbols in the project folder.",
            "parameters": {"type": "object", "properties": {
                "repo": {"type": "string", "description": "Folder. Optional."}}}}},
    ]


async def _worker_call(user_id: str, action: str, args: dict) -> dict:
    res = await worker_client.try_remote("github", {"user_id": user_id,
                                                    "args": {"action": action, **args}}, wait_s=60)
    if res is None:
        return {"status": "error", "reason": "the worker is not configured, so Engineer cannot read the code"}
    return res


async def _run_tool(user_id: str, name: str, args: dict, default_repo) -> dict:
    if name not in READ_ONLY_TOOLS:
        return {"status": "error", "reason": "that tool is not available in Engineer mode"}
    repo = str(args.get("repo") or default_repo or "").strip()
    if not repo:
        return {"status": "error", "reason": "no project folder is active. Upload the project zip first."}
    if name == "search_code":
        return await _worker_call(user_id, "search_code", {"repo": repo, "query": str(args.get("query") or "")})
    if name == "read_file":
        try:
            start = int(args.get("start_line") or 1)
            count = int(args.get("max_lines") or 200)
        except (TypeError, ValueError):
            return {"status": "error", "reason": "start_line and max_lines must be whole numbers"}
        return await _worker_call(user_id, "read_file", {"repo": repo, "path": str(args.get("path") or ""),
                                                         "start_line": start, "max_lines": count})
    return await _worker_call(user_id, "repo_map", {"repo": repo})


def _clip(obj) -> str:
    return json.dumps(obj, default=str)[:TOOL_RESULT_CHARS]


async def fetch_image_data_url(url, client: httpx.AsyncClient | None = None):
    """Only https links from the upload host, only images, only small ones."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname not in IMAGE_HOSTS:
        return None

    async def _get(c: httpx.AsyncClient):
        r = await c.get(str(url), timeout=30)
        if r.status_code != 200 or len(r.content) > IMAGE_MAX_BYTES:
            return None
        ctype = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if not ctype.startswith("image/"):
            return None
        return f"data:{ctype};base64," + base64.b64encode(r.content).decode()

    if client is not None:
        return await _get(client)
    async with httpx.AsyncClient() as c:
        return await _get(c)


def _budget_ok() -> bool:
    return spend_tracker.can_spend("engineer", RESERVE_USD) and spend_tracker.can_spend("anthropic", RESERVE_USD)


async def run_engineer(user_id: str, session_id: str, text: str, image_url=None) -> dict:
    """Returns {"reply": str, "paused": bool}. Never changes anything."""
    if not enabled():
        return {"reply": DISABLED_MSG, "paused": True}
    if not _budget_ok():
        return {"reply": PAUSED_MSG, "paused": True}
    default_repo = await workspace_pointer.get_active(user_id)
    note = "" if default_repo else ("\n\nNo project folder is active yet, so code search will not work "
                                    "until the project zip is uploaded.")
    content = [{"type": "text", "text": f"Report from the owner:\n{text}{note}"}]
    if image_url:
        data_url = await fetch_image_data_url(image_url)
        if data_url:
            content.append({"type": "image_url", "image_url": {"url": data_url}})
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]
    tools = _tools()

    for _ in range(MAX_STEPS):
        if not _budget_ok():
            return {"reply": PAUSED_MSG, "paused": True}
        try:
            res = await call_claude({"api_key_env": "ANTHROPIC_API_KEY"}, MODEL, messages, tools)
        except ClaudeQuota:
            return {"reply": "Engineer could not reach Claude just now. Your report is saved. Try again in a minute.",
                    "paused": False}
        except Exception as e:
            return {"reply": f"Engineer hit an error and stopped: {type(e).__name__}. Your report is saved.",
                    "paused": False}
        usage = res.get("usage") or {}
        await spend_tracker.record("anthropic", MODEL, usage)
        await spend_tracker.record("engineer", MODEL, usage)

        call = res.get("tool_call")
        if call:
            messages.append(res["raw_message"])
            result = await _run_tool(user_id, call["name"], call.get("args") or {}, default_repo)
            tool_id = res["raw_message"]["tool_calls"][0]["id"]
            messages.append({"role": "tool", "tool_call_id": tool_id, "content": _clip(result)})
            continue
        reply = (res.get("text") or "").strip()
        return {"reply": reply or "Engineer had nothing to add.", "paused": False}

    return {"reply": "Engineer stopped after its step limit. Send the report again with the part you think "
                     "matters most, so it can look at fewer files.", "paused": False}


async def persist_pair(db, session_id: str, user_id: str, user_text: str, reply: str, img_url=None) -> None:
    """Store the turn in the same session shape the normal chat uses."""
    if db is None:
        return
    now = _dt.datetime.utcnow()
    user_msg = {"role": "user", "content": user_text, "time": now}
    if img_url:
        user_msg["attachments"] = [{"url": img_url, "file_type": "image", "filename": "screenshot"}]
    assistant_msg = {"role": "assistant", "content": reply, "time": now, "steps": [],
                     "model_used": f"engineer ({MODEL})", "reasoning_mode": False}
    try:
        await db["ai_sessions"].update_one(
            {"session_id": session_id, "user_id": user_id},
            {"$push": {"messages": {"$each": [user_msg, assistant_msg]}},
             "$set": {"updated_at": now, "surface_origin": "web"},
             "$setOnInsert": {"created_at": now, "name": f"Chat {now.strftime('%b %d')}"}},
            upsert=True)
    except Exception:
        pass
