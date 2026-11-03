"""Google Drive Skill - List, read, and search files with auto token refresh."""

import httpx
# V3: google_token comes from payload.get("google_token")
from core.google_oauth import get_valid_google_token
from core.ssrf_guard import is_safe_url


class GoogleDriveSkill:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = "https://www.googleapis.com/drive/v3"
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json"
        }
        self.error = None

    async def list_files(self, limit: int = 10, query: str = None) -> list:
        """List files in Drive."""
        try:
            params = {
                "pageSize": limit,
                "fields": "files(id, name, mimeType, createdTime, modifiedTime, size, owners)"
            }
            
            if query:
                params["q"] = query
            
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{self.base_url}/files",
                    headers=self.headers,
                    params=params
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                
                files = []
                for file in data.get("files", []):
                    files.append({
                        "id": file.get("id"),
                        "name": file.get("name"),
                        "type": file.get("mimeType"),
                        "created": file.get("createdTime"),
                        "modified": file.get("modifiedTime"),
                        "size": file.get("size", "unknown")
                    })
                
                return files
        
        except Exception as e:
            self.error = str(e)
            raise

    async def search_files(self, query: str, limit: int = 10, raw_query: bool = False) -> list:
        """
        Search files by name or query.

        BUG FIXED 2026-08-14: this always wrapped `query` in its own
        "name contains '...' or fullText contains '...'" template — but
        callers like list_folder (f"'{folder_id}' in parents") and the
        folder-name lookup (f"mimeType='...' and name contains '...'")
        already build a complete, valid Drive query string themselves.
        Wrapping an already-valid query in another template produced
        doubly-nested, malformed quoting — e.g. "'{folder_id}' in
        parents" became "name contains ''{folder_id}' in parents' or
        fullText contains ''{folder_id}' in parents'" — which Drive's
        API correctly rejected with 400 Bad Request. That was the exact
        cause of every "Client error 400 Bad Request" seen in
        production for folder listing and combined-filter searches.
        raw_query=True now skips the wrapping for callers that already
        built a complete query string; existing free-text callers are
        unaffected since they don't pass it and default to the old
        wrapped behavior.
        """
        try:
            search_query = query if raw_query else f"name contains '{query}' or fullText contains '{query}'"
            
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{self.base_url}/files",
                    headers=self.headers,
                    params={
                        "q": search_query,
                        "pageSize": limit,
                        "fields": "files(id, name, mimeType, createdTime, modifiedTime)"
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
                        "type": f.get("mimeType")
                    }
                    for f in data.get("files", [])
                ]
        
        except Exception as e:
            self.error = str(e)
            raise

    async def read_file(self, file_id: str) -> dict:
        """
        Read the text content of a Google Drive file.
        - Google Docs/Sheets/Slides: exported as plain text via export API
        - PDF/text/csv/json: downloaded directly
        - Binary files (images, videos): returns metadata only with a note
        Returns {"content": str, "name": str, "mime": str, "size": int}
        """
        EXPORT_MIME = {
            "application/vnd.google-apps.document":     "text/plain",
            "application/vnd.google-apps.spreadsheet":  "text/csv",
            "application/vnd.google-apps.presentation": "text/plain",
        }
        DIRECT_READ = {
            "text/plain", "text/csv", "text/html", "text/markdown",
            "application/json", "text/xml", "application/xml",
            "text/javascript", "text/x-python",
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # First get file metadata to know mime type and name
                meta_r = await client.get(
                    f"{self.base_url}/files/{file_id}",
                    headers=self.headers,
                    params={"fields": "id,name,mimeType,size"}
                )
                if meta_r.status_code == 401:
                    raise ValueError("Token expired or invalid")
                meta_r.raise_for_status()
                meta = meta_r.json()
                name = meta.get("name", "unknown")
                mime = meta.get("mimeType", "")
                size = int(meta.get("size") or 0)

                # Google Workspace formats — export as plain text
                if mime in EXPORT_MIME:
                    export_r = await client.get(
                        f"{self.base_url}/files/{file_id}/export",
                        headers=self.headers,
                        params={"mimeType": EXPORT_MIME[mime]}
                    )
                    export_r.raise_for_status()
                    content = export_r.text[:50000]  # cap at 50KB of text
                    return {"name": name, "mime": mime, "content": content,
                            "size": len(content), "truncated": len(export_r.text) > 50000}

                # Plain text / code / data files — download directly
                elif mime in DIRECT_READ or size < 500_000:
                    dl_r = await client.get(
                        f"{self.base_url}/files/{file_id}",
                        headers=self.headers,
                        params={"alt": "media"}
                    )
                    dl_r.raise_for_status()
                    try:
                        content = dl_r.text[:50000]
                        return {"name": name, "mime": mime, "content": content,
                                "size": len(content), "truncated": len(dl_r.text) > 50000}
                    except Exception:
                        return {"name": name, "mime": mime,
                                "content": None, "size": size,
                                "note": f"Binary file ({mime}) — cannot read as text."}
                else:
                    return {"name": name, "mime": mime, "content": None, "size": size,
                            "note": f"File is {size} bytes and type '{mime}' — too large or binary to read inline."}

        except Exception as e:
            self.error = str(e)
            raise

    async def find_and_read(self, name_query: str) -> dict:
        """Search for a file by name then read its content."""
        # Escape single quotes in the name to avoid malformed Drive API query
        safe_name = name_query.replace("'", "\\'")[:100]  # cap length too
        files = await self.search_files(f"name contains '{safe_name}'", limit=3)
        if not files:
            # Try with just the first few significant words
            words = [w for w in name_query.split() if len(w) > 3][:3]
            if words:
                short_query = " ".join(words).replace("'", "\\'")
                files = await self.search_files(f"name contains '{short_query}'", limit=3)
        if not files:
            return {"error": f"No file found matching '{name_query}'"}
        file_id = files[0].get("id") or files[0].get("file_id")
        if not file_id:
            return {"error": "Search returned a file without an ID"}
        result = await self.read_file(file_id)
        result["matched_file"] = files[0]
        return result

    async def get_file_info(self, file_id: str) -> dict:
        """Get detailed file information."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{self.base_url}/files/{file_id}",
                    headers=self.headers,
                    params={
                        "fields": "id, name, mimeType, size, createdTime, modifiedTime, description, webViewLink"
                    }
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                file = response.json()
                
                return {
                    "id": file.get("id"),
                    "name": file.get("name"),
                    "type": file.get("mimeType"),
                    "size": file.get("size"),
                    "created": file.get("createdTime"),
                    "modified": file.get("modifiedTime"),
                    "description": file.get("description"),
                    "link": file.get("webViewLink")
                }
        
        except Exception as e:
            self.error = str(e)
            raise

    async def get_recent_files(self, limit: int = 10) -> list:
        """Get recently modified files."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{self.base_url}/files",
                    headers=self.headers,
                    params={
                        "pageSize": limit,
                        "orderBy": "modifiedTime desc",
                        "fields": "files(id, name, mimeType, modifiedTime)"
                    }
                )
                
                if response.status_code == 401:
                    raise ValueError("Token expired or invalid")
                
                response.raise_for_status()
                data = response.json()
                
                return [
                    {
                        "name": f.get("name"),
                        "type": f.get("mimeType"),
                        "modified": f.get("modifiedTime")
                    }
                    for f in data.get("files", [])
                ]
        
        except Exception as e:
            self.error = str(e)
            raise



