"""Google Docs Skill - Read and edit documents with auto token refresh."""

import httpx
# V3: google_token comes from payload.get("google_token")
from core.google_oauth import get_valid_google_token


class GoogleDocsSkill:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.docs_url = "https://docs.googleapis.com/v1/documents"
        self.drive_url = "https://www.googleapis.com/drive/v3"
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json"
        }
        self.error = None

    async def get_document(self, doc_id: str) -> dict:
        """Get document content and metadata."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{self.docs_url}/{doc_id}",
                    headers=self.headers
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                doc = response.json()
                
                # Extract text content
                text = ""
                for element in doc.get("body", {}).get("content", []):
                    if "paragraph" in element:
                        for run in element["paragraph"].get("elements", []):
                            if "textRun" in run:
                                text += run["textRun"].get("content", "")
                
                return {
                    "id": doc.get("documentId"),
                    "title": doc.get("title"),
                    "text": text[:1000],  # First 1000 chars
                    "created": doc.get("createdTime"),
                    "modified": doc.get("modifiedTime"),
                    "mime_type": doc.get("mimeType")
                }
        
        except Exception as e:
            self.error = str(e)
            raise

    async def list_docs(self, limit: int = 10) -> list:
        """List Google Docs files."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{self.drive_url}/files",
                    headers=self.headers,
                    params={
                        "q": "mimeType='application/vnd.google-apps.document'",
                        "pageSize": limit,
                        "fields": "files(id, name, createdTime, modifiedTime)"
                    }
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                
                return [
                    {
                        "id": f.get("id"),
                        "name": f.get("name"),
                        "created": f.get("createdTime"),
                        "modified": f.get("modifiedTime")
                    }
                    for f in data.get("files", [])
                ]
        
        except Exception as e:
            self.error = str(e)
            raise

    async def search_docs(self, query: str, limit: int = 10) -> list:
        """Search for documents by name."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{self.drive_url}/files",
                    headers=self.headers,
                    params={
                        "q": f"mimeType='application/vnd.google-apps.document' and name contains '{query}'",
                        "pageSize": limit,
                        "fields": "files(id, name, modifiedTime)"
                    }
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                
                return [
                    {
                        "id": f.get("id"),
                        "name": f.get("name"),
                        "modified": f.get("modifiedTime")
                    }
                    for f in data.get("files", [])
                ]
        
        except Exception as e:
            self.error = str(e)
            raise



async def run(payload):
    """
    Execute google_docs skill with structured args.

    REWRITTEN: action='read' (calling get_document(), which already
    existed and worked) was permanently unreachable before — run() had no
    way to pull a doc_id out of raw English, so it always returned
    "Document reading requires document ID" no matter what. Now the model
    passes doc_id directly (taken from a prior list/search result, per
    the manifest's instruction), unlocking real document reading.
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
            "message": "Docs actions: list, search (with query), read (with doc_id)"
        }

    try:
        google_token = await get_valid_google_token(user_id, db)
        skill = GoogleDocsSkill(access_token=google_token)

        if action == "list":
            limit = args.get("limit") or 10
            docs = await skill.list_docs(limit=int(limit))
            return {"status": "success", "result": {"documents": docs, "count": len(docs)}}

        elif action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}
            limit = args.get("limit") or 10
            docs = await skill.search_docs(query, limit=int(limit))
            return {
                "status": "success",
                "result": {"search_results": docs, "count": len(docs), "query": query}
            }

        elif action == "read":
            doc_id = (args.get("doc_id") or "").strip()
            if not doc_id:
                return {"status": "error", "reason": "read requires a 'doc_id' (get one from list or search first)"}
            doc = await skill.get_document(doc_id)
            return {"status": "success", "result": {"document": doc}}

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: list, search, read"
            }

    except ValueError as e:
        if "refresh token" in str(e).lower():
            return {
                "status": "error",
                "reason": f"Authentication issue: {str(e)}. Please re-login via Google OAuth."
            }
        else:
            return {"status": "error", "reason": f"Docs error: {str(e)}"}

    except Exception as e:
        return {"status": "error", "reason": f"Docs skill error: {str(e)}"}
