"""
Asks Render to restart the worker, once per outage.

The owner calls note_unreachable() when the worker stops answering (a crash,
usually an out-of-memory kill). The first such call in an outage asks Render
for a restart. note_reachable() ends the outage, so the next one can ask again.
There is no timer: a restart is asked for only when the worker is really down,
never because it is merely busy or low on memory.

Needs two optional owner env vars:
  RENDER_API_KEY              from Render: Account Settings > API Keys
  RENDER_WORKER_SERVICE_ID    the worker's id, the srv-... part of its dashboard URL

Without them nothing happens here, and Render's own restart applies.
"""
import logging
import os

import httpx

logger = logging.getLogger("oadson.worker_restart")

_outage = {"requested": False}


def configured() -> bool:
    return bool(os.getenv("RENDER_API_KEY", "").strip() and os.getenv("RENDER_WORKER_SERVICE_ID", "").strip())


def note_reachable() -> None:
    _outage["requested"] = False


async def note_unreachable(transport=None) -> bool:
    """True when a restart was requested for this outage. Never raises."""
    if _outage["requested"] or not configured():
        return False
    _outage["requested"] = True  # one request per outage, even if Render refuses it
    service = os.getenv("RENDER_WORKER_SERVICE_ID", "").strip()
    key = os.getenv("RENDER_API_KEY", "").strip()
    try:
        async with httpx.AsyncClient(transport=transport, timeout=15) as client:
            r = await client.post(f"https://api.render.com/v1/services/{service}/restart",
                                  headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as e:
        logger.warning("could not reach Render to restart the worker: %s", type(e).__name__)
        return False
    if r.status_code in (200, 201, 202, 204):
        logger.warning("worker is down; asked Render to restart it")
        return True
    logger.warning("Render refused the worker restart: HTTP %d %s", r.status_code, r.text[:160])
    return False
