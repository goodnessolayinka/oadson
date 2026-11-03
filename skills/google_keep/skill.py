"""Google Keep Skill - Create and manage notes with auto token refresh."""

import httpx
# V3: google_token comes from payload.get("google_token")
from core.google_oauth import get_valid_google_token


class GoogleKeepSkill:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = "https://keep.googleapis.com/v1/notes"
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json"
        }
        self.error = None

    async def list_notes(self, limit: int = 10) -> list:
        """List all notes."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    self.base_url,
                    headers=self.headers,
                    params={"pageSize": limit}
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                
                notes = []
                for note in data.get("notes", []):
                    # Extract text from note
                    text = ""
                    for item in note.get("body", {}).get("textContent", {}).get("text", ""):
                        text += item
                    
                    notes.append({
                        "id": note.get("name"),
                        "title": note.get("title", "(Untitled)"),
                        "text": text[:200],
                        "created": note.get("createTime"),
                        "updated": note.get("updateTime"),
                        "labels": note.get("labels", [])
                    })
                
                return notes
        
        except Exception as e:
            self.error = str(e)
            raise

    async def create_note(self, title: str, text: str) -> dict:
        """Create a new note."""
        try:
            note_body = {
                "title": title,
                "body": {
                    "textContent": {
                        "text": text
                    }
                }
            }
            
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    self.base_url,
                    headers=self.headers,
                    json=note_body
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                note = response.json()
                
                return {
                    "id": note.get("name"),
                    "title": note.get("title"),
                    "created": note.get("createTime")
                }
        
        except Exception as e:
            self.error = str(e)
            raise

    async def search_notes(self, query: str) -> list:
        """Search notes (Keep API limitation: full-text search not available via API)."""
        try:
            # Keep API doesn't support full-text search, so we list and filter client-side
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    self.base_url,
                    headers=self.headers,
                    params={"pageSize": 100}
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                
                matching_notes = []
                for note in data.get("notes", []):
                    if query.lower() in note.get("title", "").lower():
                        matching_notes.append({
                            "title": note.get("title"),
                            "updated": note.get("updateTime")
                        })
                
                return matching_notes
        
        except Exception as e:
            self.error = str(e)
            raise



async def run(payload):
    """
    Execute google_keep skill with structured args.

    REWRITTEN: action='create' (calling create_note(), already working)
    was permanently unreachable before — same dead-end pattern as
    docs/calendar/drive. Now title+text flow through directly from
    structured args.
    """
    user_id = payload.get("user_id")
    db = payload.get("db")
    args = payload.get("args", {}) or {}

    if user_id is None or db is None:
        return {"status": "error", "reason": "user_id or db not in payload"}

    action = args.get("action")
    if not action:
        return {
            "status": "info",
            "message": "Keep actions: list, search (with query), create (with title/text)"
        }

    try:
        google_token = await get_valid_google_token(user_id, db)
        skill = GoogleKeepSkill(access_token=google_token)

        if action == "list":
            notes = await skill.list_notes()
            return {"status": "success", "result": {"notes": notes, "count": len(notes)}}

        elif action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}
            notes = await skill.search_notes(query)
            return {
                "status": "success",
                "result": {"search_results": notes, "count": len(notes), "query": query}
            }

        elif action == "create":
            title = (args.get("title") or "").strip()
            text = (args.get("text") or "").strip()
            if not title:
                return {"status": "error", "reason": "create requires a 'title'"}
            if not text:
                return {"status": "error", "reason": "create requires 'text'"}
            note = await skill.create_note(title, text)
            return {
                "status": "success",
                "result": {"note": note, "message": f"Created note '{title}'"}
            }

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: list, search, create"
            }

    except ValueError as e:
        if "refresh token" in str(e).lower():
            return {
                "status": "error",
                "reason": f"Authentication issue: {str(e)}. Please re-login via Google OAuth."
            }
        else:
            return {"status": "error", "reason": f"Keep error: {str(e)}"}

    except Exception as e:
        return {"status": "error", "reason": f"Keep skill error: {str(e)}"}
