import httpx
from typing import Optional

class CVELookupSkill:
    def __init__(self):
        self.base_url = "https://services.nvd.nist.gov/rest/json/cves/2.0"
    
    async def search_cve(self, query: str, limit: int = 10) -> dict:
        """Search for CVEs by keyword."""
        try:
            params = {
                "keywordSearch": query,
                "resultsPerPage": min(limit, 100)
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.base_url,
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "CVE search failed"}
                
                data = response.json()
                vulnerabilities = []
                
                for vuln in data.get("vulnerabilities", [])[:limit]:
                    cve = vuln.get("cve", {})
                    vulnerabilities.append({
                        "id": cve.get("id"),
                        "description": cve.get("descriptions", [{}])[0].get("value", ""),
                        "published": cve.get("published"),
                        "severity": cve.get("metrics", {}).get("cvssV3", {}).get("baseSeverity", "Unknown")
                    })
                
                return {
                    "status": "success",
                    "query": query,
                    "results": len(vulnerabilities),
                    "vulnerabilities": vulnerabilities,
                    "message": f"Found {len(vulnerabilities)} CVE(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_cve(self, cve_id: str) -> dict:
        """Get details for a specific CVE."""
        try:
            params = {"cveId": cve_id}
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.base_url,
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "CVE not found"}
                
                data = response.json()
                vulns = data.get("vulnerabilities", [])
                
                if not vulns:
                    return {"status": "error", "reason": f"CVE {cve_id} not found"}
                
                cve = vulns[0].get("cve", {})
                
                return {
                    "status": "success",
                    "cve": {
                        "id": cve.get("id"),
                        "description": cve.get("descriptions", [{}])[0].get("value"),
                        "published": cve.get("published"),
                        "modified": cve.get("lastModified"),
                        "metrics": cve.get("metrics", {}),
                        "references": [r.get("url") for r in cve.get("references", [])],
                        "weaknesses": cve.get("weaknesses", [])
                    },
                    "message": f"CVE {cve_id} details retrieved"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def search_by_product(self, product: str, limit: int = 10) -> dict:
        """Search CVEs affecting a specific product."""
        try:
            params = {
                "cpeName": f"cpe:2.3:a:*:{product}:*:*:*:*:*:*:*:*",
                "resultsPerPage": min(limit, 100)
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.base_url,
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Search failed"}
                
                data = response.json()
                vulnerabilities = []
                
                for vuln in data.get("vulnerabilities", [])[:limit]:
                    cve = vuln.get("cve", {})
                    vulnerabilities.append({
                        "id": cve.get("id"),
                        "severity": cve.get("metrics", {}).get("cvssV3", {}).get("baseSeverity"),
                        "score": cve.get("metrics", {}).get("cvssV3", {}).get("baseScore"),
                        "published": cve.get("published")
                    })
                
                return {
                    "status": "success",
                    "product": product,
                    "results": len(vulnerabilities),
                    "vulnerabilities": vulnerabilities,
                    "message": f"Found {len(vulnerabilities)} CVE(s) for {product}"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def search_by_severity(self, severity: str = "CRITICAL", limit: int = 10) -> dict:
        """Search for CVEs by severity level."""
        try:
            valid_severities = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
            
            if severity.upper() not in valid_severities:
                return {
                    "status": "error",
                    "reason": f"Invalid severity. Valid: {', '.join(valid_severities)}"
                }
            
            # Note: NVD API has limitations on filtering by severity
            # This is a workaround using keyword search
            params = {
                "keywordSearch": severity,
                "resultsPerPage": min(limit, 100)
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.base_url,
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Search failed"}
                
                data = response.json()
                vulnerabilities = []
                
                for vuln in data.get("vulnerabilities", []):
                    cve = vuln.get("cve", {})
                    cve_severity = cve.get("metrics", {}).get("cvssV3", {}).get("baseSeverity")
                    
                    if cve_severity and cve_severity.upper() == severity.upper():
                        vulnerabilities.append({
                            "id": cve.get("id"),
                            "severity": cve_severity,
                            "score": cve.get("metrics", {}).get("cvssV3", {}).get("baseScore"),
                            "published": cve.get("published")
                        })
                
                return {
                    "status": "success",
                    "severity": severity.upper(),
                    "results": len(vulnerabilities[:limit]),
                    "vulnerabilities": vulnerabilities[:limit],
                    "message": f"Found {len(vulnerabilities)} {severity} CVE(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute cve_lookup skill with structured args.

    REWRITTEN: previously this tried every method on the class in
    sequence (dir(skill) iteration), passing the ENTIRE raw message as a
    single positional argument to each one, with a bare `except: pass`
    swallowing every failure silently. For a request like "find CVEs
    about Apache", it would try get_cve("find CVEs about Apache") first
    (alphabetically before search_cve), which NVD correctly rejects since
    that's not a real CVE ID — and the error from THAT got surfaced to
    the user as "Requires specific valid CVE ID format", even though the
    user never asked for a specific CVE at all. No real intent detection
    existed; it was trial-and-error against an external API.

    Now the model picks one of four real actions directly, with a clean
    argument for each (query/cve_id/product/severity) per manifest.json's
    schema — no guessing, no blind trial-and-error against NVD's API.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "CVE actions: search (with query), get (with cve_id), by_product (with product), by_severity (with severity)"
        }

    try:
        skill = CVELookupSkill()
        limit = args.get("limit") or 10

        if action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}
            return await skill.search_cve(query, limit=int(limit))

        elif action == "get":
            cve_id = (args.get("cve_id") or "").strip()
            if not cve_id:
                return {"status": "error", "reason": "get requires a 'cve_id' (format: CVE-YYYY-NNNNN)"}
            return await skill.get_cve(cve_id)

        elif action == "by_product":
            product = (args.get("product") or "").strip()
            if not product:
                return {"status": "error", "reason": "by_product requires a 'product'"}
            return await skill.search_by_product(product, limit=int(limit))

        elif action == "by_severity":
            severity = (args.get("severity") or "CRITICAL").strip()
            return await skill.search_by_severity(severity, limit=int(limit))

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: search, get, by_product, by_severity"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
