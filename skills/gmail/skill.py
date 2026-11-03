"""Gmail Skill - Read, draft, send emails with automatic token refresh."""

import httpx
from typing import Optional
# V3: google_token comes from payload.get("google_token")
from core.google_oauth import get_valid_google_token


class GmailSkill:
    def __init__(self, access_token: str):
        """Initialize with valid access token."""
        self.access_token = access_token
        self.base_url = "https://www.googleapis.com/gmail/v1/users/me"
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json"
        }
        self.error = None

    async def get_unread_emails(self, limit: int = 10) -> list:
        """Get unread emails from inbox."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Get unread message IDs
                response = await client.get(
                    f"{self.base_url}/messages",
                    headers=self.headers,
                    params={"q": "is:unread", "maxResults": limit}
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                messages = data.get("messages", [])
                
                # Fetch full message details
                emails = []
                for msg in messages:
                    msg_response = await client.get(
                        f"{self.base_url}/messages/{msg['id']}",
                        headers=self.headers
                    )
                    msg_response.raise_for_status()
                    msg_data = msg_response.json()
                    
                    headers = {h["name"]: h["value"] for h in msg_data["payload"].get("headers", [])}
                    emails.append({
                        "id": msg["id"],
                        "from": headers.get("From", "Unknown"),
                        "subject": headers.get("Subject", "(No subject)"),
                        "snippet": msg_data.get("snippet", "")
                    })
                
                return emails
        
        except Exception as e:
            self.error = str(e)
            raise

    async def get_inbox_count(self) -> int:
        """Get count of unread emails."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{self.base_url}/messages",
                    headers=self.headers,
                    params={"q": "is:unread"}
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                return len(data.get("messages", []))
        
        except Exception as e:
            self.error = str(e)
            raise

    async def send_email(self, to: str, subject: str, body: str) -> bool:
        """Send email (placeholder - requires MIME encoding)."""
        # Full implementation would require creating MIME message
        # For now, return error
        self.error = "Email sending not yet implemented (requires MIME encoding)"
        return False

    async def search_emails(self, query: str, limit: int = 5) -> list:
        """Search emails by query."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{self.base_url}/messages",
                    headers=self.headers,
                    params={"q": query, "maxResults": limit}
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                
                emails = []
                for msg in data.get("messages", []):
                    msg_response = await client.get(
                        f"{self.base_url}/messages/{msg['id']}",
                        headers=self.headers
                    )
                    msg_response.raise_for_status()
                    msg_data = msg_response.json()
                    
                    headers = {h["name"]: h["value"] for h in msg_data["payload"].get("headers", [])}
                    emails.append({
                        "id": msg["id"],
                        "from": headers.get("From", "Unknown"),
                        "subject": headers.get("Subject", "(No subject)"),
                        "snippet": msg_data.get("snippet", "")
                    })
                
                return emails
        
        except Exception as e:
            self.error = str(e)
            raise


async def run(payload):
    """
    Execute gmail skill with structured args.

    REWRITTEN: previously this keyword-matched raw message text (e.g.
    "unread" OR "inbox" OR "email" all meant "list unread", then a
    SEPARATE nested check for "how many"/"count" decided between listing
    and counting — fragile, and the search branch parsed the query by
    stripping the words "search"/"find"/"look for" out of the raw
    message, same brittle pattern as memory_manager/github before their
    fixes. Now the model picks a real action + clean query directly per
    manifest.json's schema.
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
            "message": "Gmail actions: unread, count, search (with a query)"
        }

    try:
        google_token = await get_valid_google_token(user_id, db)
        skill = GmailSkill(access_token=google_token)

        if action == "unread":
            limit = args.get("limit") or 10
            emails = await skill.get_unread_emails(limit=int(limit))
            return {
                "status": "success",
                "result": {
                    "unread_emails": emails,
                    "count": len(emails),
                    "message": f"Found {len(emails)} unread emails"
                }
            }

        elif action == "count":
            count = await skill.get_inbox_count()
            return {
                "status": "success",
                "result": {
                    "unread_count": count,
                    "message": f"You have {count} unread emails"
                }
            }

        elif action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}
            limit = args.get("limit") or 5
            emails = await skill.search_emails(query, limit=int(limit))
            return {
                "status": "success",
                "result": {
                    "search_results": emails,
                    "count": len(emails),
                    "query": query
                }
            }

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: unread, count, search"
            }

    except ValueError as e:
        if "refresh token" in str(e).lower():
            return {
                "status": "error",
                "reason": f"Authentication issue: {str(e)}. Please re-login via Google OAuth."
            }
        else:
            return {"status": "error", "reason": f"Gmail error: {str(e)}"}

    except Exception as e:
        return {"status": "error", "reason": f"Gmail skill error: {str(e)}"}
