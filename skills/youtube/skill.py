import httpx
import os
from typing import Optional, List

class YouTubeSkill:
    def __init__(self):
        self.api_key = os.getenv("GOOGLE_YOUTUBE_API_KEY")
        self.base_url = "https://www.googleapis.com/youtube/v3"
    
    async def search(self, query: str, max_results: int = 10) -> dict:
        """Search for videos on YouTube."""
        try:
            if not self.api_key:
                return {"status": "error", "reason": "GOOGLE_YOUTUBE_API_KEY not set"}
            
            params = {
                "key": self.api_key,
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": min(max_results, 50),
                "order": "relevance"
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/search",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": f"YouTube search failed: {response.text}"}
                
                data = response.json()
                videos = []
                
                for item in data.get("items", []):
                    if item.get("id", {}).get("kind") == "youtube#video":
                        video_id = item.get("id", {}).get("videoId")
                        snippet = item.get("snippet", {})
                        
                        videos.append({
                            "video_id": video_id,
                            "title": snippet.get("title"),
                            "channel": snippet.get("channelTitle"),
                            "description": snippet.get("description")[:200],
                            "thumbnail": snippet.get("thumbnails", {}).get("medium", {}).get("url"),
                            "url": f"https://www.youtube.com/watch?v={video_id}"
                        })
                
                return {
                    "status": "success",
                    "query": query,
                    "results": len(videos),
                    "videos": videos,
                    "message": f"Found {len(videos)} video(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_video_info(self, video_id: str) -> dict:
        """Get detailed info about a video."""
        try:
            if not self.api_key:
                return {"status": "error", "reason": "GOOGLE_YOUTUBE_API_KEY not set"}
            
            params = {
                "key": self.api_key,
                "part": "snippet,contentDetails,statistics",
                "id": video_id
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/videos",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Video not found"}
                
                data = response.json()
                if not data.get("items"):
                    return {"status": "error", "reason": "Video not found"}
                
                video = data["items"][0]
                snippet = video.get("snippet", {})
                stats = video.get("statistics", {})
                details = video.get("contentDetails", {})
                
                return {
                    "status": "success",
                    "video": {
                        "video_id": video_id,
                        "title": snippet.get("title"),
                        "channel": snippet.get("channelTitle"),
                        "description": snippet.get("description"),
                        "duration": details.get("duration"),
                        "views": stats.get("viewCount"),
                        "likes": stats.get("likeCount"),
                        "comments": stats.get("commentCount"),
                        "url": f"https://www.youtube.com/watch?v={video_id}"
                    }
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_transcript(self, video_id: str) -> dict:
        """
        Get video transcript (requires yt-dlp).
        Falls back to description if transcript unavailable.
        """
        try:
            # Try using youtube-transcript-api
            try:
                from youtube_transcript_api import YouTubeTranscriptApi
                
                transcript = YouTubeTranscriptApi.get_transcript(video_id)
                full_text = " ".join([item["text"] for item in transcript])
                
                return {
                    "status": "success",
                    "video_id": video_id,
                    "transcript": full_text,
                    "source": "transcript",
                    "message": f"Transcript retrieved ({len(full_text)} characters)"
                }
            except:
                # Fallback: return video description
                info = await self.get_video_info(video_id)
                if info["status"] == "success":
                    description = info["video"].get("description", "")
                    return {
                        "status": "success",
                        "video_id": video_id,
                        "transcript": description,
                        "source": "description",
                        "message": "Transcript unavailable, returning description instead"
                    }
                
                return {"status": "error", "reason": "Could not retrieve transcript or description"}
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def search_channel(self, channel_name: str, max_results: int = 10) -> dict:
        """Search for videos from a specific channel."""
        try:
            if not self.api_key:
                return {"status": "error", "reason": "GOOGLE_YOUTUBE_API_KEY not set"}
            
            # First search for the channel
            params = {
                "key": self.api_key,
                "part": "snippet",
                "q": channel_name,
                "type": "channel",
                "maxResults": 1
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/search",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Channel not found"}
                
                data = response.json()
                if not data.get("items"):
                    return {"status": "error", "reason": "Channel not found"}
                
                channel_id = data["items"][0]["id"]["channelId"]
                
                # Now search for videos from this channel
                params = {
                    "key": self.api_key,
                    "part": "snippet",
                    "channelId": channel_id,
                    "type": "video",
                    "maxResults": min(max_results, 50),
                    "order": "date"
                }
                
                response = await client.get(
                    f"{self.base_url}/search",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Failed to fetch channel videos"}
                
                data = response.json()
                videos = []
                
                for item in data.get("items", []):
                    if item.get("id", {}).get("kind") == "youtube#video":
                        video_id = item.get("id", {}).get("videoId")
                        snippet = item.get("snippet", {})
                        
                        videos.append({
                            "video_id": video_id,
                            "title": snippet.get("title"),
                            "published_at": snippet.get("publishedAt"),
                            "url": f"https://www.youtube.com/watch?v={video_id}"
                        })
                
                return {
                    "status": "success",
                    "channel": channel_name,
                    "videos": videos,
                    "total": len(videos),
                    "message": f"Found {len(videos)} video(s) from {channel_name}"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute youtube skill with structured args."""
    args = payload.get("args", {}) or {}
    action = args.get("action")
    max_results = int(args.get("max_results") or 10)

    if not action:
        return {
            "status": "info",
            "message": "YouTube actions: search (query), video_info (video_id), transcript (video_id), channel_videos (channel_name)"
        }

    try:
        skill = YouTubeSkill()

        if action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}
            return await skill.search(query, max_results=max_results)

        elif action == "video_info":
            video_id = (args.get("video_id") or "").strip()
            if not video_id:
                return {"status": "error", "reason": "video_info requires a 'video_id'"}
            return await skill.get_video_info(video_id)

        elif action == "transcript":
            video_id = (args.get("video_id") or "").strip()
            if not video_id:
                return {"status": "error", "reason": "transcript requires a 'video_id'"}
            return await skill.get_transcript(video_id)

        elif action == "channel_videos":
            channel_name = (args.get("channel_name") or "").strip()
            if not channel_name:
                return {"status": "error", "reason": "channel_videos requires a 'channel_name'"}
            return await skill.search_channel(channel_name, max_results=max_results)

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: search, video_info, transcript, channel_videos"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
