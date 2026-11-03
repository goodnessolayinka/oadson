import subprocess
import httpx
from typing import Optional

class WHOISSkill:
    def __init__(self):
        self.base_url = "https://www.whois.com/whois"
    
    async def lookup(self, domain: str) -> dict:
        """Look up WHOIS information for a domain."""
        try:
            # Method 1: Try using whois command if available
            try:
                result = subprocess.run(
                    ["whois", domain],
                    capture_output=True,
                    text=True,
                    timeout=15
                )
                
                if result.returncode == 0:
                    return {
                        "status": "success",
                        "domain": domain,
                        "whois_data": result.stdout,
                        "source": "local_whois",
                        "message": "WHOIS lookup successful"
                    }
            except:
                pass
            
            # Method 2: Use WHOIS API
            params = {"domain": domain}
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.base_url,
                    params=params,
                    timeout=15
                )
                
                if response.status_code == 200:
                    return {
                        "status": "success",
                        "domain": domain,
                        "whois_data": response.text,
                        "source": "online_api",
                        "message": "WHOIS lookup successful"
                    }
                
                return {"status": "error", "reason": "WHOIS lookup failed"}
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def parse_whois(self, whois_data: str) -> dict:
        """Parse WHOIS data and extract key information."""
        try:
            parsed = {
                "registrar": None,
                "registrant": None,
                "created_date": None,
                "updated_date": None,
                "expiry_date": None,
                "status": None,
                "nameservers": []
            }
            
            for line in whois_data.split("\n"):
                line = line.strip()
                
                if "Registrar:" in line:
                    parsed["registrar"] = line.split(":", 1)[1].strip()
                elif "Registrant:" in line or "Registrant Name:" in line:
                    parsed["registrant"] = line.split(":", 1)[1].strip()
                elif "Creation Date:" in line or "Created Date:" in line:
                    parsed["created_date"] = line.split(":", 1)[1].strip()
                elif "Updated Date:" in line or "Last Updated:" in line:
                    parsed["updated_date"] = line.split(":", 1)[1].strip()
                elif "Expir" in line and "Date:" in line:
                    parsed["expiry_date"] = line.split(":", 1)[1].strip()
                elif "Domain Status:" in line or "Status:" in line:
                    parsed["status"] = line.split(":", 1)[1].strip()
                elif "Name Server:" in line or "Nameserver:" in line:
                    ns = line.split(":", 1)[1].strip()
                    if ns:
                        parsed["nameservers"].append(ns)
            
            return {
                "status": "success",
                "parsed_data": parsed,
                "message": "WHOIS data parsed"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute whois skill with structured args.

    REWRITTEN: lookup() happened to be tried first by the old alphabetical
    dir() ordering (lookup < parse_whois), so this skill was less broken
    than its siblings by coincidence — but parse_whois() was still
    permanently unreachable as a standalone action (the loop always
    returned lookup()'s dict before ever trying it), and there was no
    real way to ask for parsing specifically. Both are now real actions.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "WHOIS actions: lookup (with domain), parse (with whois_data)"
        }

    try:
        skill = WHOISSkill()

        if action == "lookup":
            domain = (args.get("domain") or "").strip()
            if not domain:
                return {"status": "error", "reason": "lookup requires a 'domain'"}
            return await skill.lookup(domain)

        elif action == "parse":
            whois_data = args.get("whois_data") or ""
            if not whois_data.strip():
                return {"status": "error", "reason": "parse requires 'whois_data' (raw WHOIS text)"}
            return skill.parse_whois(whois_data)

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: lookup, parse"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
