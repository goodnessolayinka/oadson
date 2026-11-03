"""
core/db_router.py — OADSON V3 Multi-Cluster Database Router.

Splits collections across multiple free-tier (512MB) MongoDB Atlas
clusters instead of being boxed in by one. Drop-in replacement for the
single AsyncIOMotorDatabase object everywhere `db["collection"]` is
used — nothing else in the codebase needs to change; db["users"],
db["memory"], etc. all keep working exactly as before, just
transparently routed to the right cluster underneath.

Why THIS split specifically: ai_sessions (full chat history across
every surface — web, WhatsApp, Telegram) and file_sessions (base64 file
content — everything is inline storage now, see api/files.py's
_upload_inline) are the two fastest-growing collections by a wide
margin, and neither has a natural size cap.
Isolating them onto their own clusters means one of them filling up
doesn't take auth, memory, or settings down with it on a shared "core"
cluster.

Adding a cluster: set its connection string as a new env var, add it to
CLUSTER_ENV_VARS, and list any collections that should live there in
COLLECTION_MAP. Anything not explicitly mapped defaults to "core".
Works fine with ONLY MONGODB_URI set (today's single-cluster setup,
zero behavior change) and upgrades automatically the moment the others
are added — no code changes needed to adopt a new cluster once it's
configured here.
"""

import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger("oadson.db_router")

# cluster name -> env var holding its connection string.
CLUSTER_ENV_VARS = {
    "core": "MONGODB_URI",             # existing var, unchanged
    "chat": "MONGODB_URI_CHAT",        # new, optional
    "storage": "MONGODB_URI_STORAGE",  # new, optional
}

# collection name -> cluster name. Anything NOT listed here uses "core".
COLLECTION_MAP = {
    "ai_sessions": "chat",
    "file_sessions": "storage",
}

DEFAULT_CLUSTER = "core"
DB_NAME = "oadson_v3"


class MultiClusterDB:
    """
    Drop-in replacement for a single AsyncIOMotorDatabase.
    db["collection_name"] works exactly as before; which physical
    cluster actually serves that collection is decided here, once, at
    startup — callers never need to know or care.
    """

    def __init__(self):
        self._clients = {}
        self._dbs = {}
        self._configured_clusters = set()

        for cluster_name, env_var in CLUSTER_ENV_VARS.items():
            uri = os.getenv(env_var)
            if uri:
                client = AsyncIOMotorClient(uri)
                self._clients[cluster_name] = client
                self._dbs[cluster_name] = client[DB_NAME]
                self._configured_clusters.add(cluster_name)
            elif cluster_name != DEFAULT_CLUSTER:
                logger.info(
                    f"{env_var} not set — collections mapped to '{cluster_name}' "
                    f"will use the '{DEFAULT_CLUSTER}' cluster instead until it is."
                )

        if DEFAULT_CLUSTER not in self._dbs:
            raise RuntimeError(f"{CLUSTER_ENV_VARS[DEFAULT_CLUSTER]} not set — the core cluster is required.")

        configured = ", ".join(sorted(self._configured_clusters))
        logger.info(f"MultiClusterDB ready — configured clusters: {configured}")

    def _cluster_for(self, collection_name: str) -> str:
        wanted = COLLECTION_MAP.get(collection_name, DEFAULT_CLUSTER)
        # Falls back to core if the target cluster's URI isn't set yet —
        # lets clusters be added one at a time without anything breaking
        # in between, and means this whole system is a no-op (100% core
        # cluster, today's exact behavior) until MONGODB_URI_CHAT /
        # MONGODB_URI_STORAGE actually get set.
        return wanted if wanted in self._configured_clusters else DEFAULT_CLUSTER

    def __getitem__(self, collection_name: str):
        cluster = self._cluster_for(collection_name)
        return self._dbs[cluster][collection_name]

    async def command(self, *args, **kwargs):
        """Existing db.command('ping') health-check call sites (main.py, api/engineer.py) — runs against the core cluster."""
        return await self._dbs[DEFAULT_CLUSTER].command(*args, **kwargs)

    async def ping_all(self) -> dict:
        """Per-cluster health status — richer than the single db.command('ping') check, since a multi-cluster setup can have ONE cluster down while the others are fine."""
        results = {}
        for cluster_name, client in self._clients.items():
            try:
                await client.admin.command("ping")
                results[cluster_name] = "ok"
            except Exception as e:
                results[cluster_name] = f"error: {e}"
        return results

    async def verify_clusters(self) -> None:
        """
        Call once at startup. Only the CORE cluster failing is fatal —
        it holds users/auth/memory, the app genuinely can't run without
        it. A secondary cluster (chat/storage) failing (bad auth, wrong
        URI, IP not whitelisted, etc.) gets logged loudly and DEMOTED out
        of _configured_clusters instead of crashing the whole app —
        _cluster_for() already falls back to core for anything mapped to
        a cluster that isn't in that set, so this just makes that
        fallback trigger for a cluster that's configured-but-broken, not
        only for one that was never configured at all.

        Previously any cluster failing here raised, which meant a typo'd
        password on a cluster used only for file uploads could take down
        chat, WhatsApp, and everything else too.
        """
        statuses = await self.ping_all()

        core_status = statuses.get(DEFAULT_CLUSTER, "error: not configured")
        if core_status != "ok":
            raise RuntimeError(f"Core cluster ('{DEFAULT_CLUSTER}') failed: {core_status} — the app cannot start without it.")
        logger.info(f"✅ Core cluster connected ({DEFAULT_CLUSTER})")

        for cluster_name, status in statuses.items():
            if cluster_name == DEFAULT_CLUSTER:
                continue
            if status == "ok":
                logger.info(f"✅ Secondary cluster connected ({cluster_name})")
            else:
                logger.error(
                    f"⚠️ Secondary cluster '{cluster_name}' failed: {status} — "
                    f"its collections will use '{DEFAULT_CLUSTER}' instead until this is fixed. "
                    f"App is starting anyway."
                )
                self._configured_clusters.discard(cluster_name)

    def close_all(self) -> None:
        for client in self._clients.values():
            client.close()
