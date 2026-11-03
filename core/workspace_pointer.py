"""
Which workspace folder a user is working in (an upload or a clone on the worker).
Owner-side pointer only. The files and their index live on the worker.
"""
from core import remote_jobs as _rj

COLLECTION = "workspace_pointer"
_mem: dict = {}


async def set_active(user_id: str, folder: str) -> None:
    _mem[str(user_id)] = folder
    db = _rj.jobs.db
    if db is not None:
        try:
            await db[COLLECTION].update_one({"_id": str(user_id)}, {"$set": {"folder": folder}},
                                            upsert=True)
        except Exception:
            pass


async def get_active(user_id: str):
    uid = str(user_id)
    if uid in _mem:
        return _mem[uid]
    db = _rj.jobs.db
    if db is None:
        return None
    try:
        doc = await db[COLLECTION].find_one({"_id": uid})
    except Exception:
        return None
    if doc and doc.get("folder"):
        _mem[uid] = doc["folder"]
        return doc["folder"]
    return None