async def run(payload):
    """
    Execute google_drive skill with structured args.

    REWRITTEN: action='info' (calling get_file_info(), already working)
    was unreachable before — same dead-end pattern as docs/calendar.
    Also worth noting: the unverifiable-looking Drive listing from
    earlier debugging (a clean answer with no visible skill dropdown) was
    almost certainly produced by THIS skill's old list_files() path
    working correctly but silently — that trust gap is now closed by
    chat_router.py's deterministic per-step input/output logging, not by
    anything specific to this rewrite.
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
            "message": "Drive actions: list, recent, search (with query), info (with file_id), read (with file_id — reads doc content), read_by_name (with name — finds and reads a file by name)"
        }

    try:
        google_token = await get_valid_google_token(user_id, db)
        skill = GoogleDriveSkill(access_token=google_token)

        if action == "list":
            limit = args.get("limit") or 10
            files = await skill.list_files(limit=int(limit))
            return {"status": "success", "result": {"files": files, "count": len(files)}}

        elif action == "recent":
            limit = args.get("limit") or 10
            files = await skill.get_recent_files(limit=int(limit))
            return {"status": "success", "result": {"recent_files": files, "count": len(files)}}

        elif action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}
            limit = args.get("limit") or 10
            files = await skill.search_files(query, limit=int(limit))
            return {
                "status": "success",
                "result": {"search_results": files, "count": len(files), "query": query}
            }

        elif action == "read":
            file_id = (args.get("file_id") or "").strip()
            if not file_id:
                return {"status": "error", "reason": "read requires a 'file_id' (get one from list/search first)"}
            result = await skill.read_file(file_id)
            return {"status": "success", "result": result}

        elif action == "read_by_name":
            name = (args.get("name") or args.get("query") or "").strip()
            if not name:
                return {"status": "error", "reason": "read_by_name requires a 'name' to search for"}
            result = await skill.find_and_read(name)
            if result.get("error"):
                return {"status": "error", "reason": result["error"]}
            return {"status": "success", "result": result}

        elif action == "read_url":
            # Read any external URL — Google Drive share links, Docs links,
            # or any public URL. Extracts readable text from HTML pages,
            # exports Google Docs/Sheets as plain text, downloads raw text files.
            url = (args.get("url") or "").strip()
            if not url:
                return {"status": "error", "reason": "read_url requires a 'url'"}

            import re as _re

            # Detect Google Drive file IDs from share/view links
            # Patterns: /file/d/{id}/, /document/d/{id}/, /spreadsheets/d/{id}/
            gdrive_id_match = _re.search(
                r"/(?:file|document|spreadsheets|presentation)s?/d/([a-zA-Z0-9_-]{10,})",
                url
            )
            if gdrive_id_match:
                file_id = gdrive_id_match.group(1)
                result = await skill.read_file(file_id)
                result["source_url"] = url
                return {"status": "success", "result": result}

            # External URL — fetch and extract text
            # SSRF GUARD ADDED 2026-08-14: this branch only runs once
            # the URL has already failed the Google Drive share-link
            # pattern above, i.e. it's about to fetch an arbitrary
            # caller-supplied URL with no restriction. Same shared
            # guard web_fetch uses.
            safe, reason = await is_safe_url(url)
            if not safe:
                return {"status": "error", "reason": reason}
            try:
                async with httpx.AsyncClient(
                    timeout=20,
                    follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 OADSON/3.0"},
                ) as client:
                    r = await client.get(url)
                    r.raise_for_status()
                    content_type = r.headers.get("content-type", "")

                    if "text/html" in content_type:
                        # Strip HTML tags to get readable text
                        html = r.text
                        # Remove scripts and styles
                        html = _re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=_re.DOTALL | _re.IGNORECASE)
                        # Strip remaining tags
                        text = _re.sub(r"<[^>]+>", " ", html)
                        # Collapse whitespace
                        text = _re.sub(r"\s{2,}", "\n", text).strip()
                        content = text[:50000]
                        return {
                            "status": "success",
                            "result": {
                                "url": url,
                                "content_type": content_type,
                                "content": content,
                                "size": len(content),
                                "truncated": len(text) > 50000,
                            }
                        }
                    elif any(t in content_type for t in ["text/", "application/json", "application/xml"]):
                        content = r.text[:50000]
                        return {
                            "status": "success",
                            "result": {
                                "url": url,
                                "content_type": content_type,
                                "content": content,
                                "size": len(content),
                                "truncated": len(r.text) > 50000,
                            }
                        }
                    else:
                        return {
                            "status": "error",
                            "reason": f"URL returned binary content ({content_type}) — cannot read as text."
                        }
            except Exception as e:
                return {"status": "error", "reason": f"Failed to fetch URL: {e}"}

        elif action == "list_folders":
            # List all folders in Drive
            limit = int(args.get("limit") or 20)
            files = await skill.search_files(
                "mimeType='application/vnd.google-apps.folder'", limit=limit
            )
            return {"status": "success", "result": {"folders": files, "count": len(files)}}

        elif action == "list_folder":
            # List contents of a specific folder by ID or name
            folder_id = (args.get("folder_id") or args.get("file_id") or "").strip()
            folder_name = (args.get("name") or args.get("folder_name") or "").strip()

            if not folder_id and folder_name:
                # Find folder ID by name first
                folders = await skill.search_files(
                    f"mimeType='application/vnd.google-apps.folder' and name contains '{folder_name}'",
                    raw_query=True,
                    limit=3
                )
                if not folders:
                    return {"status": "error", "reason": f"No folder found matching '{folder_name}'"}
                folder_id = folders[0].get("id") or folders[0].get("file_id")

            if not folder_id:
                return {"status": "error", "reason": "list_folder requires 'folder_id' or 'name'"}

            limit = int(args.get("limit") or 30)
            files = await skill.search_files(f"'{folder_id}' in parents", limit=limit, raw_query=True)
            folder_info = await skill.get_file_info(folder_id)
            return {
                "status": "success",
                "result": {
                    "folder": folder_info,
                    "contents": files,
                    "count": len(files),
                }
            }

        elif action == "info":
            file_id = (args.get("file_id") or "").strip()
            if not file_id:
                return {"status": "error", "reason": "info requires a 'file_id' (get one from list/search/recent first)"}
            info = await skill.get_file_info(file_id)
            return {"status": "success", "result": {"file": info}}

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: list, recent, search, info, read, read_by_name, read_url, list_folders, list_folder"
            }

    except ValueError as e:
        if "refresh token" in str(e).lower():
            return {
                "status": "error",
                "reason": f"Authentication issue: {str(e)}. Please re-login via Google OAuth."
            }
        else:
            return {"status": "error", "reason": f"Drive error: {str(e)}"}

    except Exception as e:
        return {"status": "error", "reason": f"Drive skill error: {str(e)}"}
