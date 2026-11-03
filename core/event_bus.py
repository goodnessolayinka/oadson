"""
core/event_bus.py — OADSON V3 Event Bus.

Everything significant emits an event.
Subscribers react. No tight coupling needed.

Events:
    chat.started
    context.built
    memory.retrieved
    intent.analyzed
    plan.generated
    task.started
    task.completed
    task.failed
    tool.executed
    review.passed
    review.failed
    memory.updated
    response.ready
    response.status_changed
"""

import asyncio
import logging
from collections import defaultdict
from typing import Callable, Any, Coroutine
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("oadson.event_bus")


@dataclass
class Event:
    name: str
    data: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    source: str = ""


# Type alias
Handler = Callable[[Event], Coroutine]


class EventBus:
    """
    Async pub/sub event bus.
    Handlers are async functions. Errors in handlers are logged, never raised.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._wildcard: list[Handler] = []   # subscribed to "*"

    def subscribe(self, event_name: str, handler: Handler) -> None:
        """
        Register an async handler for an event.
        Use "*" to receive all events.
        """
        if event_name == "*":
            self._wildcard.append(handler)
        else:
            self._subscribers[event_name].append(handler)
        logger.debug(f"Subscribed to '{event_name}': {handler.__name__}")

    def unsubscribe(self, event_name: str, handler: Handler) -> None:
        if event_name == "*":
            self._wildcard = [h for h in self._wildcard if h is not handler]
        else:
            self._subscribers[event_name] = [
                h for h in self._subscribers[event_name] if h is not handler
            ]

    async def emit(self, name: str, data: dict = None, source: str = "") -> None:
        """
        Emit an event. All matching handlers are called concurrently.
        Handler failures are isolated — one bad handler doesn't kill others.
        """
        event = Event(name=name, data=data or {}, source=source)
        handlers = self._subscribers.get(name, []) + self._wildcard

        if not handlers:
            return

        logger.debug(f"Emitting '{name}' to {len(handlers)} handler(s)")

        results = await asyncio.gather(
            *[self._safe_call(h, event) for h in handlers],
            return_exceptions=True
        )

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    f"Handler {handlers[i].__name__} failed on event '{name}': {result}"
                )

    async def _safe_call(self, handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception as e:
            raise e   # let gather catch it


# ── STANDARD EVENT NAMES (use these, don't invent strings) ──

class Events:
    CHAT_STARTED       = "chat.started"
    CONTEXT_BUILT      = "context.built"
    MEMORY_RETRIEVED   = "memory.retrieved"
    INTENT_ANALYZED    = "intent.analyzed"
    THINKING           = "thinking.generated"
    PLAN_GENERATED     = "plan.generated"
    TASK_STARTED       = "task.started"
    TASK_COMPLETED     = "task.completed"
    TASK_FAILED        = "task.failed"
    TOOL_EXECUTED      = "tool.executed"
    REVIEW_PASSED      = "review.passed"
    REVIEW_FAILED      = "review.failed"
    MEMORY_UPDATED     = "memory.updated"
    RESPONSE_READY     = "response.ready"
    TOKEN_STREAM       = "token.stream"   # fired per-chunk during final answer streaming
    # OADSON V3 EDD — Intelligent Response State & Latency Diagnostics.
    # Backend-authoritative signal for WHY a turn is taking time, distinct
    # from the existing THINKING name above (which is defined but never
    # actually emitted anywhere — a removed narration feature; left alone
    # rather than repurposed, to avoid confusing it with dead code).
    # data shape: {"session_id": str, "status": str, "reason": str|None}
    # status is one of: "thinking" | "working_on_it"
    # (accepted/background don't need their own emission here — accepted
    # reuses CHAT_STARTED, background reuses the existing TASK_STARTED/skill
    # event chat_stream.py already forwards; see that file's docstring.)
    RESPONSE_STATUS_CHANGED = "response.status_changed"
    # TRACE MODE 2026-08-20 — interstitial model text that arrives
    # ALONGSIDE a tool call (captured now by provider_router.py's
    # _call_gemini fix, previously discarded). Distinct from TOKEN_STREAM
    # (which only ever fires for the FINAL answer, after the tool loop
    # ends) and from THINKING (a separate, unused, removed-narration name
    # left alone above) — this is the model's own real words mid-loop,
    # not synthesized narration. Surface-gated to web/app only at the
    # emission site in orchestrator.py; terminal/whatsapp/telegram never
    # trigger this event at all.
    # data shape: {"session_id": str, "text": str}
    TRACE_TALK = "trace.talk"
    # ADDED 2026-09-19 — fired when a stream that ALREADY sent tokens to the
    # client failed part-way and is being replaced by a fresh full answer.
    # The client must discard whatever partial text it has rendered for this
    # turn before the replacement tokens arrive; otherwise the user sees the
    # answer twice (the partial, then the full reply appended after it).
    # data shape: {"session_id": str, "reason": str}
    STREAM_RESET = "stream.reset"
