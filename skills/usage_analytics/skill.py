"""Usage Analytics Skill - Track and analyze usage patterns."""

from datetime import datetime, timedelta
from typing import Dict, List


class UsageAnalyticsSkill:
    """Track usage statistics and analytics."""
    
    def __init__(self):
        self.stats = {
            "total_requests": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
            "skill_usage": {},
            "requests_by_hour": {},
            "requests_by_day": {}
        }
    
    def get_summary(self) -> Dict:
        """Get usage summary."""
        try:
            return {
                "status": "success",
                "total_requests": self.stats["total_requests"],
                "total_tokens": self.stats["total_tokens"],
                "total_cost": round(self.stats["total_cost"], 2),
                "unique_skills": len(self.stats["skill_usage"]),
                "timestamp": str(datetime.utcnow())
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def get_skill_usage(self) -> Dict:
        """Get usage breakdown by skill."""
        try:
            sorted_skills = sorted(
                self.stats["skill_usage"].items(),
                key=lambda x: x[1]["count"],
                reverse=True
            )
            
            return {
                "status": "success",
                "skills": dict(sorted_skills),
                "total_skills": len(self.stats["skill_usage"]),
                "timestamp": str(datetime.utcnow())
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def get_daily_stats(self, days: int = 7) -> Dict:
        """Get stats for last N days."""
        try:
            today = datetime.utcnow().date()
            daily_stats = {}
            
            for i in range(days):
                date = (today - timedelta(days=i)).isoformat()
                daily_stats[date] = self.stats["requests_by_day"].get(date, 0)
            
            return {
                "status": "success",
                "days": days,
                "daily_stats": daily_stats,
                "timestamp": str(datetime.utcnow())
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def get_hourly_stats(self, hours: int = 24) -> Dict:
        """Get stats for last N hours."""
        try:
            now = datetime.utcnow()
            hourly_stats = {}
            
            for i in range(hours):
                hour = (now - timedelta(hours=i)).strftime("%Y-%m-%d %H:00")
                hourly_stats[hour] = self.stats["requests_by_hour"].get(hour, 0)
            
            return {
                "status": "success",
                "hours": hours,
                "hourly_stats": hourly_stats,
                "timestamp": str(datetime.utcnow())
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def get_cost_breakdown(self) -> Dict:
        """Get cost breakdown by skill."""
        try:
            total_cost = self.stats["total_cost"]
            
            cost_by_skill = {}
            for skill, data in self.stats["skill_usage"].items():
                # Estimate cost per skill (rough average)
                skill_cost = (data.get("tokens", 0) / max(self.stats["total_tokens"], 1)) * total_cost
                cost_by_skill[skill] = round(skill_cost, 4)
            
            return {
                "status": "success",
                "total_cost": round(total_cost, 2),
                "cost_by_skill": cost_by_skill,
                "timestamp": str(datetime.utcnow())
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def get_top_skills(self, limit: int = 10) -> Dict:
        """Get top N most used skills."""
        try:
            sorted_skills = sorted(
                self.stats["skill_usage"].items(),
                key=lambda x: x[1]["count"],
                reverse=True
            )
            
            top_skills = dict(sorted_skills[:limit])
            
            return {
                "status": "success",
                "limit": limit,
                "top_skills": top_skills,
                "timestamp": str(datetime.utcnow())
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def reset_stats(self) -> Dict:
        """Reset all statistics."""
        try:
            self.stats = {
                "total_requests": 0,
                "total_tokens": 0,
                "total_cost": 0.0,
                "skill_usage": {},
                "requests_by_hour": {},
                "requests_by_day": {}
            }
            
            return {
                "status": "success",
                "message": "Statistics reset successfully",
                "timestamp": str(datetime.utcnow())
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute usage_analytics skill with structured args.

    HONEST LIMITATION, not fixed in this pass: UsageAnalyticsSkill's
    __init__ initializes self.stats fresh every single call (all zeros)
    — there is no persistent tracking mechanism anywhere in this file or
    feeding it from elsewhere in the codebase. Every action below will
    correctly run, but will report zero/empty data regardless of real
    usage, because nothing actually records usage anywhere yet. Wiring
    real tracking (e.g. logging each skill call's tokens/cost into Mongo
    from chat_router.py's run_agent_loop, then reading real aggregates
    here) is a separate, larger task than converting this dispatch
    pattern — flagged here rather than fabricated.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "Analytics actions: summary, skill_usage, daily, hourly, cost_breakdown, top_skills, reset"
        }

    try:
        skill = UsageAnalyticsSkill()

        if action == "summary":
            return skill.get_summary()
        elif action == "skill_usage":
            return skill.get_skill_usage()
        elif action == "daily":
            return skill.get_daily_stats(days=int(args.get("days") or 7))
        elif action == "hourly":
            return skill.get_hourly_stats(hours=int(args.get("hours") or 24))
        elif action == "cost_breakdown":
            return skill.get_cost_breakdown()
        elif action == "top_skills":
            return skill.get_top_skills(limit=int(args.get("limit") or 10))
        elif action == "reset":
            return skill.reset_stats()
        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: summary, skill_usage, daily, hourly, cost_breakdown, top_skills, reset"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
