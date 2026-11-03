"""
main.py — OADSON V3.

Slim. Startup delegates to bootstrap.
Routers delegate to orchestrator.
Nothing lives here except wiring.
"""

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("oadson.main")

# SECURITY FIX 2026-08-14: httpx's own internal logger logs every
# outbound request at INFO level, full URL included — query params and
# all. Several skills/core modules (Gemini calls, provider_router,
# provider_manager) put API keys directly in the URL as ?key=... rather
# than a header, since that's how Gemini's REST API is designed. With
# the root logger at INFO and no override, every one of those keys was
# landing in plaintext in Render's logs on every single request — a
# real live secret leaking continuously, not a one-off. httpx itself
# has no way to redact the key; the only place this can be fixed is by
# not logging httpx's request lines at INFO at all. Raising httpx (and
# httpcore, which it wraps) to WARNING stops the per-request log line
# entirely while leaving actual httpx warnings/errors visible, and
# doesn't touch the level of any of OADSON's own loggers.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from bootstrap import bootstrap, teardown
    await bootstrap()
    yield
    await teardown()


from api.biometrics import router as biometrics_router

app = FastAPI(
    title="OADSON V3",
    version="3.0.0",
    description="AI Operating System. One brain. Every surface.",
    lifespan=lifespan,
)

