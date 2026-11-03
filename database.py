"""
database.py — OADSON V3 Database.

Motor async MongoDB. Called once by bootstrap.
Nothing else imports the client directly — use get("db") from bootstrap.

Now backed by core/db_router.py's MultiClusterDB instead of a single
AsyncIOMotorDatabase — splits the two fastest-growing collections
(ai_sessions, file_sessions) onto their own free-tier clusters so
neither can fill up the 512MB cap and take the rest of the app down
with it. Every db["collection"] call site elsewhere in the codebase is
completely unaffected — MultiClusterDB implements the same
__getitem__ interface, so nothing else needed to change. Works with
just MONGODB_URI set (zero behavior change from before); add
MONGODB_URI_CHAT / MONGODB_URI_STORAGE to actually start using
additional clusters.
"""

import os
import logging
from core.db_router import MultiClusterDB

logger = logging.getLogger("oadson.database")

if not os.getenv("MONGODB_URI"):
    raise RuntimeError("MONGODB_URI not set.")

db = MultiClusterDB()


async def create_indexes() -> None:

    # USERS
    await db["users"].create_index([("email", 1)], unique=True, sparse=True)
    await db["users"].create_index([("created_at", -1)])

    # SESSIONS (auth tokens)
    await db["sessions"].create_index([("session_token", 1)], unique=True, sparse=True)
    await db["sessions"].create_index([("user_id", 1)])
    await db["sessions"].create_index([("expires_at", 1)])

    # AI_SESSIONS (conversation history) — lives on the "chat" cluster
    # once MONGODB_URI_CHAT is set (core/db_router.py), core cluster
    # until then. Index calls are unaffected either way.
    await db["ai_sessions"].create_index(
        [("session_id", 1), ("user_id", 1)], unique=True
    )
    await db["ai_sessions"].create_index([("user_id", 1)])
    await db["ai_sessions"].create_index([("updated_at", -1)])
    await db["ai_sessions"].create_index([("user_id", 1), ("surface_origin", 1)])

    # MEMORY
    await db["memory"].create_index(
        [("user_id", 1), ("key", 1)], unique=True, sparse=True
    )
    await db["memory"].create_index([("user_id", 1)])
    await db["memory"].create_index([("user_id", 1), ("type", 1)])
    await db["memory"].create_index([("user_id", 1), ("category", 1)])
    await db["memory"].create_index([("updated_at", -1)])

    # FILES
    await db["files"].create_index([("user_id", 1)])
    await db["files"].create_index([("user_id", 1), ("created_at", -1)])

    # FILE_SESSIONS (base64 file content) — lives on the "storage"
    # cluster once MONGODB_URI_STORAGE is set, core cluster until then.
    await db["file_sessions"].create_index([("session_id", 1), ("filename", 1)])

    # JOBS
    await db["jobs"].create_index([("job_id", 1)], unique=True)
    await db["jobs"].create_index([("user_id", 1), ("status", 1)])
    await db["jobs"].create_index([("created_at", -1)])

    # SKILL EXECUTIONS (event log)
    await db["skill_executions"].create_index([("user_id", 1)])
    await db["skill_executions"].create_index([("skill", 1)])
    await db["skill_executions"].create_index([("timestamp", -1)])
    await db["skill_executions"].create_index([("session_id", 1)])
    # WHATSAPP MESSAGES
    await db["whatsapp_processed_messages"].create_index("created_at", expireAfterSeconds=86400)

    # ÌLÀNÀ — process state, one doc per (user_id, session_id)
    await db["ilana_state"].create_index(
        [("user_id", 1), ("session_id", 1)], unique=True
    )
    await db["ilana_state"].create_index([("user_id", 1)])
    logger.info("✅ OADSON V3 indexes created.")


async def init_db() -> None:
    try:
        # verify_clusters() (core/db_router.py) is the graceful-degradation
        # check: only the CORE cluster failing is fatal. A secondary
        # cluster (chat/storage) that fails gets logged and demoted out
        # of active routing instead of raising and killing the whole app.
        # (Previously called db.ping_all() directly here and raised on
        # ANY cluster failing — see PATCH_NOTES.md. That method still
        # existing on MultiClusterDB, unused, was the bug: the fix was
        # written but never actually wired in here.)
        await db.verify_clusters()
        await create_indexes()
    except Exception as e:
        logger.error(f"Database init failed: {e}")
        raise


async def close_db() -> None:
    db.close_all()
    logger.info("🛑 MongoDB connections closed.")
