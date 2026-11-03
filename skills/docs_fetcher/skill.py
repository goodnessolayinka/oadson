import httpx
import os
from typing import Optional

from core.ssrf_guard import is_safe_url

class DocsFetcherSkill:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        from google import genai
        self.client = genai.Client(api_key=self.api_key)

    async def _check_url(self, url: str) -> Optional[dict]:
        """
        SSRF GUARD ADDED 2026-08-14. Every public method below fetches a
        caller-supplied URL — none of them checked it wasn't pointed at
        an internal address before this. Shared guard, same one
        web_fetch already used (core/ssrf_guard.py). Returns an error
        dict to return immediately if the URL isn't safe, or None if
        it's fine to proceed.
        """
        safe, reason = await is_safe_url(url)
        if not safe:
            return {"status": "error", "reason": reason}
        return None

    async def fetch_and_summarize(self, url: str, summary_length: str = "medium") -> dict:
        """Fetch documentation from URL and summarize it."""
        blocked = await self._check_url(url)
        if blocked:
            return blocked
        try:
            # Fetch the page
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=30, follow_redirects=True)
                
                if response.status_code != 200:
                    return {"status": "error", "reason": f"Failed to fetch URL: {response.status_code}"}
                
                content = response.text
            
            # Summarize with Gemini
            length_hints = {
                "short": "2-3 sentences",
                "medium": "3-5 sentences",
                "long": "full summary with sections"
            }
            
            prompt = f"""Summarize this documentation in {length_hints.get(summary_length, 'medium')}:

{content[:5000]}

Provide a clear, concise summary focusing on:
1. Main purpose
2. Key features
3. Getting started (if applicable)
4. Important warnings or notes"""
            
            summary_response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "url": url,
                "summary_length": summary_length,
                "summary": summary_response.text,
                "message": "Documentation summarized"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def fetch_api_docs(self, url: str) -> dict:
        """Fetch and extract API documentation."""
        blocked = await self._check_url(url)
        if blocked:
            return blocked
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=30)
                
                if response.status_code != 200:
                    return {"status": "error", "reason": f"Failed to fetch: {response.status_code}"}
                
                content = response.text
            
            prompt = f"""Extract API documentation from this content:

{content[:5000]}

List:
1. Available endpoints (with methods and paths)
2. Required authentication
3. Request parameters
4. Response format
5. Error codes
6. Rate limits (if any)
7. Example requests"""
            
            extraction = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "url": url,
                "api_docs": extraction.text,
                "message": "API documentation extracted"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def find_section(self, url: str, section_name: str) -> dict:
        """Find a specific section in documentation."""
        blocked = await self._check_url(url)
        if blocked:
            return blocked
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=30)
                
                if response.status_code != 200:
                    return {"status": "error", "reason": f"Failed to fetch: {response.status_code}"}
                
                content = response.text
            
            prompt = f"""From this documentation, find and extract the '{section_name}' section:

{content[:5000]}

Provide the full content of that section."""
            
            section_response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "url": url,
                "section": section_name,
                "content": section_response.text,
                "message": f"Section '{section_name}' found"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def compare_docs(self, url1: str, url2: str) -> dict:
        """Compare two documentation pages."""
        blocked = await self._check_url(url1) or await self._check_url(url2)
        if blocked:
            return blocked
        try:
            async with httpx.AsyncClient() as client:
                response1 = await client.get(url1, timeout=30)
                response2 = await client.get(url2, timeout=30)
                
                if response1.status_code != 200 or response2.status_code != 200:
                    return {"status": "error", "reason": "Failed to fetch one or both URLs"}
                
                content1 = response1.text[:5000]
                content2 = response2.text[:5000]
            
            prompt = f"""Compare these two documentations:

Doc 1:
{content1}

Doc 2:
{content2}

Highlight:
1. Similarities
2. Key differences
3. Which is more comprehensive
4. Recommendations"""
            
            comparison = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "url1": url1,
                "url2": url2,
                "comparison": comparison.text,
                "message": "Documentation compared"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_examples(self, url: str) -> dict:
        """Extract code examples from documentation."""
        blocked = await self._check_url(url)
        if blocked:
            return blocked
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=30)
                
                if response.status_code != 200:
                    return {"status": "error", "reason": f"Failed to fetch: {response.status_code}"}
                
                content = response.text
            
            prompt = f"""Extract all code examples from this documentation:

{content[:5000]}

Format them clearly with:
1. Language/framework
2. Description
3. Full code
4. Explanation"""
            
            examples = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "url": url,
                "examples": examples.text,
                "message": "Code examples extracted"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute docs_fetcher skill with structured args.

    REWRITTEN: compare_docs needs TWO urls and find_section needs a url
    PLUS a section_name — both previously unreachable under single-string
    blind dispatch.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "Docs fetcher actions: summarize, api_docs, examples (url), find_section (url/section_name), compare (url1/url2)"
        }

    try:
        skill = DocsFetcherSkill()

        if action == "compare":
            url1 = (args.get("url1") or "").strip()
            url2 = (args.get("url2") or "").strip()
            if not url1 or not url2:
                return {"status": "error", "reason": "compare requires 'url1' and 'url2'"}
            return await skill.compare_docs(url1, url2)

        url = (args.get("url") or "").strip()
        if not url:
            return {"status": "error", "reason": f"{action} requires a 'url'"}

        if action == "summarize":
            return await skill.fetch_and_summarize(url, summary_length=(args.get("summary_length") or "medium").strip())
        elif action == "api_docs":
            return await skill.fetch_api_docs(url)
        elif action == "find_section":
            section_name = (args.get("section_name") or "").strip()
            if not section_name:
                return {"status": "error", "reason": "find_section requires a 'section_name'"}
            return await skill.find_section(url, section_name)
        elif action == "examples":
            return await skill.get_examples(url)
        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: summarize, api_docs, find_section, compare, examples"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
