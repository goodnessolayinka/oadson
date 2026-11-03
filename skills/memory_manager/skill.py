"""
Memory Manager Skill - Connect to MongoDB via MemoryVault.

Added 'search' action — MemoryVault.search() existed but was never
exposed. Without it, Gemini has to guess exact key names for recall,
which fails whenever the key doesn't match verbatim (e.g. guessing
"academic_comeback" when the real key is "academic_comeback_timetable_v2").

Now: if recall fails, or the exact key isn't known, search finds it
by partial key/content match instead.

FIX 2026-08-29 — real production incident: a user asking to clear all
sleep-tracking memory watched the model do 26+ separate tool-call
round-trips, individually searching for then deleting one memory key
at a time (sleep_quality, last_sleep_duration, afternoon_sleep_block,
...), taking minutes for something that should be instant. Root cause
was NOT this file — delete_matching (see below and MemoryVault's own
docstring) already existed and does exactly this in ONE call. The bug
was that manifest.json's function schema (the actual, only thing the
model sees when deciding what tool call to make) never listed
delete_matching as a valid action at all, and its description/no-arg
info message here still only advertised save/recall/search/list/delete.
The model wasn't being slow or careless — it was correctly using the
only deletion tool it could actually see. Manifest fixed to expose and
recommend delete_matching for "forget everything about X" requests.
"""

from memory.vault import MemoryVault


