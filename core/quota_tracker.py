"""
core/quota_tracker.py — OADSON V3 Quota Tracker.

In-memory sliding-window usage tracking per (provider, model), so
core/provider_router.py can proactively skip a model that's close to its
rate limit instead of only finding out after a 429 comes back. Not
persisted to Mongo — resets on every Render restart/redeploy, which is
an acceptable tradeoff for a single-process free-tier deployment (the
whole point is avoiding wasted requests within a live process, not
perfect cross-restart accounting).

Two windows tracked per model: requests-per-minute (rolling 60s) and
requests-per-day (resets at UTC midnight) — the two dimensions every
provider in core/provider_registry.py actually rate-limits on.
"""

import time
import logging
from datetime import datetime, timezone
from collections import defaultdict

logger = logging.getLogger("oadson.quota_tracker")

# key: "provider:model" -> list of unix timestamps of recent requests
_request_log: dict = defaultdict(list)
# key: "provider:model" -> (utc_date_string, count) for today's requests
_daily_count: dict = {}
# key: "provider:model" -> unix time until which this model is considered
# dead. ADDED 2026-09-19 — see mark_exhausted()'s docstring.
_cooldown_until: dict = {}

# How long a model that just returned 429/5xx is skipped. Short enough
# that a model recovering from a per-minute limit is retried soon, long
# enough that one dead model isn't re-tried (a full network round trip,
# up to the client timeout) at the head of EVERY request in between.
DEFAULT_COOLDOWN_SECS = 60
# A daily-quota 429 won't clear in a minute — see mark_exhausted().
DAILY_COOLDOWN_SECS = 15 * 60


def _key(provider: str, model: str) -> str:
    return f"{provider}:{model}"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def can_use(provider: str, model: str, rpm: int = None, rpd: int = None) -> bool:
    """
    True if this model has headroom under BOTH its RPM and RPD limits
    right now. None for either limit means "not tracked, assume OK" —
    used for Cerebras's token-based daily cap, which this simple
    request-count tracker doesn't model precisely (see record_use).
    """
    k = _key(provider, model)
    now = time.time()

    # A model that recently returned 429/5xx is skipped outright. Without
    # this, can_use() only knew about SUCCESSFUL calls (record_use is only
    # reached after a call succeeds), so a model that was genuinely out of
    # quota looked "healthy" forever: every request re-tried it first, paid
    # a full round trip for the failure, and only then moved on.
    until = _cooldown_until.get(k)
    if until:
        if now < until:
            return False
        _cooldown_until.pop(k, None)

    if rpm:
        recent = [t for t in _request_log[k] if now - t < 60]
        _request_log[k] = recent
        if len(recent) >= rpm:
            return False

    if rpd:
        today = _today()
        date, count = _daily_count.get(k, (today, 0))
        if date != today:
            count = 0
        if count >= rpd:
            return False

    return True


def record_use(provider: str, model: str) -> None:
    """Call this after an ACTUAL request attempt (success or failure that still consumed quota — i.e. anything that wasn't skipped by can_use)."""
    k = _key(provider, model)
    now = time.time()
    _request_log[k].append(now)

    today = _today()
    date, count = _daily_count.get(k, (today, 0))
    if date != today:
        count = 0
    _daily_count[k] = (today, count + 1)


def mark_exhausted(provider: str, model: str, reason: str = "", daily: bool = False) -> None:
    """
    Put a model on cooldown after it returned 429/5xx/timeout, so the NEXT
    request skips it instead of paying for the same failure again.

    ADDED 2026-09-19. Root cause of the "response time varies from 1 to 15
    minutes" report: decide_gemini() walks 12 Gemini models in registry
    order, and record_use() was only ever called on SUCCESS. A model that
    returned 429 was never remembered, so on a bad quota day every single
    message re-tried each dead model in turn (each a full network round
    trip, up to 60s on a timeout) before reaching one that worked. The
    delay therefore depended on how many models happened to be exhausted
    at that moment — exactly the "varies from 1 to 15 minutes" pattern.

    daily=True (Google's RESOURCE_EXHAUSTED / per-day quota text) uses the
    longer cooldown, since that won't clear in a minute.
    """
    k = _key(provider, model)
    secs = DAILY_COOLDOWN_SECS if daily else DEFAULT_COOLDOWN_SECS
    _cooldown_until[k] = time.time() + secs
    logger.warning(f"QUOTA cooldown {secs}s -> {k} ({reason[:160] or 'no reason given'})")


def cooldown_remaining(provider: str, model: str) -> float:
    """Seconds left on a model's cooldown, 0 if none. For logging/status."""
    until = _cooldown_until.get(_key(provider, model), 0)
    return max(0.0, until - time.time())


def usage_snapshot() -> dict:
    """Debug/status helper — current usage per tracked model, for a future /engineer/status-style view."""
    now = time.time()
    out = {}
    for k, timestamps in _request_log.items():
        recent = [t for t in timestamps if now - t < 60]
        today = _today()
        date, count = _daily_count.get(k, (today, 0))
        out[k] = {
            "requests_last_60s": len(recent),
            "requests_today": count if date == today else 0,
            "cooldown_remaining_s": round(max(0.0, _cooldown_until.get(k, 0) - now), 1),
        }
    return out