# ── CORS ──
ALLOWED_ORIGINS = [
    os.getenv("FRONTEND_BASE_URL", "http://localhost:3000"),
    "https://testforsigninwothgoogle.netlify.app",
    "http://localhost:3000",
    "http://127.0.0.1:5500",
    "http://127.0.0.1:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── ENGINEER PING: quiet + rate-limited ─────────────────────────────
# ADDED 2026-08-31: /engineer/ping (api/engineer.py) is deliberately
# cheap on its OWN — no DB call, no allocation — but it was being
# polled every ~5 seconds continuously the whole time the engineer
# screen stayed open (confirmed directly in production logs: a steady
# stream of "GET /api/v3/engineer/ping" lines every 5s for the entire
# session length, sometimes hours). Two real, compounding costs from
# that sustained rate, neither fixable inside the ping handler itself:
#   1. uvicorn's default access logger formats and emits a full log
#      line for EVERY one of those requests, indefinitely, for as long
#      as a tab/screen is left open — real, continuous string
#      allocation and I/O on a 512MB instance, from a route whose only
#      job is a latency check nobody is reading thousands of times.
#   2. Nothing on the server enforced any minimum interval between
#      pings — a client that never clears its polling interval (tab
#      left open, navigated away without cleanup) could poll as fast
#      as it wanted, forever, with no backstop.
# This fixes both, scoped to exactly this one route so nothing else's
# logging or timing changes.
#
# BUGFIX during review: the first version of the log-suppression half
# toggled logging.getLogger("uvicorn.access")'s level down and back up
# around each ping request inside the middleware. That's genuinely
# unsafe under real concurrency — the logger's level is global,
# process-wide state; a DIFFERENT request's access-log line landing
# while a ping request had it lowered would get silently swallowed
# too, and multiple concurrent pings racing to restore "the" prior
# level could leave it stuck wrong. A logging.Filter that inspects each
# record's own path and only drops matching ones has no such race —
# every record is judged independently, nothing shared gets mutated.
class _SuppressPingAccessLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn's access log record args are (client_addr, method,
        # path, http_version, status_code) — checked defensively since
        # this exact shape isn't a guaranteed public API; on any
        # mismatch, fail open (keep the log line) rather than risk
        # silently swallowing unrelated log records.
        try:
            path = record.args[2]
            return "/engineer/ping" not in str(path)
        except Exception:
            return True


logging.getLogger("uvicorn.access").addFilter(_SuppressPingAccessLog())

_ping_last_seen: dict[str, float] = {}
_PING_MIN_INTERVAL_S = 2.0  # well below the frontend's own 5s interval — only catches a runaway/misbehaving client


@app.middleware("http")
async def _engineer_ping_rate_guard(request, call_next):
    if request.url.path == "/api/v3/engineer/ping":
        import time as _time
        from fastapi.responses import JSONResponse

        client_ip = request.client.host if request.client else "unknown"
        now = _time.monotonic()
        last = _ping_last_seen.get(client_ip, 0.0)
        # Unbounded dict growth guard — same "don't let a rare thing
        # become an unbounded thing" posture as elsewhere in this
        # codebase (see core/tool_loop.py's CONTENTS_SIZE_GUARD_BYTES).
        # A real deployment sees a handful of distinct client IPs, not
        # thousands, but capping here costs nothing and prevents this
        # dict from ever becoming its own slow leak.
        if len(_ping_last_seen) > 500:
            _ping_last_seen.clear()
        _ping_last_seen[client_ip] = now

        if now - last < _PING_MIN_INTERVAL_S:
            return JSONResponse(
                status_code=429,
                content={"status": "rate_limited", "retry_after_ms": int((_PING_MIN_INTERVAL_S - (now - last)) * 1000)},
            )

    return await call_next(request)


# ── ROUTERS ──
from api.chat import router as chat_router
from api.auth import router as auth_router
from api.sessions import router as sessions_router
from api.memory import router as memory_router
from api.skills import router as skills_router
from api.models import router as models_router
from api.files import router as files_router
from api.remote_workspace import router as remote_workspace_router
from api.whatsapp import router as whatsapp_router
from api.telegram import router as telegram_router
from api.engineer import router as engineer_router
from api.serve import router as serve_router
from api.chat_stream import router as chat_stream_router
from api.jobs import router as jobs_router
from api.settings import router as settings_router
from api.analytics import router as analytics_router
from api.voice import router as voice_router

app.include_router(chat_router,     prefix="/api/v3", tags=["Chat"])
app.include_router(chat_stream_router, prefix="/api/v3", tags=["Chat"])
app.include_router(jobs_router,     prefix="/api/v3", tags=["Jobs"])
app.include_router(auth_router,     prefix="/api/v3", tags=["Auth"])
app.include_router(sessions_router, prefix="/api/v3", tags=["Sessions"])
app.include_router(memory_router,   prefix="/api/v3", tags=["Memory"])
app.include_router(skills_router,   prefix="/api/v3", tags=["Skills"])
app.include_router(models_router,   prefix="/api/v3", tags=["Models"])
app.include_router(files_router,    prefix="/api/v3", tags=["Files"])
app.include_router(remote_workspace_router, prefix="/api/v3", tags=["RemoteWorkspace"])
app.include_router(whatsapp_router, prefix="/api/v3", tags=["WhatsApp"])
app.include_router(telegram_router, prefix="/api/v3", tags=["Telegram"])
app.include_router(engineer_router, prefix="/api/v3", tags=["Engineer"])
app.include_router(biometrics_router, prefix="/api/v3", tags=["Biometrics"])
# WORKER LINK — signed result callbacks from workers. Root path on purpose (see api/worker_callback.py).
from api.worker_callback import router as worker_callback_router  # noqa: E402
app.include_router(worker_callback_router)
app.include_router(serve_router,    prefix="/api/v3", tags=["Files"])
app.include_router(settings_router, prefix="/api/v3", tags=["Settings"])
app.include_router(analytics_router, prefix="/api/v3", tags=["Analytics"])
# ADDED 2026-11-02 — voice endpoints for the mobile app: POST /tts,
# POST /stt, POST /speech-profile/correction. See api/voice.py.
app.include_router(voice_router,     prefix="/api/v3", tags=["Voice"])


# ── SWAGGER BEARER AUTH ──
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
    }
    for path in schema.get("paths", {}).values():
        for method in path.values():
            method.setdefault("security", [{"BearerAuth": []}])
    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi


