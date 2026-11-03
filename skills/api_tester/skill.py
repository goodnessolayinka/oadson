import httpx
import json
from typing import Optional, Dict, Any

from core.ssrf_guard import is_safe_url

class APITesterSkill:
    def __init__(self):
        pass
    
    async def test_endpoint(self, url: str, method: str = "GET", 
                           headers: Optional[Dict] = None, 
                           body: Optional[str] = None,
                           timeout: int = 15) -> dict:
        """
        Test an API endpoint.

        SSRF GUARD ADDED 2026-08-14: this is the sharpest of the five
        unprotected URL-fetching skills found in an audit — arbitrary
        method + arbitrary body + arbitrary URL, with no check at all
        that the URL wasn't pointed at an internal address (Render's own
        network, a cloud metadata endpoint, localhost). Effectively an
        unrestricted internal-request proxy without this. Same shared
        guard web_fetch already used (core/ssrf_guard.py) — resolves
        the hostname and blocks private/loopback/link-local/reserved
        ranges before the request is ever made.
        """
        safe, reason = await is_safe_url(url)
        if not safe:
            return {"status": "error", "reason": reason}
        try:
            data = None
            if body:
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = body
            
            if not headers:
                headers = {"User-Agent": "OADSON-APITester/1.0"}
            
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method.upper(),
                    url,
                    headers=headers,
                    json=data if isinstance(data, dict) else None,
                    content=data if isinstance(data, str) else None,
                    timeout=timeout,
                    follow_redirects=True
                )
                
                try:
                    response_body = response.json()
                except:
                    response_body = response.text
                
                return {
                    "status": "success",
                    "status_code": response.status_code,
                    "response": response_body,
                    "headers": dict(response.headers)
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute api_tester skill with structured args.

    REWRITTEN: same dead-code pattern as archive_creator — test_endpoint()
    is a real, well-built method (proper method/headers/body handling),
    but the old run() never called it under any input; both keyword
    branches returned canned instructional text instead.
    """
    args = payload.get("args", {}) or {}
    url = (args.get("url") or "").strip()

    if not url:
        return {
            "status": "info",
            "message": "API Tester: provide a 'url' to test (method/headers/body optional)"
        }

    try:
        skill = APITesterSkill()
        method = (args.get("method") or "GET").strip()
        headers = args.get("headers") if isinstance(args.get("headers"), dict) else None
        body = args.get("body")

        return await skill.test_endpoint(url, method=method, headers=headers, body=body)

    except Exception as e:
        return {"status": "error", "reason": str(e)}
