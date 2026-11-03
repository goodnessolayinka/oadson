"""
core/spend_tracker.py — spend ledger and hard caps for PAID providers only.

Free providers are never recorded here. Every paid call is priced from the
response's real token usage, added to the ledger, and refused BEFORE the
request if it would breach a cap (lifetime or per UTC day).

STORAGE
  - Production: MongoDB collection `spend_ledger`, one doc per
    "<provider>:total" and "<provider>:<YYYY-MM-DD>", written with $inc so a
    restart can't reset the total. Attached at startup by bootstrap via attach().
  - Fallback (no db attached, e.g. local dev): .oadson/spend_ledger.json
    or SPEND_LEDGER_PATH.
  Reads are synchronous and come from an in-memory mirror, because the cap
  check runs on the sync path in model_policy.auto_model_for().

PRICING is conservative on purpose: DeepSeek always at PEAK rates, and
Anthropic's long-prompt tier applies above 100K input tokens.
"""
import datetime
import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("oadson.spend_tracker")

_LOCK = threading.Lock()
_DEFAULT_PATH = Path(__file__).resolve().parent.parent / ".oadson" / "spend_ledger.json"
LEDGER_PATH = Path(os.getenv("SPEND_LEDGER_PATH", str(_DEFAULT_PATH)))
COLLECTION = "spend_ledger"

# $5 for two weeks ~= $0.35/day. Anthropic: $4.50 lifetime (keeps a $0.50
# margin under the $5 you're spending) and $0.35/day. DeepSeek is a separate
# $2 balance, capped below its balance.
CAPS = {
    "anthropic": {
        "total": float(os.getenv("ANTHROPIC_TOTAL_CAP_USD", "4.50")),
        "daily": float(os.getenv("ANTHROPIC_DAILY_CAP_USD", "0.35")),
    },
    "engineer": {
        "total": float(os.getenv("ENGINEER_TOTAL_CAP_USD", "2.00")),
        "daily": float(os.getenv("ENGINEER_DAILY_CAP_USD", "1.00")),
    },
    "deepseek": {
        "total": float(os.getenv("DEEPSEEK_TOTAL_CAP_USD", "1.80")),
        "daily": float(os.getenv("DEEPSEEK_DAILY_CAP_USD", "0.50")),
    },
    # CoreWeave: the user's $50 academic credit. The total cap stays under the
    # credit so OADSON's own ledger is never the thing that runs it dry.
    # Daily $5 keeps one busy day from using up the whole total.
    "coreweave": {
        "total": float(os.getenv("COREWEAVE_TOTAL_CAP_USD", "45.00")),
        "daily": float(os.getenv("COREWEAVE_DAILY_CAP_USD", "5.00")),
    },
    # The backup CoreWeave key (COREWEAVE_API_KEY_2), from a second account with its own credit.
    "coreweave_2": {
        "total": float(os.getenv("COREWEAVE_2_TOTAL_CAP_USD", "45.00")),
        "daily": float(os.getenv("COREWEAVE_2_DAILY_CAP_USD", "5.00")),
    },
}

_ANTHROPIC_PRICES = {"short": (0.10, 0.50), "long": (0.50, 2.50)}  # (in, out) per 1M
_DEEPSEEK_PEAK = {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20}
# CoreWeave list prices per 1M tokens (in, out), from
# coreweave.com/forge-pricing/inference-agents, checked 2026-11-01.
# Input is billed at the full rate here: no cache discount is assumed.
# Any CoreWeave model missing from this table falls back to _FALLBACK_PRICE.
_COREWEAVE_PRICES = {
    "deepseek-ai/DeepSeek-V4.1-Flash": (0.20, 0.65),
    "zai-org/GLM-5.3-Flash": (0.15, 0.50),
    "openai/gpt-oss-20b": (0.03, 0.13),
    "Qwen/Qwen3.6-35B-A3B": (0.25, 1.25),
    "meta-llama/Llama-3.3-70B-Instruct": (0.71, 0.71),
    "meta-llama/Llama-3.1-8B-Instruct": (0.22, 0.22),
}
_FALLBACK_PRICE = (0.50, 2.50)

_db = None
_mirror = {"total": {}, "daily": {}}  # {"total": {provider: usd}, "daily": {date: {provider: usd}}}


