from datetime import datetime, timedelta
from typing import Optional
import re

# REWRITTEN 2026-10-01 — the previous implementation stored reminders in
# `self.reminders: dict[str, dict] = {}` on the ReminderSkill INSTANCE,
# but core/skill_executor.py (and this file's own `run()`) create a
# BRAND NEW ReminderSkill(memory_vault) on every single call — so
# self.reminders was wiped clean between every invocation. action='set'
# wrote a note to the memory vault (as a plain content string, for
# human-readable recall only) but check_reminders() read exclusively
# from the in-memory dict, which could NEVER contain anything a prior
# call had set. The skill was non-functional for its actual job —
# "set" appeared to succeed, but no later "check" could ever find it.
#
# Now reads/writes a dedicated `reminders` Mongo collection directly
# (structured fields: deadline as a real datetime, a `delivered` flag,
# user_id) instead of either the broken in-memory dict or trying to
# force structured, queryable data through MemoryVault.store()'s
# plain-string `content` field, which isn't designed for this. This
# collection is also what core/reminder_agent.py's fast-polling delivery
# loop reads from directly — see that file for the actual proactive
# WhatsApp delivery side, deliberately separate from core/
# proactive_agent.py's daily-capped, 30-minute-interval loop, since an
# explicit user-set reminder is a promise to keep, not a discretionary
# check-in subject to the same "don't be annoying" gates.

class ReminderSkill:
    def __init__(self, db, user_id: str):
        self.db = db
        self.user_id = user_id
        self.collection = db["reminders"]

    async def set_reminder(self, task_name: str, deadline: datetime) -> str:
        doc = {
            "user_id": self.user_id,
            "task_name": task_name,
            "deadline": deadline,
            "created_at": datetime.utcnow(),
            "delivered": False,
        }
        result = await self.collection.insert_one(doc)
        return str(result.inserted_id)

    async def check_due(self) -> list[dict]:
        """Returns every reminder for this user whose deadline has passed and hasn't been delivered yet."""
        now = datetime.utcnow()
        cursor = self.collection.find({
            "user_id": self.user_id,
            "deadline": {"$lte": now},
            "delivered": False,
        })
        return [doc async for doc in cursor]

    async def mark_delivered(self, reminder_id) -> None:
        from bson import ObjectId
        await self.collection.update_one(
            {"_id": ObjectId(reminder_id) if not isinstance(reminder_id, type(None)) else reminder_id},
            {"$set": {"delivered": True, "delivered_at": datetime.utcnow()}},
        )


def _parse_deadline(message: str) -> datetime:
    """
    Lightweight natural-language deadline parser — no external dependencies,
    consistent with how gmail/google_drive/memory_manager do plain keyword
    matching rather than reaching for a date-parsing library. Covers the
    common phrasings ("tomorrow", "today", "in N hours/days/minutes", a
    weekday name) and defaults to 24 hours out if nothing matches, so a
    reminder always gets a usable deadline instead of crashing.
    """
    msg = message.lower()
    now = datetime.utcnow()

    if "tomorrow" in msg:
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    if "today" in msg or "tonight" in msg:
        return now.replace(hour=20, minute=0, second=0, microsecond=0)

    m = re.search(r"in (\d+)\s*hour", msg)
    if m:
        return now + timedelta(hours=int(m.group(1)))

    m = re.search(r"in (\d+)\s*(day|minute)", msg)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        return now + timedelta(days=n) if unit == "day" else now + timedelta(minutes=n)

    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, day in enumerate(weekdays):
        if day in msg:
            days_ahead = (i - now.weekday()) % 7
            days_ahead = days_ahead or 7  # if today, assume next week's occurrence
            return (now + timedelta(days=days_ahead)).replace(hour=9, minute=0, second=0, microsecond=0)

    # No recognizable time phrase — default to 24h out rather than failing.
    return now + timedelta(hours=24)


def _extract_task_name(message: str) -> str:
    """Strip command words and time phrases to isolate the task name."""
    msg = message.strip()
    for phrase in ["remind me about", "remind me to", "remind me of", "remind me",
                   "set a reminder for", "set reminder for", "reminder for",
                   "remind", "reminder"]:
        if msg.lower().startswith(phrase):
            msg = msg[len(phrase):].strip()
            break

    for time_phrase in ["tomorrow", "today", "tonight"]:
        msg = re.sub(rf"\b{time_phrase}\b", "", msg, flags=re.IGNORECASE)

    msg = re.sub(r"\bin \d+\s*(hour|minute|day)s?\b", "", msg, flags=re.IGNORECASE)
    msg = re.sub(r"\b(by|on) (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", "", msg, flags=re.IGNORECASE)

    return msg.strip(" .,!") or "your task"


# ── SKILL ENTRY POINT ──
# Primary path is structured args (action/task_name/deadline as a real
# ISO timestamp, per manifest.json's parameters schema). _parse_deadline/
# _extract_task_name above are kept ONLY as a fallback for resilience —
# if Gemini ever sends a vague deadline string instead of clean ISO
# format, this skill can still recover rather than erroring outright.

async def run(payload):
    """Execute reminder skill against the real `reminders` Mongo collection."""
    user_id = payload.get("user_id")
    db = payload.get("db")
    args = payload.get("args", {}) or {}

    if not user_id:
        return {"status": "error", "reason": "User ID not available"}

    if db is None:
        return {"status": "error", "reason": "Database connection not available in payload"}

    action = args.get("action")
    if not action:
        return {
            "status": "info",
            "message": "Reminder commands: action='set' with task_name + deadline, or action='check'."
        }

    try:
        skill = ReminderSkill(db, user_id)

        if action == "set":
            task_name = (args.get("task_name") or "").strip()
            deadline_str = (args.get("deadline") or "").strip()

            if not task_name:
                raw_message = payload.get("message", "")
                task_name = _extract_task_name(raw_message) if raw_message else "your task"

            if deadline_str:
                try:
                    deadline = datetime.fromisoformat(deadline_str)
                except ValueError:
                    deadline = _parse_deadline(deadline_str)
            else:
                raw_message = payload.get("message", "")
                deadline = _parse_deadline(raw_message) if raw_message else (datetime.utcnow() + timedelta(hours=24))

            reminder_id = await skill.set_reminder(task_name, deadline)

            return {
                "status": "success",
                "result": {
                    "reminder_id": reminder_id,
                    "task_name": task_name,
                    "deadline": deadline.isoformat(),
                    "message": f"Reminder set: {task_name} at {deadline.strftime('%A %I:%M %p')} UTC — "
                               f"you'll be messaged within a few minutes of this time."
                }
            }

        elif action == "check":
            due = await skill.check_due()
            if not due:
                return {"status": "success", "result": {"message": "No reminders due right now"}}
            lines = [f"{d['task_name']} (was due {d['deadline'].strftime('%A %I:%M %p')} UTC)" for d in due]
            return {"status": "success", "result": {"message": "; ".join(lines), "count": len(due)}}

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be 'set' or 'check'"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
