"""
core/event_handlers.py — OADSON V3 Event Handlers.

All event subscribers live here.
Registered once by bootstrap after the bus is ready.

Handlers:
  - Log all events to console
  - Persist skill executions to db.skill_executions
  - Track token usage per session
"""

import logging
from datetime import datetime
from core.event_bus import EventBus, Events, Event

logger = logging.getLogger("oadson.event_handlers")


async def _on_tool_executed(event: Event) -> None:
    """Persist every skill execution to db.skill_executions."""
    from bootstrap import get
    db = get("db")

    await db["skill_executions"].insert_one({
        "skill": event.data.get("skill"),
        "status": event.data.get("status"),
        "user_id": event.data.get("user_id"),
        "session_id": event.data.get("session_id"),
        "surface": event.data.get("surface"),
        "duration_ms": event.data.get("duration_ms"),
        "timestamp": datetime.utcnow(),
    })


async def _on_response_ready(event: Event) -> None:
    """Log response delivery. Extend for analytics later."""
    logger.info(
        f"Response ready — user:{event.data.get('user_id')} "
        f"model:{event.data.get('model_used')} "
        f"session:{event.data.get('session_id')}"
    )


async def _on_review_failed(event: Event) -> None:
    logger.warning(f"Review failed: {event.data.get('issues')}")


async def _on_task_failed(event: Event) -> None:
    logger.error(f"Task failed: {event.data.get('error')} | skill:{event.data.get('skill')}")


async def _on_any(event: Event) -> None:
    """Wildcard — debug log every event. Disable in prod if noisy."""
    logger.debug(f"[EVENT] {event.name} | {event.data}")


def register_all(bus: EventBus) -> None:
    """
    Called by bootstrap after the bus is created.
    Wire all handlers here.
    """
    bus.subscribe(Events.TOOL_EXECUTED, _on_tool_executed)
    bus.subscribe(Events.RESPONSE_READY, _on_response_ready)
    bus.subscribe(Events.REVIEW_FAILED, _on_review_failed)
    bus.subscribe(Events.TASK_FAILED, _on_task_failed)

    # Uncomment in dev to trace every event:
    # bus.subscribe("*", _on_any)

    logger.info("✅ Event handlers registered.")
