"""
bootstrap.py — OADSON V3 startup.

One place. One moment. Everything initializes here.
Nothing else owns startup logic.

Responsibilities:
- Database init
- Single SkillsManager instantiation (shared via registry)
- Plugin loading
- Service registry population
- Event bus startup

Import order matters. Don't touch it.
"""

import os
import logging
from typing import Any

logger = logging.getLogger("oadson.bootstrap")


# ── SERVICE REGISTRY ──
# One dict. Entire app reads from here.
# Nothing instantiates its own copies.
_registry: dict[str, Any] = {}


def register(name: str, instance: Any) -> None:
    if name in _registry:
        logger.warning(f"Service '{name}' already registered — overwriting.")
    _registry[name] = instance
    logger.info(f"✅ Registered service: {name}")


def get(name: str) -> Any:
    if name not in _registry:
        raise RuntimeError(
            f"Service '{name}' not found in registry. "
            f"Was bootstrap() called before this? Available: {list(_registry.keys())}"
        )
    return _registry[name]


def has(name: str) -> bool:
    return name in _registry


# ── BOOTSTRAP ──

async def bootstrap() -> None:
    """
    Call once at application startup inside FastAPI lifespan.
    Order is intentional — do not reorder.
    """
    logger.info("🚀 OADSON V3 bootstrap starting...")

    # 1. Database
    await _init_database()

    # 2. Event bus
    _init_event_bus()

    # 3. Skills (one instance, shared everywhere)
    # Re-materialize any skills previously created via skill_creator —
    # Render's filesystem doesn't survive a restart/redeploy, Mongo does.
    await _rematerialize_generated_skills()
    _init_skills()

    # 4. Memory manager
    _init_memory()

    # 5. Provider manager (Gemini + OpenRouter)
    _init_providers()

    # 6. Orchestrator (depends on all of the above)
    _init_orchestrator()

    # 7. Job queue worker loop
    _init_job_queue()

    # 8. Proactive WhatsApp agent — autonomous outreach, AI-driven
    _init_proactive_agent()

    # 8b. Reminder delivery agent — ADDED 2026-10-01, deliberately
    # separate from the proactive agent above: fast-polling (60s, not
    # 30min), no daily cap, no hard-sleep window. An explicit @reminder
    # the user set is a promise to keep, not a discretionary check-in
    # subject to the same "don't be annoying" gates as unprompted
    # messages — see core/reminder_agent.py's module docstring.
    _init_reminder_agent()

    # 9. Feature/env-var sanity check — see _check_feature_env_vars' own
    # docstring for the actual incident this exists to prevent.
    _check_feature_env_vars()

    logger.info("✅ OADSON V3 bootstrap complete.")
    _log_registry_summary()


async def _init_database() -> None:
    from database import init_db
    await init_db()
    from database import db
    register("db", db)

    # SPEND LEDGER — load paid-provider totals from Mongo so caps survive restarts.
    from core import spend_tracker
    await spend_tracker.attach(db)
    from core import remote_jobs as _remote_jobs
    _remote_jobs.jobs.configure(db)

    # MEMORY MONITORING 2026-08-28 — one-time setup of the capped,
    # crash-surviving memory_events collection (see
    # core/oadson_home.py's log_memory_event/ensure_memory_events_capped
    # for what writes to it and why it's capped). No-ops after the
    # first successful run ever, on any deploy — Mongo won't let an
    # existing collection be re-capped, and that's exactly what's
    # wanted here.
    from core.oadson_home import ensure_memory_events_capped
    await ensure_memory_events_capped(db)


def _init_event_bus() -> None:
    from core.event_bus import EventBus
    from core.event_handlers import register_all
    bus = EventBus()
    register_all(bus)
    register("event_bus", bus)


async def _rematerialize_generated_skills() -> None:
    from core.skill_creator import rematerialize_generated_skills
    skills_dir = os.getenv("SKILLS_DIR", "skills")
    await rematerialize_generated_skills(get("db"), skills_dir)