async def run(payload):
    """Execute memory_manager skill with persistent storage."""
    user_id = payload.get("user_id")
    db = payload.get("db")
    args = payload.get("args", {}) or {}

    if not user_id:
        return {"status": "error", "reason": "User ID not available"}

    if db is None:
        return {"status": "error", "reason": "Database connection not available in payload"}

    action = args.get("action")
    if not action:
        return {
            "status": "info",
            "message": "Memory commands: save, recall, search, list, delete, delete_matching. "
                       "Use 'search' when you don't know the exact key. "
                       "Use 'delete_matching' with a 'query' to forget everything about a "
                       "topic in one call — do not call 'delete' repeatedly in a loop."
        }

    try:
        vault = MemoryVault(db, user_id)

        if action == "save":
            key = (args.get("key") or "").strip().lower().replace(" ", "_")
            value = (args.get("value") or "").strip()
            category = (args.get("category") or "general").strip()

            if not key:
                return {"status": "error", "reason": "save requires a 'key'"}
            if not value:
                return {"status": "error", "reason": "save requires a 'value'"}

            await vault.store(key, value, category)
            return {
                "status": "success",
                "message": f"Saved: {key} = {value}",
                "key": key,
                "value": value
            }

        elif action == "recall":
            key = (args.get("key") or "").strip().lower().replace(" ", "_")
            if not key:
                return {"status": "error", "reason": "recall requires a 'key'"}

            value = await vault.recall(key)
            if value:
                return {
                    "status": "success",
                    "message": f"Your {key} is: {value}",
                    "key": key,
                    "value": value
                }
            else:
                # Exact key not found — try a fuzzy search as fallback
                # before giving up entirely
                results = await vault.search(key, limit=5)
                if results:
                    return {
                        "status": "success",
                        "message": f"No exact match for '{key}', but found related memories.",
                        "key": key,
                        "related_matches": results
                    }
                return {
                    "status": "info",
                    "message": f"No memory found under '{key}' or anything related. "
                               f"Try 'search' with a broader term, or 'list' to see everything."
                }

        elif action == "search":
            query = (args.get("query") or args.get("key") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}

            limit = int(args.get("limit") or 10)
            results = await vault.search(query, limit=limit)

            return {
                "status": "success",
                "message": f"Found {len(results)} memory/memories matching '{query}'",
                "query": query,
                "results": results
            }

        elif action == "list":
            category = args.get("category")
            memories = await vault.list_all(category=category)
            return {
                "status": "success",
                "message": f"Total memories: {len(memories)}",
                "memories": memories
            }

        elif action == "pin":
            # Promote memory to core tier — always injected into every conversation
            key = (args.get("key") or "").strip().lower().replace(" ", "_")
            if not key:
                return {"status": "error", "reason": "pin requires a 'key'"}
            result = await vault.pin(key)
            return {**result, "message": f"'{key}' promoted to core memory — will appear in every conversation."}

        elif action == "unpin":
            # Demote core memory back to extended tier
            key = (args.get("key") or "").strip().lower().replace(" ", "_")
            if not key:
                return {"status": "error", "reason": "unpin requires a 'key'"}
            result = await vault.unpin(key)
            return {**result, "message": f"'{key}' moved to extended memory — retrieved only when relevant."}

        elif action == "list_core":
            # List all core (always-in-context) memories
            memories = await vault.get_core_memories()
            if not memories:
                return {
                    "status": "success",
                    "message": "No core memories set. Use action='pin' to promote important memories to always-in-context.",
                    "core_memories": []
                }
            return {
                "status": "success",
                "message": f"{len(memories)} core memory/memories (always injected into every conversation)",
                "core_memories": [{"key": m.get("key"), "content": m.get("content"), "updated_at": str(m.get("updated_at",""))} for m in memories]
            }

        elif action == "update_core":
            # Incorporate a fact directly into the structured core summary.
            # Use when user says "add to core memory", "always remember this", etc.
            import os
            fact = (
                args.get("fact")
                or args.get("value")
                or args.get("content")
                or args.get("query")
                or ""
            ).strip()
            if not fact:
                return {"status": "error", "reason": "update_core requires 'fact' — the information to add to core memory."}

            api_key = os.getenv("GEMINI_API_KEY", "")
            if not api_key:
                return {"status": "error", "reason": "GEMINI_API_KEY not configured."}

            rendered = await vault.extend_core_summary([fact], api_key)
            return {
                "status": "success",
                "message": f"Core memory updated with: {fact[:120]}",
                "preview": rendered[:300] if rendered else "",
            }

        elif action == "show_core":
            # Display the current core memory summary.
            doc = await vault.get_core_summary()
            if not doc or not doc.get("content"):
                return {
                    "status": "info",
                    "message": "No core memory summary yet. Use 'Rebuild from memories' in Settings → Memory, or chat normally and it will build automatically.",
                }
            rendered = vault.render_core_for_injection(doc)
            version = doc.get("version", "?")
            updated = doc.get("updated_at")
            date_str = updated.isoformat()[:10] if updated and hasattr(updated, "isoformat") else ""
            return {
                "status": "success",
                "message": f"Current core memory (v{version}{', updated ' + date_str if date_str else ''}):",
                "core_summary": rendered,
            }

        elif action == "save_core":
            # Save directly as core memory (shortcut for save + pin)
            key = (args.get("key") or "").strip().lower().replace(" ", "_")
            value = args.get("value") or args.get("content") or ""
            if not key or not value:
                return {"status": "error", "reason": "save_core requires 'key' and 'value'"}
            result = await vault.store(key, value, category="core")
            return {
                **result,
                "message": f"'{key}' saved as core memory — will appear in every conversation."
            }

        elif action == "delete":
            key = (args.get("key") or "").strip().lower().replace(" ", "_")
            if not key:
                return {"status": "error", "reason": "delete requires a 'key'"}

            if await vault.delete(key):
                return {"status": "success", "message": f"Deleted: {key}"}
            else:
                return {"status": "error", "reason": f"Memory '{key}' not found"}

        elif action == "delete_matching":
            # ADDED 2026-08-22 — see MemoryVault.delete_matching's own
            # docstring in memory/vault.py for the full real-incident
            # rationale (a user's repeated "forget GST" requests never
            # actually took effect because the model picked the wrong
            # exact key for plain 'delete', and the correct memory kept
            # resurfacing in daily proactive check-ins for days
            # afterward). This is the same delete power without the
            # model needing to correctly transcribe a key at all —
            # describe what to forget, this finds and removes it.
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "delete_matching requires a 'query' describing what to forget"}
            result = await vault.delete_matching(query)
            if result["count"] == 0:
                return {"status": "success", "message": f"No memories matched '{query}' — nothing was deleted.", "deleted": []}
            return {
                "status": "success",
                "message": f"Deleted {result['count']} memor{'y' if result['count']==1 else 'ies'} matching '{query}': " +
                            ", ".join(d["key"] for d in result["deleted"]),
                "deleted": result["deleted"],
            }

        elif action == "deduplicate":
            # Find and merge memories that mean the same thing.
            # Uses cosine similarity on stored embeddings — only works after
            # backfill_embeddings.py has been run (or enough new stores).
            # Threshold 0.92 = very conservative, only merges near-identical.
            threshold = float(args.get("threshold", 0.92))
            result = await vault.deduplicate(similarity_threshold=threshold)
            if result["merged"] == 0:
                return {
                    "status": "success",
                    "message": f"No duplicates found in {result['checked']} memories (threshold={threshold}).",
                    "checked": result["checked"],
                }
            return {
                "status": "success",
                "message": f"Merged {result['merged']} duplicate pair(s) from {result['checked']} memories.",
                "merged": result["merged"],
                "pairs": result["pairs"][:10],  # cap for readability
            }

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: save, save_core, recall, search, list, list_core, pin, unpin, delete, delete_matching, update_core, show_core, deduplicate"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