# ── HEALTH ──
@app.get("/health", tags=["default"], summary="Basic liveness check")
@app.head("/health", tags=["default"])
async def health():
    from datetime import datetime
    # Fire-and-forget. If you set up an external keep-alive ping (e.g. a
    # free cron service hitting this every 10 min to counter Render's
    # 15-min spin-down), it does double duty: keeps the service warm AND
    # advances the background job queue. See core/job_queue.py.
    try:
        import asyncio
        from bootstrap import get
        from core.job_queue import kick_job_queue
        asyncio.create_task(kick_job_queue(get("db"), get("orchestrator")))
    except Exception:
        pass  # health check must never fail because of this
    return {"status": "alive", "version": "3.0.0", "timestamp": datetime.utcnow().isoformat()}


@app.get(
    "/health/diagnostics",
    tags=["default"],
    summary="Full diagnostics",
    description=(
        "Checks the things that actually determine whether OADSON can serve "
        "a request right now: database connectivity, whether an AI provider "
        "key is configured, how many skills loaded successfully, and whether "
        "the event bus (used by /chat/stream) is up. Unlike /health, this "
        "one can return 200 with `\"status\": \"degraded\"` — check the body, "
        "not just the status code."
    ),
)
async def health_diagnostics():
    from datetime import datetime
    from bootstrap import get

    result = {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "checks": {},
    }

    # Database — per-cluster now that there can be more than one
    # (core/db_router.py). A single cluster failing while others are
    # fine should show as "degraded", not be hidden behind one flag.
    try:
        db = get("db")
        cluster_statuses = await db.ping_all()
        all_ok = all(v == "ok" for v in cluster_statuses.values())
        result["checks"]["database"] = {"status": "ok" if all_ok else "error", "clusters": cluster_statuses}
        if not all_ok:
            result["status"] = "degraded"
    except Exception as e:
        result["checks"]["database"] = {"status": "error", "detail": str(e)}
        result["status"] = "degraded"

    # AI provider — confirm a key is actually configured, not that it works
    # (an actual generateContent call here would cost quota on every health
    # check, which defeats the point).
    gemini_configured = bool(os.getenv("GEMINI_API_KEY"))
    openrouter_configured = bool(os.getenv("OPENROUTER_API_KEY"))
    groq_configured = bool(os.getenv("GROQ_API_KEY"))
    cerebras_configured = bool(os.getenv("CEREBRAS_API_KEY"))
    result["checks"]["ai_provider"] = {
        "status": "ok" if gemini_configured else "error",
        "gemini_configured": gemini_configured,
        "groq_configured": groq_configured,
        "cerebras_configured": cerebras_configured,
        "openrouter_configured": openrouter_configured,
    }
    if not gemini_configured:
        result["status"] = "degraded"

    # Skills
    try:
        registry = get("skill_registry")
        skill_count = registry.count()
        result["checks"]["skills"] = {"status": "ok", "loaded": skill_count}
        if skill_count == 0:
            result["checks"]["skills"]["status"] = "warning"
            result["status"] = "degraded"
    except Exception as e:
        result["checks"]["skills"] = {"status": "error", "detail": str(e)}
        result["status"] = "degraded"

    # Event bus (chat_stream depends on this existing)
    try:
        get("event_bus")
        result["checks"]["event_bus"] = {"status": "ok"}
    except Exception as e:
        result["checks"]["event_bus"] = {"status": "error", "detail": str(e)}
        result["status"] = "degraded"

    return result


@app.get("/", tags=["default"])
async def root():
    from bootstrap import get
    try:
        registry = get("skill_registry")
        skill_count = registry.count()
    except Exception:
        skill_count = 0
    return {
        "name": "OADSON V3",
        "status": "online",
        "skills": skill_count,
        "surfaces": ["web", "app", "terminal", "whatsapp", "pwa"],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=False,
    )
