import httpx
from typing import Optional, List

class WaybackSkill:
    def __init__(self):
        self.base_url = "https://archive.org/wayback/available"
    
    async def get_snapshots(self, url: str) -> dict:
        """Get available snapshots for a URL."""
        try:
            params = {"url": url}
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.base_url,
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Failed to query Wayback Machine"}
                
                data = response.json()
                
                if not data.get("archived_snapshots"):
                    return {
                        "status": "success",
                        "url": url,
                        "snapshots": [],
                        "message": "No snapshots found"
                    }
                
                closest = data.get("archived_snapshots", {}).get("closest", {})
                
                return {
                    "status": "success",
                    "url": url,
                    "available": data.get("archived_snapshots") is not None,
                    "closest_snapshot": {
                        "date": closest.get("timestamp"),
                        "url": closest.get("url"),
                        "status": closest.get("status")
                    },
                    "message": "Snapshot found" if closest else "No snapshots available"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_calendar(self, url: str) -> dict:
        """Get calendar of available snapshots."""
        try:
            # Use a different endpoint for calendar data
            params = {"url": url, "output": "json"}
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://web.archive.org/web/timemap/json",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Failed to get calendar"}
                
                data = response.json()
                
                # Extract timestamps
                timestamps = data[1:] if isinstance(data, list) and len(data) > 1 else []
                
                return {
                    "status": "success",
                    "url": url,
                    "total_snapshots": len(timestamps),
                    "snapshots": timestamps[:20],
                    "message": f"Found {len(timestamps)} snapshot(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def view_snapshot(self, url: str, date: Optional[str] = None) -> dict:
        """Get URL to view a snapshot."""
        try:
            if date:
                # Format: YYYYMMDD
                snapshot_url = f"https://web.archive.org/web/{date}/{url}"
            else:
                # Get the closest snapshot
                snapshot_url = f"https://web.archive.org/web/*/{url}"
            
            return {
                "status": "success",
                "url": snapshot_url,
                "date": date,
                "message": "Snapshot URL generated"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute wayback skill with structured args."""
    args = payload.get("args", {}) or {}
    action = args.get("action")
    url = (args.get("url") or "").strip()

    if not action:
        return {
            "status": "info",
            "message": "Wayback actions: check, calendar, view_url (all require url)"
        }

    if not url:
        return {"status": "error", "reason": f"{action} requires a 'url'"}

    try:
        skill = WaybackSkill()

        if action == "check":
            return await skill.get_snapshots(url)
        elif action == "calendar":
            return await skill.get_calendar(url)
        elif action == "view_url":
            result = skill.view_snapshot(url, date=args.get("date"))
            if hasattr(result, "__await__"):
                result = await result
            return result
        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: check, calendar, view_url"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
