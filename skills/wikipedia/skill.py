import httpx
from typing import Optional

class WikipediaSkill:
    def __init__(self):
        self.base_url = "https://en.wikipedia.org/w/api.php"
    
    async def search(self, query: str, limit: int = 5) -> dict:
        """Search Wikipedia."""
        try:
            params = {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srwhat": "text",
                "format": "json",
                "srlimit": limit
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.base_url,
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Wikipedia search failed"}
                
                data = response.json()
                results = []
                
                for result in data.get("query", {}).get("search", []):
                    results.append({
                        "title": result.get("title"),
                        "snippet": result.get("snippet"),
                        "url": f"https://en.wikipedia.org/wiki/{result.get('title', '').replace(' ', '_')}"
                    })
                
                return {
                    "status": "success",
                    "query": query,
                    "results": results,
                    "total": len(results),
                    "message": f"Found {len(results)} Wikipedia article(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_summary(self, title: str) -> dict:
        """Get a summary of a Wikipedia article."""
        try:
            params = {
                "action": "query",
                "titles": title,
                "prop": "extracts",
                "explaintext": True,
                "exlimit": 1,
                "exsentences": 10,
                "format": "json"
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.base_url,
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Failed to fetch article"}
                
                data = response.json()
                pages = data.get("query", {}).get("pages", {})
                
                if not pages:
                    return {"status": "error", "reason": f"Article not found: {title}"}
                
                page = list(pages.values())[0]
                
                if "extract" not in page:
                    return {"status": "error", "reason": f"Could not extract summary for {title}"}
                
                return {
                    "status": "success",
                    "title": page.get("title"),
                    "summary": page.get("extract"),
                    "url": f"https://en.wikipedia.org/wiki/{page.get('title', '').replace(' ', '_')}",
                    "message": f"Summary retrieved for '{page.get('title')}'"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_full_article(self, title: str, max_length: int = 5000) -> dict:
        """Get full article text."""
        try:
            params = {
                "action": "query",
                "titles": title,
                "prop": "extracts",
                "explaintext": True,
                "format": "json"
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.base_url,
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Failed to fetch article"}
                
                data = response.json()
                pages = data.get("query", {}).get("pages", {})
                
                if not pages:
                    return {"status": "error", "reason": f"Article not found: {title}"}
                
                page = list(pages.values())[0]
                
                if "extract" not in page:
                    return {"status": "error", "reason": f"Could not extract article for {title}"}
                
                content = page.get("extract", "")
                truncated = len(content) > max_length
                
                return {
                    "status": "success",
                    "title": page.get("title"),
                    "content": content[:max_length],
                    "truncated": truncated,
                    "url": f"https://en.wikipedia.org/wiki/{page.get('title', '').replace(' ', '_')}",
                    "message": f"Article retrieved ({len(content)} characters)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_infobox(self, title: str) -> dict:
        """Get infobox data from an article."""
        try:
            params = {
                "action": "query",
                "titles": title,
                "prop": "revisions",
                "rvprop": "content",
                "format": "json"
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.base_url,
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Failed to fetch article"}
                
                data = response.json()
                pages = data.get("query", {}).get("pages", {})
                
                if not pages:
                    return {"status": "error", "reason": f"Article not found: {title}"}
                
                # Extract infobox from wikitext (simplified)
                return {
                    "status": "success",
                    "title": title,
                    "message": "Use get_summary() or get_full_article() for better results"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_random_article(self) -> dict:
        """Get a random Wikipedia article."""
        try:
            params = {
                "action": "query",
                "list": "random",
                "rnnamespace": 0,
                "rnlimit": 1,
                "format": "json"
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.base_url,
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Failed to fetch random article"}
                
                data = response.json()
                random_article = data.get("query", {}).get("random", [])[0]
                
                title = random_article.get("title")
                
                # Now get the summary
                return await self.get_summary(title)
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute wikipedia skill with structured args."""
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "Wikipedia actions: search (query), summary (title), full_article (title), random"
        }

    try:
        skill = WikipediaSkill()

        if action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}
            return await skill.search(query, limit=int(args.get("limit") or 5))

        elif action == "summary":
            title = (args.get("title") or "").strip()
            if not title:
                return {"status": "error", "reason": "summary requires a 'title'"}
            return await skill.get_summary(title)

        elif action == "full_article":
            title = (args.get("title") or "").strip()
            if not title:
                return {"status": "error", "reason": "full_article requires a 'title'"}
            return await skill.get_full_article(title, max_length=int(args.get("max_length") or 5000))

        elif action == "random":
            return await skill.get_random_article()

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: search, summary, full_article, random"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
