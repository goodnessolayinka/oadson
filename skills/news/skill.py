import httpx
import os
from typing import Optional, List

class NewsSkill:
    def __init__(self):
        self.api_key = os.getenv("NEWS_API_KEY")
        self.base_url = "https://newsapi.org/v2"
    
    async def get_top_headlines(self, country: str = "us", limit: int = 10) -> dict:
        """Get top news headlines by country."""
        try:
            if not self.api_key:
                return {
                    "status": "error",
                    "reason": "NEWS_API_KEY not set. Get free API key from newsapi.org"
                }
            
            params = {
                "country": country,
                "pageSize": min(limit, 100),
                "apiKey": self.api_key
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/top-headlines",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": f"API error: {response.text}"}
                
                data = response.json()
                
                if data.get("status") != "ok":
                    return {"status": "error", "reason": data.get("message", "Unknown error")}
                
                articles = []
                for article in data.get("articles", []):
                    articles.append({
                        "title": article.get("title"),
                        "description": article.get("description"),
                        "source": article.get("source", {}).get("name"),
                        "url": article.get("url"),
                        "image": article.get("urlToImage"),
                        "published_at": article.get("publishedAt"),
                        "author": article.get("author")
                    })
                
                return {
                    "status": "success",
                    "country": country,
                    "total_results": data.get("totalResults", 0),
                    "articles": articles[:limit],
                    "message": f"Found {len(articles)} headline(s) for {country.upper()}"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def search_news(self, query: str, sort_by: str = "relevance", limit: int = 10) -> dict:
        """Search for news articles."""
        try:
            if not self.api_key:
                return {"status": "error", "reason": "NEWS_API_KEY not set"}
            
            params = {
                "q": query,
                "sortBy": sort_by,
                "pageSize": min(limit, 100),
                "apiKey": self.api_key
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/everything",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": f"API error: {response.text}"}
                
                data = response.json()
                
                if data.get("status") != "ok":
                    return {"status": "error", "reason": data.get("message")}
                
                articles = []
                for article in data.get("articles", []):
                    articles.append({
                        "title": article.get("title"),
                        "description": article.get("description"),
                        "source": article.get("source", {}).get("name"),
                        "url": article.get("url"),
                        "image": article.get("urlToImage"),
                        "published_at": article.get("publishedAt"),
                        "author": article.get("author")
                    })
                
                return {
                    "status": "success",
                    "query": query,
                    "total_results": data.get("totalResults", 0),
                    "articles": articles[:limit],
                    "message": f"Found {len(articles)} article(s) for '{query}'"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_breaking_news(self, limit: int = 10) -> dict:
        """Get breaking news (real-time)."""
        try:
            if not self.api_key:
                return {"status": "error", "reason": "NEWS_API_KEY not set"}
            
            params = {
                "q": "breaking",
                "sortBy": "publishedAt",
                "pageSize": min(limit, 100),
                "apiKey": self.api_key
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/everything",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "API error"}
                
                data = response.json()
                
                articles = []
                for article in data.get("articles", []):
                    articles.append({
                        "title": article.get("title"),
                        "description": article.get("description"),
                        "source": article.get("source", {}).get("name"),
                        "url": article.get("url"),
                        "published_at": article.get("publishedAt")
                    })
                
                return {
                    "status": "success",
                    "articles": articles[:limit],
                    "message": f"Found {len(articles)} breaking news item(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_news_by_category(self, category: str, limit: int = 10) -> dict:
        """Get news by category (business, tech, sports, health, etc)."""
        try:
            if not self.api_key:
                return {"status": "error", "reason": "NEWS_API_KEY not set"}
            
            valid_categories = [
                "business", "entertainment", "general", "health",
                "science", "sports", "technology"
            ]
            
            if category.lower() not in valid_categories:
                return {
                    "status": "error",
                    "reason": f"Invalid category. Valid: {', '.join(valid_categories)}"
                }
            
            params = {
                "category": category.lower(),
                "pageSize": min(limit, 100),
                "apiKey": self.api_key
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/top-headlines",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "API error"}
                
                data = response.json()
                
                articles = []
                for article in data.get("articles", []):
                    articles.append({
                        "title": article.get("title"),
                        "description": article.get("description"),
                        "source": article.get("source", {}).get("name"),
                        "url": article.get("url"),
                        "published_at": article.get("publishedAt")
                    })
                
                return {
                    "status": "success",
                    "category": category,
                    "articles": articles[:limit],
                    "message": f"Found {len(articles)} {category} news item(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_sources(self, language: str = "en") -> dict:
        """Get available news sources."""
        try:
            if not self.api_key:
                return {"status": "error", "reason": "NEWS_API_KEY not set"}
            
            params = {
                "language": language,
                "apiKey": self.api_key
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/sources",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "API error"}
                
                data = response.json()
                
                sources = []
                for source in data.get("sources", []):
                    sources.append({
                        "id": source.get("id"),
                        "name": source.get("name"),
                        "description": source.get("description"),
                        "url": source.get("url"),
                        "language": source.get("language"),
                        "country": source.get("country")
                    })
                
                return {
                    "status": "success",
                    "language": language,
                    "sources": sources,
                    "message": f"Found {len(sources)} news source(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute news skill with structured args."""
    args = payload.get("args", {}) or {}
    action = args.get("action")
    limit = int(args.get("limit") or 10)

    if not action:
        return {
            "status": "info",
            "message": "News actions: top_headlines, search (query), breaking, by_category (category), sources"
        }

    try:
        skill = NewsSkill()

        if action == "top_headlines":
            return await skill.get_top_headlines(country=(args.get("country") or "us").strip(), limit=limit)
        elif action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}
            return await skill.search_news(query, limit=limit)
        elif action == "breaking":
            return await skill.get_breaking_news(limit=limit)
        elif action == "by_category":
            category = (args.get("category") or "").strip()
            if not category:
                return {"status": "error", "reason": "by_category requires a 'category'"}
            return await skill.get_news_by_category(category, limit=limit)
        elif action == "sources":
            return await skill.get_sources(language=(args.get("language") or "en").strip())
        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: top_headlines, search, breaking, by_category, sources"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
