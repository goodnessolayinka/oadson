import httpx
from typing import Optional, Dict

from core.ssrf_guard import is_safe_url

class HeaderAnalyzerSkill:
    def __init__(self):
        pass
    
    async def get_headers(self, url: str) -> dict:
        """Get HTTP headers from a URL."""
        safe, reason = await is_safe_url(url)
        if not safe:
            return {"status": "error", "reason": reason}
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.head(url, timeout=15)
                
                headers = dict(response.headers)
                
                return {
                    "status": "success",
                    "url": url,
                    "status_code": response.status_code,
                    "headers": headers,
                    "message": f"Headers retrieved ({response.status_code})"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def analyze_security(self, url: str) -> dict:
        """Analyze security-related headers."""
        safe, reason = await is_safe_url(url)
        if not safe:
            return {"status": "error", "reason": reason}
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.head(url, timeout=15)
                
                headers = {k.lower(): v for k, v in response.headers.items()}
                
                security_headers = {
                    "X-Frame-Options": headers.get("x-frame-options"),
                    "X-Content-Type-Options": headers.get("x-content-type-options"),
                    "Content-Security-Policy": headers.get("content-security-policy"),
                    "Strict-Transport-Security": headers.get("strict-transport-security"),
                    "X-XSS-Protection": headers.get("x-xss-protection"),
                    "Referrer-Policy": headers.get("referrer-policy")
                }
                
                issues = []
                
                if not security_headers["X-Frame-Options"]:
                    issues.append("Missing X-Frame-Options header")
                
                if not security_headers["X-Content-Type-Options"]:
                    issues.append("Missing X-Content-Type-Options header")
                
                if not security_headers["Content-Security-Policy"]:
                    issues.append("Missing Content-Security-Policy header")
                
                if not security_headers["Strict-Transport-Security"]:
                    issues.append("Missing Strict-Transport-Security header")
                
                return {
                    "status": "success",
                    "url": url,
                    "security_headers": security_headers,
                    "issues": issues,
                    "score": f"{(6 - len(issues))/6 * 100:.0f}%",
                    "message": f"Found {len(issues)} security issue(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def detect_server(self, url: str) -> dict:
        """Detect web server and technologies."""
        safe, reason = await is_safe_url(url)
        if not safe:
            return {"status": "error", "reason": reason}
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.head(url, timeout=15)
                
                headers = {k.lower(): v for k, v in response.headers.items()}
                
                server = headers.get("server", "Unknown")
                powered_by = headers.get("x-powered-by", None)
                
                technologies = [server]
                if powered_by:
                    technologies.append(powered_by)
                
                return {
                    "status": "success",
                    "url": url,
                    "server": server,
                    "powered_by": powered_by,
                    "technologies": technologies,
                    "message": f"Server: {server}"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def analyze_header(self, header_name: str, header_value: str) -> dict:
        """Analyze what a specific header means."""
        try:
            header_explanations = {
                "x-frame-options": {
                    "description": "Prevents clickjacking attacks",
                    "values": {
                        "DENY": "Frame is not allowed",
                        "SAMEORIGIN": "Frame allowed only from same origin",
                        "ALLOW-FROM": "Frame allowed from specific origin"
                    }
                },
                "x-content-type-options": {
                    "description": "Prevents MIME type sniffing",
                    "values": {
                        "nosniff": "Enforces declared content type"
                    }
                },
                "content-security-policy": {
                    "description": "Controls which resources can load",
                    "example": "default-src 'self'; script-src 'self' cdn.example.com"
                },
                "strict-transport-security": {
                    "description": "Forces HTTPS connections",
                    "example": "max-age=31536000; includeSubDomains"
                }
            }
            
            explanation = header_explanations.get(header_name.lower(), {
                "description": f"Security/performance header: {header_name}",
                "value": header_value
            })
            
            return {
                "status": "success",
                "header": header_name,
                "value": header_value,
                "explanation": explanation,
                "message": "Header explained"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute header_analyzer skill with structured args.

    REWRITTEN: analyze_header() has a different shape from the other
    three methods — it's sync (not async) and takes TWO positional args
    (header_name, header_value) instead of one. The old loop always
    called every method with a single raw-message argument, so
    analyze_header(message) would TypeError immediately (missing
    header_value) and get silently swallowed — this action was
    permanently dead for a structural reason, not just method-ordering.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "Header analyzer actions: get_headers, security_audit, detect_server (all need url), explain_header (needs header_name/header_value)"
        }

    try:
        skill = HeaderAnalyzerSkill()

        if action == "explain_header":
            header_name = (args.get("header_name") or "").strip()
            header_value = args.get("header_value") or ""
            if not header_name:
                return {"status": "error", "reason": "explain_header requires a 'header_name'"}
            return skill.analyze_header(header_name, header_value)

        url = (args.get("url") or "").strip()
        if not url:
            return {"status": "error", "reason": f"{action} requires a 'url'"}

        if action == "get_headers":
            return await skill.get_headers(url)
        elif action == "security_audit":
            return await skill.analyze_security(url)
        elif action == "detect_server":
            return await skill.detect_server(url)
        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: get_headers, security_audit, detect_server, explain_header"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
