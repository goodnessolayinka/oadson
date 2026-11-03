"""
api/engineer.py — OADSON V3 Engineer Mode.

Admin endpoints. Gated by OADSON_ENGINEER_TOKEN.
Not behind Google OAuth — these run when auth might be broken.

Endpoints:
  GET  /engineer/status        — full system health
  GET  /engineer/registry      — all loaded services
  GET  /engineer/skills        — skills with full manifest
  POST /engineer/skills/reload — hot-reload all skills
  GET  /engineer/events        — recent skill executions from db
  GET  /engineer/sessions      — active session count
  POST /engineer/memory/clear  — clear working memory for a user
"""

import os
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Header
from typing import Optional

logger = logging.getLogger("oadson.api.engineer")


router = APIRouter()


def _verify_engineer(token: Optional[str] = Header(None, alias="x-engineer-token")):
    expected = os.getenv("OADSON_ENGINEER_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Engineer Mode not configured. Set OADSON_ENGINEER_TOKEN.")
    if not token or token != expected:
        raise HTTPException(403, "Invalid engineer token.")


# ── STATUS ──

# ── PING / LATENCY ──
# ADDED 2026-08-16 per direct request: frontend needs round-trip
# latency in ms with a red/yellow/green banding, without hand-rolling
# the thresholds client-side every time. Deliberately NOT gated behind
# _verify_engineer — a latency check is the one thing you'd want to run
# BEFORE you know whether the server/token setup is even healthy, and
# it leaks no sensitive data (no service names, no DB status, nothing
# from system_status below), so it's safe to leave open. Server does
# the absolute minimum possible work (no DB call, no skill registry
# lookup) so the measured time reflects real network/server round-trip,
# not incidental work this endpoint happens to do.
LATENCY_THRESHOLDS_MS = {"green_max": 300, "yellow_max": 800}
# green:  0-300ms   — feels instant
# yellow: 300-800ms — noticeable but usable
# red:    800ms+    — the actual "why is OADSON slow" zone


@router.get("/engineer/ping")
async def ping(request_id: Optional[str] = None):
    """
    Minimal round-trip latency probe.

    Client usage: record a timestamp, call this, record the response
    timestamp, subtract. This endpoint's own `server_time` field lets
    the client optionally also estimate one-way clock skew, but the
    PRIMARY signal is just "how long did this request take" measured
    entirely client-side — the server has no way to know true network
    latency, only round-trip time as observed by whoever's calling.

    request_id is optional passthrough (echoed back unchanged) so a
    client firing several concurrent pings can match responses to
    requests without relying on arrival order.
    """
    return {
        "status": "ok",
        "server_time_ms": int(datetime.utcnow().timestamp() * 1000),
        "request_id": request_id,
        "thresholds_ms": LATENCY_THRESHOLDS_MS,
    }


def classify_latency_ms(latency_ms: float) -> str:
    """
    Shared classification so the color banding is defined ONCE, here,
    rather than the frontend and any future backend caller each
    hand-rolling their own copy of the same 300/800 cutoffs and
    drifting out of sync over time.
    """
    if latency_ms <= LATENCY_THRESHOLDS_MS["green_max"]:
        return "green"
    if latency_ms <= LATENCY_THRESHOLDS_MS["yellow_max"]:
        return "yellow"
    return "red"


