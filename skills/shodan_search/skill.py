import httpx
import os
from typing import Optional

class ShodanSearchSkill:
    def __init__(self):
        self.api_key = os.getenv("SHODAN_API_KEY")
        self.base_url = "https://api.shodan.io"
    
    async def search(self, query: str, limit: int = 10) -> dict:
        """Search Shodan for devices/services."""
        try:
            if not self.api_key:
                return {
                    "status": "error",
                    "reason": "SHODAN_API_KEY not set. Get free API key from shodan.io"
                }
            
            params = {
                "q": query,
                "key": self.api_key,
                "limit": min(limit, 100)
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/shodan/host/search",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": f"Shodan search failed: {response.text}"}
                
                data = response.json()
                results = []
                
                for match in data.get("matches", [])[:limit]:
                    results.append({
                        "ip": match.get("ip_str"),
                        "port": match.get("port"),
                        "org": match.get("org"),
                        "os": match.get("os"),
                        "country": match.get("location", {}).get("country_name"),
                        "timestamp": match.get("timestamp")
                    })
                
                return {
                    "status": "success",
                    "query": query,
                    "total": data.get("total", 0),
                    "results": results,
                    "message": f"Found {len(results)} result(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_host_info(self, ip_address: str) -> dict:
        """Get detailed information about a host."""
        try:
            if not self.api_key:
                return {"status": "error", "reason": "SHODAN_API_KEY not set"}
            
            params = {"key": self.api_key}
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/shodan/host/{ip_address}",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Host not found"}
                
                data = response.json()
                
                return {
                    "status": "success",
                    "ip": ip_address,
                    "info": {
                        "country": data.get("country_name"),
                        "org": data.get("org"),
                        "ports": data.get("ports", []),
                        "services": [f"{p}" for p in data.get("ports", [])][:10],
                        "total_ports": len(data.get("ports", []))
                    },
                    "message": f"Found {len(data.get('ports', []))} open port(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute shodan_search skill with structured args.

    REWRITTEN: same blind try-every-method pattern as cve_lookup/
    virustotal. dir(skill) would reach get_host_info() before search()
    alphabetically, so a free-text query like "find apache servers in
    Germany" would get passed as an ip_address, fail against Shodan's
    host endpoint, and search() (the actually-correct method) never ran.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "Shodan actions: search (with query), host_info (with ip_address)"
        }

    try:
        skill = ShodanSearchSkill()

        if action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}
            limit = args.get("limit") or 10
            return await skill.search(query, limit=int(limit))

        elif action == "host_info":
            ip_address = (args.get("ip_address") or "").strip()
            if not ip_address:
                return {"status": "error", "reason": "host_info requires an 'ip_address'"}
            return await skill.get_host_info(ip_address)

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: search, host_info"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
