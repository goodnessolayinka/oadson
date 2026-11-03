from datetime import datetime
from typing import Optional, List

class NoteTakerSkill:
    def __init__(self, db, user_id: str):
        # CRITICAL FIX: previously this imported a global `db` directly
        # (`from database import db`) and NONE of the queries below
        # filtered by user_id — get_all_notes() and search_notes() would
        # return EVERY user's notes to ANY user. Now db comes from the
        # request payload (same pattern as every other skill) and every
        # single query is scoped to user_id.
        if not user_id:
            raise ValueError("NoteTakerSkill requires a user_id")
        self.collection = db["quick_notes"]
        self.user_id = user_id
    
    async def save_note(self, content: str, title: Optional[str] = None, tags: Optional[List[str]] = None) -> dict:
        """Save a quick note to MongoDB."""
        try:
            note = {
                "user_id": self.user_id,
                "title": title or f"Note {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
                "content": content,
                "tags": tags or [],
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            }
            
            result = await self.collection.insert_one(note)
            
            return {
                "status": "success",
                "note_id": str(result.inserted_id),
                "title": note["title"],
                "message": f"Note saved: '{note['title']}'"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_note(self, note_id: str) -> dict:
        """Retrieve a specific note."""
        try:
            from bson import ObjectId
            
            note = await self.collection.find_one({"_id": ObjectId(note_id), "user_id": self.user_id})
            
            if not note:
                return {"status": "error", "reason": "Note not found"}
            
            return {
                "status": "success",
                "note": {
                    "id": str(note["_id"]),
                    "title": note.get("title"),
                    "content": note.get("content"),
                    "tags": note.get("tags", []),
                    "created_at": note.get("created_at"),
                    "updated_at": note.get("updated_at")
                }
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_all_notes(self, limit: int = 50) -> dict:
        """Get all saved notes."""
        try:
            notes = await self.collection.find({"user_id": self.user_id}).sort("created_at", -1).to_list(limit)
            
            formatted_notes = []
            for note in notes:
                formatted_notes.append({
                    "id": str(note["_id"]),
                    "title": note.get("title"),
                    "content": note.get("content")[:100],
                    "tags": note.get("tags", []),
                    "created_at": note.get("created_at")
                })
            
            return {
                "status": "success",
                "total": len(formatted_notes),
                "notes": formatted_notes,
                "message": f"Retrieved {len(formatted_notes)} note(s)"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def search_notes(self, query: str, limit: int = 20) -> dict:
        """Search notes by title or content."""
        try:
            search_filter = {
                "$and": [
                    {"user_id": self.user_id},
                    {"$or": [
                        {"title": {"$regex": query, "$options": "i"}},
                        {"content": {"$regex": query, "$options": "i"}},
                        {"tags": {"$in": [query]}}
                    ]}
                ]
            }
            
            notes = await self.collection.find(search_filter).sort("created_at", -1).to_list(limit)
            
            formatted_notes = []
            for note in notes:
                formatted_notes.append({
                    "id": str(note["_id"]),
                    "title": note.get("title"),
                    "content": note.get("content")[:100],
                    "tags": note.get("tags", []),
                    "created_at": note.get("created_at")
                })
            
            return {
                "status": "success",
                "query": query,
                "results": len(formatted_notes),
                "notes": formatted_notes,
                "message": f"Found {len(formatted_notes)} note(s) matching '{query}'"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def update_note(self, note_id: str, content: Optional[str] = None, 
                         title: Optional[str] = None, tags: Optional[List[str]] = None) -> dict:
        """Update a note."""
        try:
            from bson import ObjectId
            
            update_data = {"updated_at": datetime.utcnow()}
            
            if content:
                update_data["content"] = content
            if title:
                update_data["title"] = title
            if tags:
                update_data["tags"] = tags
            
            result = await self.collection.update_one(
                {"_id": ObjectId(note_id), "user_id": self.user_id},
                {"$set": update_data}
            )
            
            if result.matched_count == 0:
                return {"status": "error", "reason": "Note not found"}
            
            return {
                "status": "success",
                "note_id": note_id,
                "message": "Note updated"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def delete_note(self, note_id: str, confirm: bool = False) -> dict:
        """Delete a note."""
        if not confirm:
            return {
                "status": "pending_confirmation",
                "action": "delete_note",
                "note_id": note_id,
                "message": f"Delete note {note_id}? Reply 'confirm' to proceed."
            }
        
        try:
            from bson import ObjectId
            
            result = await self.collection.delete_one({"_id": ObjectId(note_id), "user_id": self.user_id})
            
            if result.deleted_count == 0:
                return {"status": "error", "reason": "Note not found"}
            
            return {
                "status": "success",
                "message": f"Note {note_id} deleted"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def add_tag(self, note_id: str, tag: str) -> dict:
        """Add a tag to a note."""
        try:
            from bson import ObjectId
            
            result = await self.collection.update_one(
                {"_id": ObjectId(note_id), "user_id": self.user_id},
                {
                    "$addToSet": {"tags": tag},
                    "$set": {"updated_at": datetime.utcnow()}
                }
            )
            
            if result.matched_count == 0:
                return {"status": "error", "reason": "Note not found"}
            
            return {
                "status": "success",
                "message": f"Tag '{tag}' added to note"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute note_taker skill with structured args."""
    user_id = payload.get("user_id")
    db = payload.get("db")
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not user_id:
        return {"status": "error", "reason": "User ID not available"}
    if db is None:
        return {"status": "error", "reason": "Database connection not available"}

    if not action:
        return {
            "status": "info",
            "message": "Note actions: save, get, list, search, update, delete, add_tag"
        }

    try:
        skill = NoteTakerSkill(db, user_id)

        if action == "save":
            content = (args.get("content") or "").strip()
            if not content:
                return {"status": "error", "reason": "save requires 'content'"}
            return await skill.save_note(content, title=args.get("title"), tags=args.get("tags"))

        elif action == "get":
            note_id = (args.get("note_id") or "").strip()
            if not note_id:
                return {"status": "error", "reason": "get requires a 'note_id'"}
            return await skill.get_note(note_id)

        elif action == "list":
            return await skill.get_all_notes(limit=int(args.get("limit") or 50))

        elif action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}
            return await skill.search_notes(query, limit=int(args.get("limit") or 20))

        elif action == "update":
            note_id = (args.get("note_id") or "").strip()
            if not note_id:
                return {"status": "error", "reason": "update requires a 'note_id'"}
            return await skill.update_note(note_id, content=args.get("content"), title=args.get("title"), tags=args.get("tags"))

        elif action == "delete":
            note_id = (args.get("note_id") or "").strip()
            if not note_id:
                return {"status": "error", "reason": "delete requires a 'note_id'"}
            return await skill.delete_note(note_id, confirm=bool(args.get("confirm", False)))

        elif action == "add_tag":
            note_id = (args.get("note_id") or "").strip()
            tag = (args.get("tag") or "").strip()
            if not note_id or not tag:
                return {"status": "error", "reason": "add_tag requires 'note_id' and 'tag'"}
            return await skill.add_tag(note_id, tag)

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: save, get, list, search, update, delete, add_tag"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