@router.get("/engineer/status")
async def system_status(token: str = Header(..., alias="x-engineer-token")):
    _verify_engineer(token)

    from bootstrap import _registry, get

    services = list(_registry.keys())

    # Skill count
    try:
        skill_count = get("skill_registry").count()
    except Exception:
        skill_count = -1

    # DB ping — per-cluster now that there can be more than one
    # (core/db_router.py). A single "ok"/"error" would hide a scenario
    # where the core cluster is fine but the chat or storage cluster
    # (once configured) is down.
    db_clusters = {}
    try:
        db = get("db")
        db_clusters = await db.ping_all()
    except Exception as e:
        db_clusters = {"core": f"error: {e}"}

    # Provider check — includes the new fallback providers
    # (core/provider_registry.py) alongside the existing ones.
    try:
        pm = get("provider_manager")
        gemini_ok = bool(pm._gemini_key)
        openrouter_ok = bool(pm._openrouter_key)
    except Exception:
        gemini_ok = openrouter_ok = False
    groq_ok = bool(os.getenv("GROQ_API_KEY"))
    cerebras_ok = bool(os.getenv("CEREBRAS_API_KEY"))

    return {
        "version": "3.0.0",
        "timestamp": datetime.utcnow().isoformat(),
        "services": services,
        "skill_count": skill_count,
        "database": db_clusters,
        "providers": {
            "gemini": "configured" if gemini_ok else "missing key",
            "groq": "configured" if groq_ok else "missing key",
            "cerebras": "configured" if cerebras_ok else "missing key",
            "openrouter": "configured" if openrouter_ok else "missing key",
        },
        "whatsapp": os.getenv("WHATSAPP_BRIDGE_SECRET", "") != "",
        "telegram": os.getenv("TELEGRAM_BOT_TOKEN", "") != "",
    }


@router.get("/engineer/memory-metrics")
async def memory_metrics(token: str = Header(..., alias="x-engineer-token")):
    """
    ADDED 2026-08-29 — real container CPU/RAM/disk, via
    skills/system_status/skill.py's cgroup-aware get_system_metrics()
    (the fix for the earlier host-vs-container bug — this is the same
    live check confirmed accurate on the real server: 512MB total,
    correctly scoped to "container (cgroup v2)" instead of the host's
    full memory). No db dependency — this is a pure OS-level read, same
    as the skill's own doc says. Kept as its own endpoint rather than
    folded into /engineer/status because it's called far more often in
    practice (anyone checking "are we close to OOM" wants just this,
    not the whole status payload) and status already has more calls
    doing more work in it.
    """
    _verify_engineer(token)
    from skills.system_status.skill import SystemStatusSkill
    skill = SystemStatusSkill(db=None)
    return skill.get_system_metrics()


@router.get("/engineer/memory-events")
async def memory_events_endpoint(
    limit: int = 20,
    token: str = Header(..., alias="x-engineer-token"),
):
    """
    ADDED 2026-08-29 — the crash-diagnosis log from
    core/oadson_home.log_memory_event (capped Mongo collection, written
    at the start of a memory-heavy operation like a github clone, and
    again if the pre-flight memory guard actually blocks one — see that
    module's own docstring for the full design). Was only reachable via
    a skill call in chat (@system_status memory_events) — no dedicated
    HTTP route existed, which is why it never showed up as a button in
    the engineer dashboard alongside self-test, skill-reliability, etc.
    Real gap, not a frontend bug — fixed by actually adding the route.
    """
    _verify_engineer(token)
    from bootstrap import get
    db = get("db")
    from skills.system_status.skill import SystemStatusSkill
    skill = SystemStatusSkill(db=db)
    return await skill.get_memory_events(limit=limit)


# ── REGISTRY ──

@router.get("/engineer/registry")
async def view_registry(token: str = Header(..., alias="x-engineer-token")):
    _verify_engineer(token)
    from bootstrap import _registry
    return {"services": list(_registry.keys())}


# ── SKILLS ──

@router.get("/engineer/skills")
async def list_all_skills(token: str = Header(..., alias="x-engineer-token")):
    _verify_engineer(token)
    from bootstrap import get
    registry = get("skill_registry")
    return {"skills": registry.all(), "count": registry.count()}


