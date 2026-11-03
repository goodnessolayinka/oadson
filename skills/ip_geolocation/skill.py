import httpx
from typing import Optional

class IPGeolocationSkill:
    def __init__(self):
        pass
    
    async def lookup(self, ip_address: str) -> dict:
        """Look up IP geolocation information."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://ip-api.com/json/{ip_address}",
                    timeout=15,
                    params={"fields": "status,message,continent,country,countryCode,region,regionName,city,zip,lat,lon,timezone,isp,org,as"}
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "IP lookup failed"}
                
                data = response.json()
                
                if data.get("status") != "success":
                    return {"status": "error", "reason": data.get("message", "IP lookup failed")}
                
                return {
                    "status": "success",
                    "ip": ip_address,
                    "location": {
                        "country": data.get("country"),
                        "country_code": data.get("countryCode"),
                        "continent": data.get("continent"),
                        "region": data.get("regionName"),
                        "city": data.get("city"),
                        "zip": data.get("zip"),
                        "latitude": data.get("lat"),
                        "longitude": data.get("lon"),
                        "timezone": data.get("timezone")
                    },
                    "organization": {
                        "isp": data.get("isp"),
                        "org": data.get("org"),
                        "as": data.get("as")
                    },
                    "message": f"IP located in {data.get('city')}, {data.get('country')}"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def bulk_lookup(self, ip_addresses: list) -> dict:
        """Look up multiple IP addresses."""
        try:
            results = []
            
            async with httpx.AsyncClient() as client:
                for ip in ip_addresses:
                    response = await client.get(
                        f"https://ip-api.com/json/{ip}",
                        timeout=15,
                        params={"fields": "status,country,city,lat,lon,isp"}
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("status") == "success":
                            results.append({
                                "ip": ip,
                                "country": data.get("country"),
                                "city": data.get("city"),
                                "isp": data.get("isp")
                            })
            
            return {
                "status": "success",
                "total": len(ip_addresses),
                "resolved": len(results),
                "results": results,
                "message": f"Resolved {len(results)}/{len(ip_addresses)} IP(s)"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_my_ip(self) -> dict:
        """Get your current IP address and location."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "https://ip-api.com/json/",
                    timeout=15,
                    params={"fields": "status,query,country,city,lat,lon,isp"}
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Failed to get IP"}
                
                data = response.json()
                
                if data.get("status") != "success":
                    return {"status": "error", "reason": "IP lookup failed"}
                
                return {
                    "status": "success",
                    "ip": data.get("query"),
                    "country": data.get("country"),
                    "city": data.get("city"),
                    "isp": data.get("isp"),
                    "latitude": data.get("lat"),
                    "longitude": data.get("lon"),
                    "message": f"Your IP: {data.get('query')}"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute ip_geolocation skill with structured args."""
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "IP geolocation actions: lookup (ip_address), bulk_lookup (ip_addresses), my_ip"
        }

    try:
        skill = IPGeolocationSkill()

        if action == "lookup":
            ip_address = (args.get("ip_address") or "").strip()
            if not ip_address:
                return {"status": "error", "reason": "lookup requires an 'ip_address'"}
            return await skill.lookup(ip_address)

        elif action == "bulk_lookup":
            ip_addresses = args.get("ip_addresses")
            if not ip_addresses or not isinstance(ip_addresses, list):
                return {"status": "error", "reason": "bulk_lookup requires an 'ip_addresses' array"}
            return await skill.bulk_lookup(ip_addresses)

        elif action == "my_ip":
            return await skill.get_my_ip()

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: lookup, bulk_lookup, my_ip"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
