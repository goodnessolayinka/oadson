"""
core/skill_reliability.py — OADSON V3 Skill Reliability Scoring.

Built 2026-08-09, item #2 ("Skill Reliability Scoring") from the
hardening plan handed off to another AI for ideas — picked as the one
reasonable item to build immediately because it needed zero new
infrastructure: db.skill_executions already exists (written by
core/event_handlers.py on every single skill call, already read by
the watchdog's error-rate check), this just does more math over the
same collection.

One real gap found while building this: no timing data existed
anywhere in the execution pipeline, so "average latency" — a field
the plan explicitly wanted — genuinely couldn't be computed. Added
minimal instrumentation (2 lines in orchestrator.py, one new field in
event_handlers.py) rather than skip it or fake it. Executions logged
before that change simply won't have duration_ms; handled as missing
data, not zero.

Deliberately NOT full "contradiction detection" or "evidence-first
gating" from the same plan — those need real NLU/verification logic
against live conversation state, not just log aggregation, and are a
much bigger, riskier lift than "the one reasonable thing to build
right now."
"""

from datetime import datetime, timedelta, timezone

# Below this many executions in a window, a trend call isn't
# statistically meaningful — matches the plan's own
# INSUFFICIENT_DATA state rather than forcing a verdict on noise.
MIN_SAMPLE_FOR_TREND = 6
TREND_DELTA_THRESHOLD = 0.15  # 15 percentage points to call it a real trend, not noise


async def _skill_stats(db, skill_name: str, since: datetime = None) -> dict:
    match = {"skill": skill_name}
    if since:
        match["timestamp"] = {"$gte": since}

    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": None,
            "total": {"$sum": 1},
            "successes": {"$sum": {"$cond": [{"$eq": ["$status", "success"]}, 1, 0]}},
            "failures": {"$sum": {"$cond": [{"$in": ["$status", ["error", "timeout", "blocked"]]}, 1, 0]}},
            "avg_duration_ms": {"$avg": "$duration_ms"},
            "last_timestamp": {"$max": "$timestamp"},
        }},
    ]
    result = await db["skill_executions"].aggregate(pipeline).to_list(length=1)
    if not result:
        return {"total": 0, "successes": 0, "failures": 0, "avg_duration_ms": None, "last_timestamp": None}
    row = result[0]
    row.pop("_id", None)
    return row


async def _last_event(db, skill_name: str, status_filter: list) -> datetime | None:
    doc = await db["skill_executions"].find_one(
        {"skill": skill_name, "status": {"$in": status_filter}},
        sort=[("timestamp", -1)],
    )
    return doc["timestamp"] if doc else None


def _trend_from_windows(recent_rate: float, prior_rate: float, recent_n: int, prior_n: int) -> str:
    if recent_n < MIN_SAMPLE_FOR_TREND or prior_n < MIN_SAMPLE_FOR_TREND:
        return "INSUFFICIENT_DATA"
    delta = recent_rate - prior_rate
    if delta >= TREND_DELTA_THRESHOLD:
        return "IMPROVING"
    if delta <= -TREND_DELTA_THRESHOLD:
        return "DEGRADING"
    return "STABLE"


async def get_skill_reliability(db, skill_name: str) -> dict:
    """Full report for one skill: overall + recent-window + trend."""
    overall = await _skill_stats(db, skill_name)
    if overall["total"] == 0:
        return {
            "skill": skill_name,
            "status": "no_data",
            "message": f"No execution history for '{skill_name}' yet.",
        }

    now = datetime.now(timezone.utc)
    # Two adjacent windows for trend comparison — recent 48h vs the
    # 48h before that. Bounded, not "scan all history every time".
    recent_window = await _skill_stats(db, skill_name, since=now - timedelta(hours=48))
    prior_window = await _skill_stats(db, skill_name, since=now - timedelta(hours=96))
    prior_only_total = prior_window["total"] - recent_window["total"]
    prior_only_successes = prior_window["successes"] - recent_window["successes"]

    recent_rate = (recent_window["successes"] / recent_window["total"]) if recent_window["total"] else None
    prior_rate = (prior_only_successes / prior_only_total) if prior_only_total > 0 else None

    trend = (
        _trend_from_windows(recent_rate, prior_rate, recent_window["total"], prior_only_total)
        if recent_rate is not None and prior_rate is not None
        else "INSUFFICIENT_DATA"
    )

    last_success = await _last_event(db, skill_name, ["success"])
    last_failure = await _last_event(db, skill_name, ["error", "timeout", "blocked"])

    success_rate = overall["successes"] / overall["total"] if overall["total"] else 0

    return {
        "skill": skill_name,
        "status": "ok",
        "total_executions": overall["total"],
        "successes": overall["successes"],
        "failures": overall["failures"],
        "success_rate_pct": round(success_rate * 100, 1),
        "recent_success_rate_pct": round(recent_rate * 100, 1) if recent_rate is not None else None,
        "trend": trend,
        "avg_duration_ms": round(overall["avg_duration_ms"]) if overall.get("avg_duration_ms") else None,
        "last_success_at": last_success.isoformat() if last_success else None,
        "last_failure_at": last_failure.isoformat() if last_failure else None,
    }


async def get_all_skill_reliability(db, min_executions: int = 1) -> dict:
    """
    Report across every skill that has execution history, sorted worst
    success-rate first — the "what needs attention" view for Engineer
    Mode, rather than making someone check skills one at a time.
    """
    skill_names = await db["skill_executions"].distinct("skill")
    reports = []
    for name in skill_names:
        if not name:
            continue
        report = await get_skill_reliability(db, name)
        if report.get("status") == "ok" and report["total_executions"] >= min_executions:
            reports.append(report)

    reports.sort(key=lambda r: r["success_rate_pct"])
    degrading = [r["skill"] for r in reports if r["trend"] == "DEGRADING"]

    return {
        "status": "ok",
        "skills_tracked": len(reports),
        "degrading_skills": degrading,
        "reports": reports,
    }