@router.post("/engineer/skills/reload")
async def reload_all_skills(token: str = Header(..., alias="x-engineer-token")):
    _verify_engineer(token)
    from bootstrap import get
    loader = get("skill_loader")
    registry = get("skill_registry")

    before = registry.count()
    loader.load_all()
    after = registry.count()

    return {
        "status": "reloaded",
        "before": before,
        "after": after,
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.post("/engineer/self-test")
async def self_test(token: str = Header(..., alias="x-engineer-token")):
    """
    Added 2026-08-07. Runs core/self_test.py's regression suite — a
    targeted set of checks over bugs that actually happened in this
    codebase (the wind_flag NameError, the pptx bullet-loss, the
    WhatsApp chat_id corruption), not a generic framework. Meant to be
    run after any deploy touching file_generation, weather_forecast,
    or proactive_agent, to catch a reintroduced bug before it's found
    live in a chat again.
    """
    _verify_engineer(token)
    from core.self_test import run_self_test
    return await run_self_test()


@router.post("/engineer/test-models")
async def test_models_endpoint(
    token: str = Header(..., alias="x-engineer-token"),
    models: str = None,
):
    """
    Added 2026-08-08. Deliberately calls any number of configured
    models directly — see core/model_tester.py's docstring for why
    this exists: a light-traffic environment means fallback models
    can go unexercised for weeks between real quota-exhaustion events,
    which is exactly how 3 retired Gemini experimental IDs sat
    undetected. This is the on-demand version: test everything right
    now, on purpose.

    `models` query param: comma-separated model IDs (or 'provider/id'
    for non-Gemini) to test a specific subset. Omit to test ONLY the
    one selected model, core/model_tester.py's DEFAULT_TEST_MODEL.
    """
    _verify_engineer(token)
    from core.model_tester import test_models
    selection = [m.strip() for m in models.split(",")] if models else None
    return await test_models(selection)


@router.get("/engineer/skill-reliability")
async def skill_reliability_endpoint(
    token: str = Header(..., alias="x-engineer-token"),
    skill: str = None,
):
    """
    Added 2026-08-09 — item #2 from a hardening plan drafted by another
    AI off the session changelog, picked as the one reasonable thing to
    build immediately (no new DB, no pytest dependency that doesn't
    actually exist in this codebase, just real math over
    db.skill_executions which is already being written on every call).

    `skill` query param: name of one skill for its full report. Omit
    for a ranked report across every skill with execution history,
    worst success-rate first.
    """
    _verify_engineer(token)
    from bootstrap import get
    from core.skill_reliability import get_skill_reliability, get_all_skill_reliability
    db = get("db")
    if skill:
        return await get_skill_reliability(db, skill)
    return await get_all_skill_reliability(db)


# ── PROACTIVE AGENT ──

@router.get("/engineer/proactive")
async def proactive_status(token: str = Header(..., alias="x-engineer-token")):
    """
    Added 2026-08-07 alongside the engineer dashboard — direct REST
    access to the same bridge_settings doc core/proactive_agent.py
    writes every check cycle, so the dashboard can poll it without
    going through a chat turn. Mirrors skills/system_status.py's
    'proactive_status' action, kept separate on purpose: this one is
    for the dashboard (REST, engineer-token gated), that one is for
    asking OADSON directly in chat — same data, two access paths for
    two different situations.
    """
    _verify_engineer(token)
    from bootstrap import get
    db = get("db")
    meta = await db["bridge_settings"].find_one({"_id": "proactive_agent_meta"}) or {}
    meta.pop("_id", None)
    for k in ("last_sent_at", "last_checked_at"):
        if meta.get(k):
            meta[k] = meta[k].isoformat()
    return {"proactive_agent": meta}


# ── EVENT LOG ──

@router.get("/engineer/events")
async def event_log(
    token: str = Header(..., alias="x-engineer-token"),
    limit: int = 50,
    skill: Optional[str] = None,
):
    _verify_engineer(token)
    from bootstrap import get
    db = get("db")

    filter_ = {}
    if skill:
        filter_["skill"] = skill

    cursor = db["skill_executions"].find(
        filter_,
        {"_id": 0},
    ).sort("timestamp", -1).limit(limit)

    events = []
    async for doc in cursor:
        doc["timestamp"] = doc["timestamp"].isoformat() if hasattr(doc.get("timestamp"), "isoformat") else str(doc.get("timestamp", ""))
        events.append(doc)

    return {"events": events, "count": len(events)}


# ── SESSIONS ──

@router.get("/engineer/sessions")
async def session_stats(token: str = Header(..., alias="x-engineer-token")):
    _verify_engineer(token)
    from bootstrap import get
    db = get("db")

    total = await db["ai_sessions"].count_documents({})
    recent = await db["ai_sessions"].count_documents({
        "updated_at": {"$gte": datetime.utcnow() - timedelta(hours=24)}
    })
    active_auth = await db["sessions"].count_documents({
        "expires_at": {"$gt": datetime.utcnow()}
    })

    return {
        "total_sessions": total,
        "active_last_24h": recent,
        "active_auth_tokens": active_auth,
    }


# ── MEMORY CLEAR ──

@router.post("/engineer/memory/clear")
async def clear_working_memory(
    token: str = Header(..., alias="x-engineer-token"),
    user_id: Optional[str] = None,
):
    _verify_engineer(token)
    from bootstrap import get
    memory = get("memory_manager")

    if user_id:
        await memory.clear_working(user_id=user_id)
        return {"status": "cleared", "user_id": user_id}

    # Clear all working memory if no user specified
    db = get("db")
    result = await db["memory"].delete_many({"type": "working"})
    return {"status": "cleared", "deleted": result.deleted_count}


# ── PROACTIVE AGENT TEST ──

@router.post("/engineer/test-proactive")
async def test_proactive_message(
    token: str = Header(..., alias="x-engineer-token"),
):
    """
    Trigger an immediate proactive WhatsApp message bypassing all timing/gap checks.
    Use this to verify the proactive agent is working after deploy.
    """
    _verify_engineer(token)
    import os
    from core.proactive_agent import (
        _decide, _get_meta, _send_whatsapp, _now_wat, build_owner_chat_id
    )
    from bootstrap import get

    db = get("db")
    user = await db["users"].find_one({})
    if not user:
        return {"status": "error", "reason": "No user found in DB."}

    user_id = str(user["_id"])
    owner_number = os.getenv("WHATSAPP_OWNER_NUMBER", "").strip()
    if not owner_number:
        return {"status": "error", "reason": "WHATSAPP_OWNER_NUMBER not set."}

    # Uses the same build_owner_chat_id() as the real proactive loop —
    # was a separately hand-rolled digit filter before 2026-08-07, which
    # happened to be correct but meant a fix to one wouldn't propagate
    # to the other.
    owner_chat_id = build_owner_chat_id(owner_number)
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    openweather_key = os.getenv("OPENWEATHER_API_KEY", "")

    meta = await _get_meta(db)

    # Force the decision call — skip all timing guards
    should_send, message, msg_format = await _decide(
        db=db,
        user_id=user_id,
        owner_chat_id=owner_chat_id,
        gemini_key=gemini_key,
        openweather_key=openweather_key,
        meta=meta,
    )

    if should_send and message:
        ok, detail, _ = await _send_whatsapp(owner_chat_id, message)
        return {
            "status": "sent" if ok else "send_failed",
            "message": message,
            "format": msg_format,
            "chat_id": owner_chat_id,
            "delivery_detail": detail,
            "note": "Timing/gap checks bypassed for test — check your WhatsApp." if ok else "GreenAPI rejected the send — see delivery_detail.",
        }
    else:
        # AI said no — force it anyway for test
        test_msg = f"[PROACTIVE TEST — {_now_wat().strftime('%H:%M WAT')}] OADSON proactive agent is live and working."
        ok, detail, _ = await _send_whatsapp(owner_chat_id, test_msg)
        return {
            "status": "test_forced" if ok else "send_failed",
            "message": test_msg,
            "chat_id": owner_chat_id,
            "delivery_detail": detail,
            "ai_decision": "AI decided not to send right now (normal), but test message sent anyway.",
        }


@router.get("/engineer/stickers")
async def sticker_pack_status(
    token: str = Header(..., alias="x-engineer-token"),
):
    """List the full sticker pack — IDs, labels, send counts, embed status."""
    _verify_engineer(token)
    from bootstrap import get
    db = get("db")
    user = await db["users"].find_one({})
    if not user:
        return {"status": "error", "reason": "No user found"}
    user_id = str(user["_id"])

    cursor = db["sticker_pack"].find(
        {"user_id": user_id},
        {"_id": 0, "sticker_id": 1, "emoji_hint": 1, "times_sent": 1,
         "saved_at": 1, "filename": 1, "download_url": 1,
         "embedding": 1}
    ).sort("saved_at", -1)

    stickers = []
    async for doc in cursor:
        has_embedding = bool(doc.pop("embedding", None))
        doc["has_embedding"] = has_embedding
        stickers.append(doc)

    return {
        "total": len(stickers),
        "embedded": sum(1 for s in stickers if s["has_embedding"]),
        "stickers": stickers,
    }


@router.post("/engineer/stickers/test-send")
async def test_sticker_send(
    token: str = Header(..., alias="x-engineer-token"),
    sticker_id: str = None,
    emoji_hint: str = None,
):
    """
    Send a test sticker to WhatsApp.
    Optionally pass sticker_id to send a specific one,
    or emoji_hint to test semantic search. Defaults to random.
    """
    _verify_engineer(token)
    import os
    from bootstrap import get
    from core.proactive_agent import build_owner_chat_id

    db = get("db")
    user = await db["users"].find_one({})
    if not user:
        return {"status": "error", "reason": "No user found"}
    user_id = str(user["_id"])

    owner_number = os.getenv("WHATSAPP_OWNER_NUMBER", "").strip()
    if not owner_number:
        return {"status": "error", "reason": "WHATSAPP_OWNER_NUMBER not set"}
    owner_chat_id = build_owner_chat_id(owner_number)

    # Use sticker_reply skill directly
    from core.skill_executor import SkillExecutor
    from bootstrap import get as bget
    executor: SkillExecutor = bget("skill_executor")

    result = await executor.execute("sticker_reply", {
        "user_id": user_id,
        "session_id": "engineer_test",
        "db": db,
        "args": {
            "action": "send",
            "sticker_id": sticker_id or "",
            "emoji_hint": emoji_hint or "",
        }
    })

    if result.get("status") == "empty":
        # Debug: also check raw count in DB
        raw_count = await db["sticker_pack"].count_documents({"user_id": user_id})
        raw_any = await db["sticker_pack"].count_documents({})
        return {
            "status": "no_stickers",
            "reason": result.get("message", "No stickers in pack yet."),
            "debug": {
                "user_id_used": user_id,
                "docs_for_this_user": raw_count,
                "total_docs_in_collection": raw_any,
            }
        }

    if result.get("status") != "success":
        return {"status": "error", "reason": result.get("reason", "Skill failed"), "detail": result}

    download_url = result.get("download_url")
    filename = result.get("filename", "sticker.webp")

    # Deliver via GreenAPI — _deliver_skill_attachment now returns the
    # real GreenAPI response (status code + body) instead of a bare
    # bool, so a rejection (sendSticker is picky: static WebP only,
    # size/dimension limits) is diagnosable straight from this response
    # instead of having to go dig through Render logs.
    from api.whatsapp import _deliver_skill_attachment
    delivery = await _deliver_skill_attachment(
        owner_chat_id,
        {"download_url": download_url, "filename": filename, "mime_type": "image/webp"},
        quoted_message_id=None,
    )
    delivered = delivery["delivered"]

    return {
        "status": "sent" if delivered else "deliver_failed",
        "sticker_id": result.get("sticker_id"),
        "emoji_hint": result.get("emoji_hint"),
        "download_url": download_url,
        "note": "Check your WhatsApp." if delivered else "GreenAPI delivery failed — see debug for the real reason.",
        "debug": None if delivered else {
            "url_attempted": download_url,
            "filename": filename,
            "chat_id": owner_chat_id,
            "endpoint": delivery.get("endpoint"),
            "greenapi_status_code": delivery.get("status_code"),
            "greenapi_response": delivery.get("body"),
            "reason": delivery.get("reason"),
        }
    }


@router.post("/engineer/stickers/backfill-labels")
async def backfill_sticker_labels(
    token: str = Header(..., alias="x-engineer-token"),
):
    """Re-run auto-labeling on all stickers that have no emoji_hint yet."""
    _verify_engineer(token)
    import os, httpx as _httpx
    from bootstrap import get
    db = get("db")
    user = await db["users"].find_one({})
    if not user:
        return {"status": "error", "reason": "No user found"}
    user_id = str(user["_id"])

    gemini_key = os.getenv("GEMINI_API_KEY", "")
    label_model = os.getenv("GEMINI_THINKING_MODEL", "gemini-2.5-flash-lite")

    cursor = db["sticker_pack"].find(
        {"user_id": user_id, "$or": [
            {"emoji_hint": {"$exists": False}},
            {"emoji_hint": ""},
        ]},
        {"sticker_id": 1, "download_url": 1, "filename": 1}
    )
    stickers = [doc async for doc in cursor]

    if not stickers:
        return {"status": "ok", "message": "All stickers already labeled."}

    labeled = 0
    failed = 0

    for sticker in stickers:
        download_url = sticker.get("download_url", "")
        if not download_url:
            failed += 1
            continue
        try:
            # Fetch the sticker image
            async with _httpx.AsyncClient(timeout=15) as client:
                img_resp = await client.get(download_url)
            if img_resp.status_code != 200:
                failed += 1
                continue
            import base64
            sticker_b64 = base64.b64encode(img_resp.content).decode()

            # Label it
            label_body = {
                "contents": [{"parts": [
                    {"inline_data": {"mime_type": "image/webp", "data": sticker_b64}},
                    {"text": (
                        "Describe this sticker in 3-5 words maximum, plus 1-2 relevant emoji. "
                        "Be specific — if it's a footballer, name them. "
                        "Format: just the description and emoji, nothing else. "
                        "Examples: 'Ronaldo siuuu celebration 🎉', 'crying laughing 😂', 'thumbs up 👍'"
                    )}
                ]}],
                "generationConfig": {"maxOutputTokens": 30, "temperature": 0.2},
            }
            # SECURITY FIX 2026-08-14: key moved from URL to
            # x-goog-api-key header — was leaking into Render's httpx
            # request logs.
            async with _httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{label_model}:generateContent",
                    json=label_body,
                    headers={"x-goog-api-key": gemini_key},
                )
            if resp.status_code != 200:
                failed += 1
                continue

            label = (
                resp.json().get("candidates", [{}])[0]
                .get("content", {}).get("parts", [{}])[0]
                .get("text", "").strip()[:80]
            )
            if not label:
                failed += 1
                continue

            # Embed the label
            from memory.vault import _embed_one
            vec = await _embed_one(label)
            update = {"$set": {"emoji_hint": label}}
            if vec:
                update["$set"]["embedding"] = vec
            await db["sticker_pack"].update_one(
                {"_id": sticker["_id"]}, update
            )
            labeled += 1

        except Exception as e:
            logger.warning(f"Backfill label failed for {sticker.get('sticker_id')}: {e}")
            failed += 1

    return {
        "status": "ok",
        "labeled": labeled,
        "failed": failed,
        "total": len(stickers),
    }
async def update_sticker_label(
    sticker_id: str,
    token: str = Header(..., alias="x-engineer-token"),
    emoji_hint: str = None,
):
    """Manually update the emoji_hint label on a sticker and re-embed it."""
    _verify_engineer(token)
    if not emoji_hint:
        return {"status": "error", "reason": "emoji_hint is required"}

    from bootstrap import get
    db = get("db")
    user = await db["users"].find_one({})
    if not user:
        return {"status": "error", "reason": "No user found"}
    user_id = str(user["_id"])

    # Re-embed the new label
    vector = None
    try:
        from memory.vault import _embed_one
        vector = await _embed_one(emoji_hint)
    except Exception:
        pass

    update = {"$set": {"emoji_hint": emoji_hint}}
    if vector:
        update["$set"]["embedding"] = vector

    res = await db["sticker_pack"].update_one(
        {"user_id": user_id, "sticker_id": sticker_id},
        update,
    )
    if res.matched_count == 0:
        return {"status": "error", "reason": f"Sticker '{sticker_id}' not found"}

    return {
        "status": "ok",
        "sticker_id": sticker_id,
        "emoji_hint": emoji_hint,
        "re_embedded": bool(vector),
    }
