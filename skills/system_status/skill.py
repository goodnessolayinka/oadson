import os
import psutil
from datetime import datetime
from typing import Optional

class SystemStatusSkill:
    def __init__(self, db):
        # Previously imported a global `db` directly. Now receives it
        # from the request payload, consistent with every other skill —
        # this is operator/system-level tooling (not per-user data), so
        # no user_id scoping is needed here, just the same db-from-
        # payload convention as the rest of the codebase.
        self.db = db
        self.start_time = datetime.utcnow()
    
    async def get_backend_status(self) -> dict:
        """Get backend health status."""
        try:
            # Check database connection
            try:
                await self.db.client.admin.command('ping')
                db_status = "healthy"
                db_latency = "< 100ms"
            except:
                db_status = "unhealthy"
                db_latency = "timeout"
            
            # Get uptime
            uptime = datetime.utcnow() - self.start_time
            uptime_str = f"{uptime.days}d {uptime.seconds // 3600}h {(uptime.seconds % 3600) // 60}m"
            
            return {
                "status": "success",
                "backend_status": {
                    "api": "operational",
                    "database": db_status,
                    "db_latency": db_latency,
                    "uptime": uptime_str,
                    "timestamp": datetime.utcnow().isoformat(),
                    "version": "V3"
                },
                "message": "Backend is healthy" if db_status == "healthy" else "Database connection issue"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def _cgroup_memory_limits(self) -> Optional[dict]:
        """
        FIX 2026-08-29 — real bug, confirmed live: psutil.virtual_memory()
        reads the PHYSICAL HOST's memory, not this container's cgroup
        limit. Render (like any container platform) runs many apps on
        one big host machine; each app is isolated via Linux cgroups,
        but psutil has no concept of that isolation — it just reads
        /proc/meminfo, which reports the whole host. Confirmed in
        production: a 512MB Render free-tier instance reported "18GB
        used of 30GB total, 12GB free" — completely fictional numbers
        for what this process can actually use, actively misleading for
        anyone trying to reason about their real memory budget. This
        also silently neutered core.oadson_home.has_memory_headroom()
        (used as a pre-flight guard before a memory-heavy github clone)
        — it was checking host headroom, which is never going to run
        low, so the guard could never actually trigger on a real 512MB
        box.

        Reads the container's REAL limit and current usage straight
        from cgroup files — cgroup v2 first (current standard, single
        unified hierarchy), falling back to v1 (older, still common).
        Returns None if neither is available (e.g. running outside a
        container, like local dev) — callers should fall back to
        psutil in that case, clearly labeled as host-level, not
        silently presented as if it were container-level.
        """
        try:
            # cgroup v2 — single unified hierarchy
            v2_max = "/sys/fs/cgroup/memory.max"
            v2_current = "/sys/fs/cgroup/memory.current"
            if os.path.exists(v2_max) and os.path.exists(v2_current):
                with open(v2_max) as f:
                    raw_limit = f.read().strip()
                with open(v2_current) as f:
                    used = int(f.read().strip())
                if raw_limit == "max":
                    return None  # no real container limit set — not meaningfully different from host
                limit = int(raw_limit)
                return {"limit_bytes": limit, "used_bytes": used, "cgroup_version": "v2"}
        except Exception:
            pass

        try:
            # cgroup v1 — older hierarchy, separate file per controller
            v1_limit_path = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
            v1_usage_path = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
            if os.path.exists(v1_limit_path) and os.path.exists(v1_usage_path):
                with open(v1_limit_path) as f:
                    limit = int(f.read().strip())
                with open(v1_usage_path) as f:
                    used = int(f.read().strip())
                # v1's "no limit" sentinel is a huge number close to the
                # architecture's max representable value (commonly
                # 9223372036854771712 or similar), not a real limit —
                # treat anything absurdly large as "no real limit set",
                # same as v2's "max".
                if limit > (1 << 62):
                    return None
                return {"limit_bytes": limit, "used_bytes": used, "cgroup_version": "v1"}
        except Exception:
            pass

        return None

    def get_system_metrics(self) -> dict:
        """Get system resource metrics."""
        try:
            # CPU usage
            cpu_percent = psutil.cpu_percent(interval=1)
            
            # Memory usage
            memory = psutil.virtual_memory()

            # FIX 2026-08-29: prefer the real container limit (see
            # _cgroup_memory_limits' own note) over psutil's host-wide
            # numbers whenever it's actually available — this is the
            # number that matters for "am I about to get OOM-killed",
            # not the host's. Falls back to psutil's host-level reading,
            # explicitly labeled as such, only when no cgroup limit
            # could be read at all (e.g. local dev outside a container).
            cgroup = self._cgroup_memory_limits()
            if cgroup:
                mem_total = cgroup["limit_bytes"]
                mem_used = cgroup["used_bytes"]
                mem_available = max(mem_total - mem_used, 0)
                mem_percent = round((mem_used / mem_total) * 100, 1) if mem_total else 0
                mem_scope = f"container (cgroup {cgroup['cgroup_version']})"
            else:
                mem_total = memory.total
                mem_used = memory.used
                mem_available = memory.available
                mem_percent = memory.percent
                mem_scope = "host (no container cgroup limit found — not container-scoped)"
            
            # Disk usage
            disk = psutil.disk_usage('/')
            
            return {
                "status": "success",
                "metrics": {
                    "cpu_usage_percent": cpu_percent,
                    "memory": {
                        "scope": mem_scope,
                        "total_mb": round(mem_total / (1024**2), 1),
                        "used_mb": round(mem_used / (1024**2), 1),
                        "available_mb": round(mem_available / (1024**2), 1),
                        "percent": mem_percent,
                    },
                    "disk": {
                        "total_gb": round(disk.total / (1024**3), 2),
                        "used_gb": round(disk.used / (1024**3), 2),
                        "percent": disk.percent
                    },
                    "timestamp": datetime.utcnow().isoformat()
                },
                "message": (
                    f"System metrics retrieved — memory figures are {mem_scope}"
                    if cgroup else
                    "System metrics retrieved — WARNING: memory figures are host-wide "
                    "(no container limit detected), NOT this app's real budget"
                ),
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_api_stats(self) -> dict:
        """Get API usage statistics."""
        try:
            # Was querying exam_sessions/questions — collections from an
            # unrelated project template, never part of OADSON V3's real
            # schema. Silently returned 0/0 always (count_documents on a
            # nonexistent collection doesn't error, it just returns 0),
            # so this looked "successful" while reporting meaningless
            # data the whole time. Fixed to query what actually exists.
            users_count = await self.db["users"].count_documents({})
            sessions_count = await self.db["ai_sessions"].count_documents({})
            memories_count = await self.db["memory"].count_documents({})
            skills_created_count = await self.db["generated_skills"].count_documents({})
            jobs_count = await self.db["jobs"].count_documents({})

            return {
                "status": "success",
                "stats": {
                    "total_users": users_count,
                    "total_sessions": sessions_count,
                    "total_memories": memories_count,
                    "total_generated_skills": skills_created_count,
                    "total_background_jobs": jobs_count,
                    "timestamp": datetime.utcnow().isoformat()
                },
                "message": "API statistics retrieved"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def check_database_health(self) -> dict:
        """Detailed database health check."""
        try:
            health = {
                "status": "checking",
                "checks": {}
            }
            
            # Connection test
            try:
                await self.db.client.admin.command('ping')
                health["checks"]["connection"] = "✓ connected"
            except:
                health["checks"]["connection"] = "✗ disconnected"
            
            # Collection access test
            try:
                await self.db["users"].count_documents({})
                health["checks"]["users_collection"] = "✓ accessible"
            except:
                health["checks"]["users_collection"] = "✗ inaccessible"
            
            # Index test
            try:
                indexes_cursor = self.db["users"].list_indexes()
                indexes_list = await indexes_cursor.to_list(length=None)
                health["checks"]["indexes"] = f"✓ {len(indexes_list)} indexes"
            except:
                health["checks"]["indexes"] = "✗ error reading indexes"
            
            all_ok = all("✓" in str(v) for v in health["checks"].values())
            health["status"] = "healthy" if all_ok else "unhealthy"
            
            return {
                "status": "success",
                "database_health": health,
                "message": "Database health check complete"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def get_environment_info(self) -> dict:
        """Get environment and configuration info."""
        try:
            env_info = {
                "api_url": os.getenv("RENDER_EXTERNAL_URL", "unknown"),
                "environment": os.getenv("ENVIRONMENT", "production"),
                "debug": os.getenv("DEBUG", "false").lower() == "true",
                "version": "OADSON V3",
                "python_version": os.sys.version.split()[0],
                "timestamp": datetime.utcnow().isoformat()
            }
            
            return {
                "status": "success",
                "environment": env_info,
                "message": "Environment info retrieved"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_proactive_status(self) -> dict:
        """
        Report the autonomous/proactive WhatsApp agent's own health —
        added 2026-08-07 specifically so this is checkable by just
        asking OADSON in chat, instead of reading Render logs (not
        practical from a phone). Reads the same bridge_settings doc
        core/proactive_agent.py writes to every check cycle.
        """
        try:
            meta = await self.db["bridge_settings"].find_one({"_id": "proactive_agent_meta"})
            if not meta:
                return {
                    "status": "info",
                    "message": "No proactive agent activity recorded yet — either it hasn't completed its first check cycle since startup (runs every 30 min, waits 1 min after boot), or it's disabled (check WHATSAPP_OWNER_NUMBER and GEMINI_API_KEY are set).",
                }

            last_checked = meta.get("last_checked_at")
            last_sent = meta.get("last_sent_at")
            last_send_ok = meta.get("last_send_ok")
            last_error = meta.get("last_error")

            now = datetime.utcnow()
            checked_ago = f"{round((now - last_checked).total_seconds() / 60)}m ago" if last_checked else "never"
            sent_ago = f"{round((now - last_sent).total_seconds() / 3600, 1)}h ago" if last_sent else "never"

            if last_send_ok is False:
                summary = f"⚠️ Last delivery attempt FAILED: {last_error}"
            elif last_sent:
                summary = f"✅ Last successful message: {sent_ago} — \"{(meta.get('last_message') or '')[:100]}\""
            else:
                summary = "No message sent yet — either nothing worth saying was decided, or still gathering activity-pattern data."

            return {
                "status": "success",
                "proactive_agent": {
                    "last_check_cycle": checked_ago,
                    "last_successful_send": sent_ago,
                    "last_send_succeeded": last_send_ok,
                    "last_error": last_error,
                    "messages_sent_today": meta.get("daily_count", 0),
                },
                "message": summary,
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def get_self_test_results(self) -> dict:
        """Run the regression suite (core/self_test.py) and report results."""
        from core.self_test import run_self_test
        return await run_self_test()

    async def get_skill_reliability(self, skill_name: str = None) -> dict:
        """Real success/failure/latency stats from db.skill_executions — one skill or all."""
        from core.skill_reliability import get_skill_reliability, get_all_skill_reliability
        if skill_name:
            return await get_skill_reliability(self.db, skill_name)
        return await get_all_skill_reliability(self.db)

    async def get_memory_events(self, limit: int = 20) -> dict:
        """
        Recent entries from the capped memory_events collection (see
        core/oadson_home.py) — the "what was memory doing before a
        heavy operation ran or got blocked" history. Survives a crash
        because it's in Mongo, not process memory or local disk.
        """
        try:
            cursor = self.db["memory_events"].find({}).sort("ts", -1).limit(limit)
            events = await cursor.to_list(length=limit)
            for e in events:
                e["_id"] = str(e.get("_id"))
                if e.get("ts"):
                    e["ts"] = e["ts"].isoformat()
            return {
                "status": "success",
                "memory_events": events,
                "message": f"{len(events)} recent memory event(s) retrieved" if events else "No memory events recorded yet.",
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def get_full_status_report(self) -> dict:
        """Get comprehensive system status report."""
        try:
            backend = await self.get_backend_status()
            metrics = self.get_system_metrics()
            stats = await self.get_api_stats()
            db_health = await self.check_database_health()
            env = self.get_environment_info()
            
            # Determine overall health
            overall_health = "healthy"
            if backend["backend_status"]["database"] == "unhealthy":
                overall_health = "critical"
            elif metrics["metrics"]["cpu_usage_percent"] > 80:
                overall_health = "warning"
            elif metrics["metrics"]["memory"]["percent"] > 80:
                overall_health = "warning"
            
            return {
                "status": "success",
                "overall_health": overall_health,
                "backend": backend.get("backend_status"),
                "system_metrics": metrics.get("metrics"),
                "api_stats": stats.get("stats"),
                "database_health": db_health.get("database_health"),
                "environment": env.get("environment"),
                "timestamp": datetime.utcnow().isoformat(),
                "message": f"Full status report: {overall_health.upper()}"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute system_status skill with structured args."""
    db = payload.get("db")
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if db is None:
        return {"status": "error", "reason": "Database connection not available"}

    if not action:
        return {
            "status": "info",
            "message": "Status actions: backend_status, metrics, api_stats, db_health, environment, full_report, proactive_status, self_test, memory_events"
        }

    try:
        skill = SystemStatusSkill(db)

        if action == "backend_status":
            return await skill.get_backend_status()
        elif action == "metrics":
            return skill.get_system_metrics()
        elif action == "api_stats":
            return await skill.get_api_stats()
        elif action == "db_health":
            return await skill.check_database_health()
        elif action == "environment":
            return skill.get_environment_info()
        elif action == "full_report":
            return await skill.get_full_status_report()
        elif action == "proactive_status":
            return await skill.get_proactive_status()
        elif action == "self_test":
            return await skill.get_self_test_results()
        elif action == "skill_reliability":
            return await skill.get_skill_reliability(args.get("skill_name"))
        elif action == "memory_events":
            return await skill.get_memory_events(args.get("limit", 20))
        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: backend_status, metrics, api_stats, db_health, environment, full_report, proactive_status, self_test, skill_reliability, memory_events"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
