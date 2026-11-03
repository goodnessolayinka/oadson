"""Google Tasks Skill - Manage tasks and to-do lists with auto token refresh."""

import httpx
# V3: google_token comes from payload.get("google_token")
from core.google_oauth import get_valid_google_token


class GoogleTasksSkill:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = "https://www.googleapis.com/tasks/v1"
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json"
        }
        self.error = None

    async def list_task_lists(self) -> list:
        """List all task lists."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{self.base_url}/users/@me/lists",
                    headers=self.headers
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                
                return [
                    {
                        "id": tl.get("id"),
                        "title": tl.get("title"),
                        "updated": tl.get("updated")
                    }
                    for tl in data.get("items", [])
                ]
        
        except Exception as e:
            self.error = str(e)
            raise

    async def get_tasks(self, list_id: str = "@default", limit: int = 10) -> list:
        """Get tasks from a task list."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{self.base_url}/lists/{list_id}/tasks",
                    headers=self.headers,
                    params={"maxResults": limit}
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                
                tasks = []
                for task in data.get("items", []):
                    tasks.append({
                        "id": task.get("id"),
                        "title": task.get("title"),
                        "status": task.get("status", "needsAction"),
                        "due": task.get("due"),
                        "updated": task.get("updated"),
                        "notes": task.get("notes", "")
                    })
                
                return tasks
        
        except Exception as e:
            self.error = str(e)
            raise

    async def create_task(self, list_id: str, title: str, notes: str = "", due: str = None) -> dict:
        """Create a new task."""
        try:
            task_body = {
                "title": title,
                "notes": notes
            }
            
            if due:
                task_body["due"] = due
            
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{self.base_url}/lists/{list_id}/tasks",
                    headers=self.headers,
                    json=task_body
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                task = response.json()
                
                return {
                    "id": task.get("id"),
                    "title": task.get("title"),
                    "status": task.get("status"),
                    "created": task.get("updated")
                }
        
        except Exception as e:
            self.error = str(e)
            raise

    async def complete_task(self, list_id: str, task_id: str) -> bool:
        """Mark task as completed."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.patch(
                    f"{self.base_url}/lists/{list_id}/tasks/{task_id}",
                    headers=self.headers,
                    json={"status": "completed"}
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                return True
        
        except Exception as e:
            self.error = str(e)
            raise

    async def get_pending_tasks(self, list_id: str = "@default") -> list:
        """Get only pending (incomplete) tasks."""
        try:
            all_tasks = await self.get_tasks(list_id, limit=100)
            return [t for t in all_tasks if t.get("status") == "needsAction"]
        
        except Exception as e:
            self.error = str(e)
            raise


async def run(payload):
    """
    Execute google_tasks skill with structured args.

    REWRITTEN: both action='create' (create_task()) and action='complete'
    (complete_task()) were permanently unreachable before — same dead-end
    pattern as the rest of the Google suite, just two stubs in one skill
    instead of one. Both already-working methods are now reachable with
    clean structured args.
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
            "message": "Tasks actions: list, task_lists, create (with title), complete (with task_id)"
        }

    try:
        google_token = await get_valid_google_token(user_id, db)
        skill = GoogleTasksSkill(access_token=google_token)
        list_id = (args.get("list_id") or "@default").strip()

        if action == "list":
            tasks = await skill.get_pending_tasks(list_id)
            return {"status": "success", "result": {"pending_tasks": tasks, "count": len(tasks)}}

        elif action == "task_lists":
            lists = await skill.list_task_lists()
            return {"status": "success", "result": {"task_lists": lists, "count": len(lists)}}

        elif action == "create":
            title = (args.get("title") or "").strip()
            notes = (args.get("notes") or "").strip()
            due = (args.get("due") or "").strip() or None
            if not title:
                return {"status": "error", "reason": "create requires a 'title'"}
            task = await skill.create_task(list_id, title, notes=notes, due=due)
            return {
                "status": "success",
                "result": {"task": task, "message": f"Created task '{title}'"}
            }

        elif action == "complete":
            task_id = (args.get("task_id") or "").strip()
            if not task_id:
                return {"status": "error", "reason": "complete requires a 'task_id' (get one from list first)"}
            ok = await skill.complete_task(list_id, task_id)
            return {
                "status": "success" if ok else "error",
                "result": {"message": "Task marked complete" if ok else "Failed to mark task complete"}
            }

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: list, task_lists, create, complete"
            }

    except ValueError as e:
        if "refresh token" in str(e).lower():
            return {
                "status": "error",
                "reason": f"Authentication issue: {str(e)}. Please re-login via Google OAuth."
            }
        else:
            return {"status": "error", "reason": f"Tasks error: {str(e)}"}

    except Exception as e:
        return {"status": "error", "reason": f"Tasks skill error: {str(e)}"}
