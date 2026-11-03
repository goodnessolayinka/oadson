import os
import httpx
import base64
from typing import Optional

class SpotifySkill:
    def __init__(self):
        self.client_id = os.getenv("SPOTIFY_CLIENT_ID")
        self.client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        self.base_url = "https://api.spotify.com/v1"
        self.auth_url = "https://accounts.spotify.com/api/token"
        self.access_token = None
        self.timeout = 10
    
    async def get_access_token(self) -> Optional[str]:
        """Get Spotify access token using Client Credentials flow."""
        if not self.client_id or not self.client_secret:
            return None
        
        try:
            # Encode credentials
            credentials = f"{self.client_id}:{self.client_secret}"
            encoded = base64.b64encode(credentials.encode()).decode()
            
            headers = {
                "Authorization": f"Basic {encoded}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            data = {"grant_type": "client_credentials"}
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.auth_url,
                    headers=headers,
                    data=data,
                    timeout=self.timeout
                )
                
                if response.status_code == 200:
                    self.access_token = response.json()["access_token"]
                    return self.access_token
        
        except Exception as e:
            return None
        
        return None
    
    async def search(self, query: str, search_type: str = "track") -> dict:
        """Search Spotify for songs, artists, or albums."""
        if not self.access_token:
            await self.get_access_token()
        
        if not self.access_token:
            return {
                "status": "error",
                "reason": "Spotify credentials not configured (SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)"
            }
        
        try:
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            }
            
            params = {
                "q": query,
                "type": search_type,
                "limit": 5
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/search",
                    headers=headers,
                    params=params,
                    timeout=self.timeout
                )
                
                if response.status_code == 200:
                    data = response.json()
                    results = []
                    
                    # Parse results based on search type
                    if search_type == "track" and data.get("tracks"):
                        for track in data["tracks"]["items"][:5]:
                            results.append({
                                "name": track["name"],
                                "artist": ", ".join([a["name"] for a in track["artists"]]),
                                "url": track["external_urls"]["spotify"],
                                "preview_url": track.get("preview_url")
                            })
                    
                    elif search_type == "artist" and data.get("artists"):
                        for artist in data["artists"]["items"][:5]:
                            results.append({
                                "name": artist["name"],
                                "followers": artist["followers"]["total"],
                                "url": artist["external_urls"]["spotify"]
                            })
                    
                    elif search_type == "album" and data.get("albums"):
                        for album in data["albums"]["items"][:5]:
                            results.append({
                                "name": album["name"],
                                "artist": album["artists"][0]["name"],
                                "release_date": album["release_date"],
                                "url": album["external_urls"]["spotify"]
                            })
                    
                    return {
                        "status": "success",
                        "query": query,
                        "type": search_type,
                        "results": results,
                        "count": len(results)
                    }
                
                else:
                    return {
                        "status": "error",
                        "reason": f"Spotify API error: {response.status_code}"
                    }
        
        except Exception as e:
            return {
                "status": "error",
                "reason": str(e)
            }

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute spotify skill with structured args.

    REWRITTEN: previously the ENTIRE raw message was passed as the search
    query verbatim — "can you search for the best song on the web and
    then use spotify to play it" would have been searched on Spotify as
    one giant literal string, same bug class as web_search/calculator.
    Now uses a clean args.query.
    """
    args = payload.get("args", {}) or {}
    query = (args.get("query") or "").strip()

    if not query:
        return {"status": "error", "reason": "No search query provided"}

    try:
        skill = SpotifySkill()
        search_type = (args.get("search_type") or "track").strip()
        return await skill.search(query, search_type)
    except Exception as e:
        return {"status": "error", "reason": str(e)}
