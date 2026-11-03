"""Google Calendar Skill - Read, create, and manage calendar events with auto token refresh."""

import httpx
from datetime import datetime
# V3: google_token comes from payload.get("google_token")
from core.google_oauth import get_valid_google_token


class GoogleCalendarSkill:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = "https://www.googleapis.com/calendar/v3"
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json"
        }
        self.error = None

    async def get_upcoming_events(self, limit: int = 10) -> list:
        """Get upcoming events from primary calendar."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{self.base_url}/calendars/primary/events",
                    headers=self.headers,
                    params={
                        "maxResults": limit,
                        "orderBy": "startTime",
                        "singleEvents": True,
                        "timeMin": datetime.utcnow().isoformat() + "Z"
                    }
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                
                events = []
                for event in data.get("items", []):
                    events.append({
                        "id": event.get("id"),
                        "title": event.get("summary", "(No title)"),
                        "start": event.get("start", {}).get("dateTime", event.get("start", {}).get("date")),
                        "end": event.get("end", {}).get("dateTime", event.get("end", {}).get("date")),
                        "description": event.get("description", ""),
                        "location": event.get("location", "")
                    })
                
                return events
        
        except Exception as e:
            self.error = str(e)
            raise

    async def create_event(self, title: str, start: str, end: str, description: str = "") -> dict:
        """Create new calendar event."""
        try:
            event_body = {
                "summary": title,
                "start": {"dateTime": start},
                "end": {"dateTime": end},
                "description": description
            }
            
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{self.base_url}/calendars/primary/events",
                    headers=self.headers,
                    json=event_body
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                
                return {
                    "id": data.get("id"),
                    "title": data.get("summary"),
                    "start": data.get("start", {}).get("dateTime"),
                    "end": data.get("end", {}).get("dateTime")
                }
        
        except Exception as e:
            self.error = str(e)
            raise

    async def get_today_events(self) -> list:
        """Get events for today."""
        today = datetime.utcnow().date().isoformat()
        
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{self.base_url}/calendars/primary/events",
                    headers=self.headers,
                    params={
                        "timeMin": f"{today}T00:00:00Z",
                        "timeMax": f"{today}T23:59:59Z",
                        "singleEvents": True,
                        "orderBy": "startTime"
                    }
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                
                return [
                    {
                        "title": e.get("summary", "(No title)"),
                        "start": e.get("start", {}).get("dateTime", e.get("start", {}).get("date")),
                        "end": e.get("end", {}).get("dateTime", e.get("end", {}).get("date"))
                    }
                    for e in data.get("items", [])
                ]
        
        except Exception as e:
            self.error = str(e)
            raise


async def run(payload):
    """
    Execute google_calendar skill with structured args.

    REWRITTEN: previously action='create' was a permanent dead end —
    create_event() already existed and worked, but run() had no way to
    pull a title/start/end out of raw English, so it always returned
    "Event creation requires structured data" no matter what the user
    asked. That's now unlocked: the model fills in title/start/end
    directly per manifest.json's schema (resolving "tomorrow at 2pm" into
    a real ISO timestamp itself), and this skill just calls the
    already-working create_event() method with clean arguments.
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
            "message": "Calendar actions: upcoming, today, create (with title/start/end)"
        }

    try:
        google_token = await get_valid_google_token(user_id, db)
        skill = GoogleCalendarSkill(access_token=google_token)

        if action == "upcoming":
            limit = args.get("limit") or 10
            events = await skill.get_upcoming_events(limit=int(limit))
            return {
                "status": "success",
                "result": {"events": events, "count": len(events)}
            }

        elif action == "today":
            events = await skill.get_today_events()
            return {
                "status": "success",
                "result": {
                    "events": events,
                    "count": len(events),
                    "date": datetime.utcnow().date().isoformat()
                }
            }

        elif action == "create":
            title = (args.get("title") or "").strip()
            start = (args.get("start") or "").strip()
            end = (args.get("end") or "").strip()
            description = (args.get("description") or "").strip()

            if not title:
                return {"status": "error", "reason": "create requires a 'title'"}
            if not start:
                return {"status": "error", "reason": "create requires a 'start' time"}
            if not end:
                return {"status": "error", "reason": "create requires an 'end' time"}

            event = await skill.create_event(title, start, end, description)
            return {
                "status": "success",
                "result": {
                    "event": event,
                    "message": f"Created event '{title}' from {start} to {end}"
                }
            }

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: upcoming, today, create"
            }

    except ValueError as e:
        if "refresh token" in str(e).lower():
            return {
                "status": "error",
                "reason": f"Authentication issue: {str(e)}. Please re-login via Google OAuth."
            }
        else:
            return {"status": "error", "reason": f"Calendar error: {str(e)}"}

    except Exception as e:
        return {"status": "error", "reason": f"Calendar skill error: {str(e)}"}
