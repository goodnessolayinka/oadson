import httpx
import subprocess
from typing import Optional, List

class SubdomainEnumSkill:
    def __init__(self):
        pass
    
    async def enum_crt_sh(self, domain: str) -> dict:
        """Enumerate subdomains using crt.sh (Certificate Transparency logs)."""
        try:
            url = f"https://crt.sh/?q=%.{domain}&output=json"
            
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=15)
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "crt.sh lookup failed"}
                
                try:
                    data = response.json()
                except:
                    return {"status": "error", "reason": "Invalid response from crt.sh"}
                
                subdomains = set()
                for cert in data:
                    name = cert.get("name_value", "")
                    for subdomain in name.split("\n"):
                        subdomain = subdomain.strip()
                        if subdomain and subdomain.endswith(domain):
                            subdomains.add(subdomain)
                
                subdomains_list = sorted(list(subdomains))
                
                return {
                    "status": "success",
                    "domain": domain,
                    "source": "crt.sh",
                    "subdomains": subdomains_list,
                    "count": len(subdomains_list),
                    "message": f"Found {len(subdomains_list)} subdomain(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def enum_dns_brute(self, domain: str, wordlist_size: str = "small") -> dict:
        """Enumerate subdomains using DNS brute force."""
        try:
            # Choose wordlist based on size
            wordlists = {
                "small": [
                    "www", "mail", "ftp", "admin", "api", "dev", "test", "prod",
                    "staging", "blog", "shop", "store", "docs", "support", "cdn",
                    "static", "img", "images", "download", "files", "assets"
                ],
                "medium": [
                    "www", "mail", "ftp", "admin", "api", "dev", "test", "prod",
                    "staging", "blog", "shop", "store", "docs", "support", "cdn",
                    "static", "img", "images", "download", "files", "assets",
                    "config", "db", "database", "backup", "tmp", "temp", "old",
                    "new", "v1", "v2", "beta", "alpha", "demo", "example"
                ],
                "large": None  # Would need external wordlist
            }
            
            wordlist = wordlists.get(wordlist_size, wordlists["small"])
            
            if not wordlist:
                return {"status": "error", "reason": "Large wordlist requires external file"}
            
            found = []
            
            for subdomain_prefix in wordlist:
                test_domain = f"{subdomain_prefix}.{domain}"
                try:
                    # Try to resolve
                    import socket
                    ip = socket.gethostbyname(test_domain)
                    found.append({
                        "subdomain": test_domain,
                        "ip": ip
                    })
                except:
                    pass
            
            return {
                "status": "success",
                "domain": domain,
                "source": "dns_brute",
                "wordlist_size": wordlist_size,
                "subdomains_found": found,
                "count": len(found),
                "message": f"Found {len(found)} subdomain(s) via DNS brute force"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def enum_search_engines(self, domain: str) -> dict:
        """Find subdomains from search engine results."""
        try:
            url = f"https://www.bing.com/search?q=domain:{domain}"
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, timeout=15)
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Search failed"}
                
                # Simple regex extraction
                import re
                pattern = rf"([\w-]+\.{re.escape(domain)})"
                matches = re.findall(pattern, response.text)
                subdomains = sorted(list(set(matches)))
                
                return {
                    "status": "success",
                    "domain": domain,
                    "source": "search_engine",
                    "subdomains": subdomains,
                    "count": len(subdomains),
                    "message": f"Found {len(subdomains)} subdomain(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def enum_all(self, domain: str) -> dict:
        """Run all enumeration methods and combine results."""
        try:
            results = {
                "domain": domain,
                "all_subdomains": set(),
                "sources": {}
            }
            
            # CRT.sh
            crt_result = await self.enum_crt_sh(domain)
            if crt_result["status"] == "success":
                results["sources"]["crt.sh"] = crt_result.get("subdomains", [])
                results["all_subdomains"].update(crt_result.get("subdomains", []))
            
            # DNS Brute
            brute_result = await self.enum_dns_brute(domain, "small")
            if brute_result["status"] == "success":
                brute_subdomains = [s["subdomain"] for s in brute_result.get("subdomains_found", [])]
                results["sources"]["dns_brute"] = brute_subdomains
                results["all_subdomains"].update(brute_subdomains)
            
            all_subdomains_list = sorted(list(results["all_subdomains"]))
            
            return {
                "status": "success",
                "domain": domain,
                "all_subdomains": all_subdomains_list,
                "total_found": len(all_subdomains_list),
                "sources_used": list(results["sources"].keys()),
                "details_by_source": results["sources"],
                "message": f"Found {len(all_subdomains_list)} total subdomain(s) from {len(results['sources'])} source(s)"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute subdomain_enum skill with structured args."""
    args = payload.get("args", {}) or {}
    action = args.get("action")
    domain = (args.get("domain") or "").strip()

    if not action:
        return {
            "status": "info",
            "message": "Subdomain enum actions: crt_sh, dns_brute, search_engines, all (all require domain)"
        }

    if not domain:
        return {"status": "error", "reason": f"{action} requires a 'domain'"}

    try:
        skill = SubdomainEnumSkill()

        if action == "crt_sh":
            return await skill.enum_crt_sh(domain)
        elif action == "dns_brute":
            wordlist_size = (args.get("wordlist_size") or "small").strip()
            return await skill.enum_dns_brute(domain, wordlist_size=wordlist_size)
        elif action == "search_engines":
            return await skill.enum_search_engines(domain)
        elif action == "all":
            return await skill.enum_all(domain)
        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: crt_sh, dns_brute, search_engines, all"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