def cost_usd(provider: str, model: str, usage: dict) -> float:
    usage = usage or {}
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    if provider in ("anthropic", "engineer"):
        inp, out = _ANTHROPIC_PRICES["long" if pt > 100_000 else "short"]
        cr = int(usage.get("cache_read_tokens") or 0)    # 10% of input
        cw = int(usage.get("cache_write_tokens") or 0)   # 125% of input
        uncached = max(pt - cr - cw, 0)
        return (uncached * inp + cw * inp * 1.25 + cr * inp * 0.10 + ct * out) / 1_000_000
    if provider == "deepseek":
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or max(pt - hit, 0))
        return (hit * _DEEPSEEK_PEAK["cache_hit"] + miss * _DEEPSEEK_PEAK["cache_miss"]
                + ct * _DEEPSEEK_PEAK["output"]) / 1_000_000
    if provider in ("coreweave", "coreweave_2"):
        inp, out = _COREWEAVE_PRICES.get(model, _FALLBACK_PRICE)
        return (pt * inp + ct * out) / 1_000_000
    inp, out = _FALLBACK_PRICE
    return (pt * inp + ct * out) / 1_000_000


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _add_to_mirror(provider: str, usd: float) -> None:
    day = _today()
    _mirror["total"][provider] = _mirror["total"].get(provider, 0.0) + usd
    d = _mirror["daily"].setdefault(day, {})
    d[provider] = d.get(provider, 0.0) + usd
    for old in sorted(_mirror["daily"])[:-14]:
        del _mirror["daily"][old]


def _load_file() -> dict:
    try:
        return json.loads(LEDGER_PATH.read_text())
    except Exception:
        return {}


async def attach(db) -> None:
    """Called from bootstrap once Mongo is up. Loads totals into the mirror."""
    global _db
    _db = db
    try:
        coll = db[COLLECTION]
        async for doc in coll.find({}):
            _id = doc.get("_id", "")
            provider, _, key = _id.partition(":")
            usd = float(doc.get("usd", 0.0))
            if key == "total":
                _mirror["total"][provider] = usd
            elif key:
                _mirror["daily"].setdefault(key, {})[provider] = usd
        logger.info(f"spend ledger attached to Mongo: {summary()}")
    except Exception as e:
        logger.error(f"spend ledger could not load from Mongo, using in-memory only: {e}")


def _bootstrap_from_file_once() -> None:
    if _db is None and not _mirror["total"]:
        d = _load_file()
        for p, usd in d.get("total", {}).items():
            _mirror["total"][p] = float(usd)
        for day, providers in d.get("daily", {}).items():
            _mirror["daily"][day] = {p: float(u) for p, u in providers.items()}


_bootstrap_from_file_once()


def spent(provider: str) -> tuple[float, float]:
    total = _mirror["total"].get(provider, 0.0)
    today = _mirror["daily"].get(_today(), {}).get(provider, 0.0)
    return total, today


def can_spend(provider: str, est_usd: float = 0.0) -> bool:
    caps = CAPS.get(provider)
    if caps is None:
        return True
    total, today = spent(provider)
    return (total + est_usd <= caps["total"]) and (today + est_usd <= caps["daily"])


async def record(provider: str, model: str, usage: dict) -> float:
    usd = cost_usd(provider, model, usage)
    with _LOCK:
        _add_to_mirror(provider, usd)
    day = _today()
    if _db is not None:
        try:
            coll = _db[COLLECTION]
            await coll.update_one({"_id": f"{provider}:total"}, {"$inc": {"usd": usd}}, upsert=True)
            await coll.update_one({"_id": f"{provider}:{day}"}, {"$inc": {"usd": usd}}, upsert=True)
        except Exception as e:
            logger.error(f"spend write to Mongo failed (kept in memory): {e}")
    else:
        _write_file()
    return usd


def _write_file() -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_mirror, indent=2))
    os.replace(tmp, LEDGER_PATH)


def summary() -> dict:
    out = {}
    for p, caps in CAPS.items():
        total, today = spent(p)
        out[p] = {"total_usd": round(total, 4), "total_cap_usd": caps["total"],
                  "today_usd": round(today, 4), "daily_cap_usd": caps["daily"]}
    return out
