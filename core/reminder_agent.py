"""
Fast, uncapped delivery loop for explicit user-set @reminder calls.

ADDED 2026-10-01 — deliberately separate from core/proactive_agent.py's
main loop, not a modification of it. That loop's 30-minute poll interval
and 3-per-day cap exist to keep UNPROMPTED check-ins (morning greetings,
"how's your day" nudges, weather/calendar summaries) from being annoying
— real, correct design for discretionary messages. But a reminder the
user explicitly asked for ("remind me in 30 minutes") is a promise to
keep, not a discretionary nudge; it has no business being silently
dropped because OADSON already said good morning and sent two other
check-ins that day, or landing up to 28 minutes late because the next
poll cycle hadn't come around yet.

This loop polls much more frequently (CHECK_INTERVAL_SECS below) and has
NO daily cap and NO hard-sleep window — if the user set a reminder for
3am, they asked for a 3am message, full stop. It reads directly from the
dedicated `reminders` Mongo collection (see skills/reminder/skill.py's
rewrite) rather than anything in proactive_agent.py's state.

Reuses proactive_agent.py's _send_whatsapp and build_owner_chat_id
directly (pure functions, no dependency on that module's daily-cap
state) rather than maintaining a second copy of the GreenAPI integration.
"""

import os
import asyncio
import logging
from datetime import datetime

from core.proactive_agent import _send_whatsapp, build_owner_chat_id

logger = logging.getLogger("oadson.reminder_agent")

# Checked every 60 seconds — far tighter than proactive_agent's 30-minute
# window, since a reminder's whole value is firing close to the time the
# user actually asked for. 60s keeps Mongo query load trivial while still
# being precise enough that "in 30 minutes" lands within a minute of
# accurate, not up to half an hour off.
CHECK_INTERVAL_SECS = 60


async def reminder_loop(db_getter) -> None:
    await asyncio.sleep(30)  # brief startup delay, consistent with proactive_agent's own pattern

    owner_number = os.getenv("WHATSAPP_OWNER_NUMBER", "")
    if not owner_number:
        logger.warning("WHATSAPP_OWNER_NUMBER not set — reminder delivery loop will idle with nothing to send to.")

    owner_chat_id = build_owner_chat_id(owner_number) if owner_number else None

    while True:
        try:
            if not owner_chat_id:
                await asyncio.sleep(CHECK_INTERVAL_SECS)
                continue

            db = db_getter()
            user = await db["users"].find_one({})
            if not user:
                await asyncio.sleep(CHECK_INTERVAL_SECS)
                continue
            user_id = str(user["_id"])

            now = datetime.utcnow()
            cursor = db["reminders"].find({
                "user_id": user_id,
                "deadline": {"$lte": now},
                "delivered": False,
            })
            due = [doc async for doc in cursor]

            for reminder in due:
                task_name = reminder.get("task_name", "your task")
                message = f"⏰ Reminder: {task_name}"
                ok, detail, _ = await _send_whatsapp(owner_chat_id, message)
                if ok:
                    await db["reminders"].update_one(
                        {"_id": reminder["_id"]},
                        {"$set": {"delivered": True, "delivered_at": datetime.utcnow()}},
                    )
                    logger.info(f"Reminder delivered: {task_name} (id={reminder['_id']})")
                else:
                    # Leave delivered=False so the NEXT cycle (60s later)
                    # retries automatically — a transient GreenAPI failure
                    # shouldn't silently lose the reminder.
                    logger.warning(f"Reminder delivery failed for '{task_name}': {detail} — will retry next cycle")

        except Exception as e:
            logger.error(f"reminder_loop iteration failed: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECS)


def start_reminder_agent(db_getter) -> None:
    """Call from bootstrap to launch the reminder delivery loop as a background task."""
    loop = asyncio.get_event_loop()
    loop.create_task(reminder_loop(db_getter))
    logger.info("⏰ Reminder delivery agent task queued.")
