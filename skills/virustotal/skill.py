import httpx
import os
import hashlib
from typing import Optional

class VirusTotalSkill:
    def __init__(self):
        self.api_key = os.getenv("VIRUSTOTAL_API_KEY")
        self.base_url = "https://www.virustotal.com/api/v3"
    
    async def scan_url(self, url: str) -> dict:
        """Scan a URL for malware."""
        try:
            if not self.api_key:
                return {
                    "status": "error",
                    "reason": "VIRUSTOTAL_API_KEY not set"
                }
            
            headers = {
                "x-apikey": self.api_key,
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            data = {"url": url}
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/urls",
                    headers=headers,
                    data=data,
                    timeout=15
                )
                
                if response.status_code not in [200, 201]:
                    return {"status": "error", "reason": "Scan failed"}
                
                result = response.json()
                analysis_id = result.get("data", {}).get("id")
                
                return {
                    "status": "success",
                    "url": url,
                    "analysis_id": analysis_id,
                    "message": "URL submitted for scanning"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_url_report(self, url: str) -> dict:
        """Get report for a scanned URL."""
        try:
            if not self.api_key:
                return {"status": "error", "reason": "VIRUSTOTAL_API_KEY not set"}
            
            # Create URL ID (base64 encoded URL)
            import base64
            url_id = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
            
            headers = {"x-apikey": self.api_key}
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/urls/{url_id}",
                    headers=headers,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "URL not found in database"}
                
                data = response.json()
                stats = data.get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
                
                return {
                    "status": "success",
                    "url": url,
                    "report": {
                        "malicious": stats.get("malicious", 0),
                        "undetected": stats.get("undetected", 0),
                        "suspicious": stats.get("suspicious", 0),
                        "harmless": stats.get("harmless", 0)
                    },
                    "message": f"Malicious detections: {stats.get('malicious', 0)}"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def scan_hash(self, hash_value: str) -> dict:
        """Check if a file hash is known to be malicious."""
        try:
            if not self.api_key:
                return {"status": "error", "reason": "VIRUSTOTAL_API_KEY not set"}
            
            headers = {"x-apikey": self.api_key}
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/files/{hash_value}",
                    headers=headers,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Hash not found in database"}
                
                data = response.json()
                stats = data.get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
                
                return {
                    "status": "success",
                    "hash": hash_value,
                    "report": {
                        "malicious": stats.get("malicious", 0),
                        "suspicious": stats.get("suspicious", 0),
                        "undetected": stats.get("undetected", 0)
                    },
                    "message": f"Malicious detections: {stats.get('malicious', 0)}"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute virustotal skill with structured args.

    REWRITTEN: same blind try-every-method pattern as cve_lookup. The
    original "URL not found in database" bug came from dir(skill)
    alphabetically reaching get_url_report() before scan_url() — and
    get_url_report() is a lookup-only call that NEVER submits anything
    for scanning, it just checks for an existing report. A never-before-
    seen URL guarantees a 404 there, which got surfaced as the final
    answer, while scan_url() (which would have actually submitted it)
    never got a chance to run because the loop stopped at the first dict
    return.

    Now the model picks check_url (lookup) vs scan_url (submit fresh)
    explicitly, per manifest.json's schema — which also means the model
    can recover correctly: try check_url first, see "not found", then
    call scan_url in a follow-up turn, instead of the old code silently
    giving up after one wrong guess.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "VirusTotal actions: check_url (existing report), scan_url (submit new scan), check_hash"
        }

    try:
        skill = VirusTotalSkill()

        if action == "check_url":
            url = (args.get("url") or "").strip()
            if not url:
                return {"status": "error", "reason": "check_url requires a 'url'"}
            return await skill.get_url_report(url)

        elif action == "scan_url":
            url = (args.get("url") or "").strip()
            if not url:
                return {"status": "error", "reason": "scan_url requires a 'url'"}
            return await skill.scan_url(url)

        elif action == "check_hash":
            hash_value = (args.get("hash") or "").strip()
            if not hash_value:
                return {"status": "error", "reason": "check_hash requires a 'hash'"}
            return await skill.scan_hash(hash_value)

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: check_url, scan_url, check_hash"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