def _init_skills() -> None:
    from core.skill_registry import SkillRegistry
    from core.skill_loader import SkillLoader
    from core.oadson_home import current_boot_id

    # RESTART RESILIENCE 2026-08-28: log this process's boot id once at
    # startup — see core/oadson_home.py's needs_reclone()/mark_fresh().
    # Purely informational here (the id is generated at module import
    # time regardless), but makes it possible to correlate "workspace
    # was reset" behavior in skill logs back to an actual restart event
    # in the deploy logs.
    logger.info(f"Workspace boot id for this process: {current_boot_id()}")

    skills_dir = os.getenv("SKILLS_DIR", "skills")
    registry = SkillRegistry()
    loader = SkillLoader(skills_dir=skills_dir, registry=registry)
    loader.load_all()

    register("skill_registry", registry)
    register("skill_loader", loader)
    logger.info(f"✅ Skills loaded: {registry.count()}")


def _init_memory() -> None:
    from memory.manager import MemoryManager
    db = get("db")
    manager = MemoryManager(db=db)
    register("memory_manager", manager)


def _init_providers() -> None:
    from core.provider_manager import ProviderManager
    manager = ProviderManager()
    register("provider_manager", manager)


def _init_orchestrator() -> None:
    from core.skill_executor import SkillExecutor
    from core.orchestrator import AIOrchestrator

    executor = SkillExecutor(registry=get("skill_registry"))
    register("skill_executor", executor)

    orchestrator = AIOrchestrator(
        db=get("db"),
        skill_registry=get("skill_registry"),
        skill_executor=executor,
        memory_manager=get("memory_manager"),
        provider_manager=get("provider_manager"),
        event_bus=get("event_bus"),
    )
    register("orchestrator", orchestrator)


def _init_job_queue() -> None:
    from core.job_queue import start_worker_loop
    start_worker_loop(db=get("db"), orchestrator=get("orchestrator"))


def _init_proactive_agent() -> None:
    from core.proactive_agent import start_proactive_agent
    db = get("db")
    start_proactive_agent(lambda: db)


def _init_reminder_agent() -> None:
    from core.reminder_agent import start_reminder_agent
    db = get("db")
    start_reminder_agent(lambda: db)


def _log_registry_summary() -> None:
    logger.info(f"Registry contents: {list(_registry.keys())}")


# REGRESSION FOUND AND RE-ADDED 2026-08-28 — this whole check (added
# 2026-08-27 after push notifications shipped fully coded, UI and all,
# while ONESIGNAL_APP_ID/ONESIGNAL_REST_API_KEY didn't exist anywhere —
# not .env, not .env.example, not even a `sync: false` placeholder in
# render.yaml, so the gap was completely silent) had been reverted away
# entirely, along with core/skill_executor.py's status-normalization fix
# and core/self_test.py's dry-run test — three separate fixes reverted
# back to their exact prior state in the same pass. Restoring this one
# too: a growing registry, not push-notifications-specific — add an
# entry any time a new optional feature depends on env vars with no
# other startup-time visibility, and it gets the same "loud warning in
# Render's own already-free logs at boot" treatment automatically.
_FEATURE_ENV_REQUIREMENTS = {
    "Push notifications (OneSignal)": ["ONESIGNAL_APP_ID", "ONESIGNAL_REST_API_KEY"],
    "WhatsApp (Green API)": ["GREEN_API_INSTANCE_ID", "GREEN_API_TOKEN"],
    "Telegram": ["TELEGRAM_BOT_TOKEN"],
    "Gemini (core model provider)": ["GEMINI_API_KEY"],
    "Google OAuth (login)": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
    "Engineer diagnostics endpoints": ["OADSON_ENGINEER_TOKEN"],
}


def _check_feature_env_vars() -> None:
    for feature, required_vars in _FEATURE_ENV_REQUIREMENTS.items():
        missing = [v for v in required_vars if not os.getenv(v, "").strip()]
        if missing:
            logger.warning(
                f"⚠️  '{feature}' is configured in code but missing env var(s): "
                f"{', '.join(missing)} — this feature will silently no-op until "
                f"they're set on Render, not error, so it's easy to miss without "
                f"this warning."
            )


async def teardown() -> None:
    """Call on shutdown."""
    from database import close_db
    await close_db()
    logger.info("🛑 OADSON V3 shutdown complete.")
