"""
workspace skill — OADSON V3.

Zero cost. Zero filesystem. MongoDB is the workspace.

Actions:
  index    — clone a GitHub repo into db.workspace (reads via GitHub API)
  read     — read a specific file from indexed repo
  search   — search file contents across indexed repo
  list     — list all indexed files in a repo
  patch    — store a suggested edit for a file
  patches  — list pending patches for a repo
  apply    — mark a patch as applied
  drop     — remove an indexed repo from workspace
  repos    — list all indexed repos
"""

import os
import httpx
import base64
import logging
from datetime import datetime

logger = logging.getLogger("oadson.skill.workspace")

GITHUB_API = "https://api.github.com"


def _headers():
    token = os.getenv("GITHUB_TOKEN", "")
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"token {token}"
    return h


async def _get_repo_tree(owner: str, repo: str, branch: str = "main") -> list:
    """Fetch full file tree from GitHub."""
    async with httpx.AsyncClient(timeout=30) as client:
        # Get default branch if not specified
        res = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=_headers())
        if res.status_code == 404:
            raise ValueError(f"Repo '{owner}/{repo}' not found or not accessible.")
        res.raise_for_status()
        default_branch = res.json().get("default_branch", "main")
        branch = branch if branch != "main" else default_branch

        # Get tree recursively
        res = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}",
            headers=_headers(),
            params={"recursive": "1"}
        )
        res.raise_for_status()
        tree = res.json().get("tree", [])
        return [f for f in tree if f.get("type") == "blob"], branch


async def _read_file_from_github(owner: str, repo: str, path: str) -> str:
    """Read file content from GitHub API."""
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
            headers=_headers()
        )
        if res.status_code == 404:
            raise ValueError(f"File '{path}' not found in {owner}/{repo}")
        res.raise_for_status()
        data = res.json()
        content = data.get("content", "")
        encoding = data.get("encoding", "base64")
        if encoding == "base64":
            return base64.b64decode(content).decode("utf-8", errors="replace")
        return content


def _should_index(path: str) -> bool:
    """Skip binary and irrelevant files."""
    skip_exts = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff",
                 ".woff2", ".ttf", ".eot", ".pdf", ".zip", ".tar", ".gz",
                 ".mp4", ".mp3", ".pyc", ".pyo", ".db", ".sqlite"}
    skip_dirs = {"node_modules/", ".git/", "__pycache__/", ".venv/", "venv/",
                 "dist/", "build/", ".next/"}
    path_lower = path.lower()
    if any(path_lower.startswith(d) for d in skip_dirs):
        return False
    if any(path_lower.endswith(ext) for ext in skip_exts):
        return False
    return True


async def run(payload: dict) -> dict:
    user_id = payload.get("user_id")
    if not user_id:
        return {"status": "error", "reason": "User ID not available"}

    try:
        from bootstrap import get
        db = get("db")
    except Exception as e:
        return {"status": "error", "reason": f"Database unavailable: {e}"}

    workspace_col = db["workspace_files"]
    patches_col = db["workspace_patches"]

    args = payload.get("args", {}) or {}
    message = payload.get("message", "").strip()
    action = args.get("action") or "repos"

    try:
        # ── INDEX ──
        if action == "index":
            repo_full = (args.get("repo") or "").strip()
            if not repo_full:
                return {"status": "error", "reason": "index requires 'repo' (e.g. your-github-user/OADSON_V3)"}

            parts = repo_full.split("/")
            if len(parts) != 2:
                return {"status": "error", "reason": "repo must be 'owner/repo_name'"}
            owner, repo = parts

            branch_arg = args.get("branch", "main")
            max_files = int(args.get("max_files") or 200)

            tree, branch = await _get_repo_tree(owner, repo, branch_arg)
            indexable = [f for f in tree if _should_index(f["path"])][:max_files]

            indexed = 0
            skipped = 0
            now = datetime.utcnow()
            _collected = {}  # contents kept for the repo index (see below)

            for file_info in indexable:
                path = file_info["path"]
                try:
                    content = await _read_file_from_github(owner, repo, path)
                    await workspace_col.update_one(
                        {"user_id": user_id, "repo": repo_full, "path": path},
                        {"$set": {
                            "user_id": user_id,
                            "repo": repo_full,
                            "owner": owner,
                            "repo_name": repo,
                            "branch": branch,
                            "path": path,
                            "content": content,
                            "size": len(content),
                            "indexed_at": now,
                        }},
                        upsert=True
                    )
                    _collected[path] = content
                    indexed += 1
                except Exception:
                    skipped += 1

            # REPO INDEX — this skill is a third repo source (GitHub API -> Mongo).
            # Index what was just fetched so chat turns can retrieve from it.
            if _collected:
                try:
                    from core import repo_index as _ri
                    idx = _ri.ingest_files(f"{user_id}:ws:{repo_full}@{branch}".lower(), _collected, user_id)
                    await _ri.save(db, idx, user_id)
                except Exception as e:
                    logger.warning(f"repo index skipped for workspace {repo_full}: {e}")

            return {
                "status": "success",
                "repo": repo_full,
                "branch": branch,
                "indexed": indexed,
                "skipped": skipped,
                "total_files": len(tree),
                "message": f"Indexed {indexed} files from {repo_full}@{branch}"
            }

        # ── REPOS ──
        elif action == "repos":
            cursor = workspace_col.aggregate([
                {"$match": {"user_id": user_id}},
                {"$group": {
                    "_id": "$repo",
                    "file_count": {"$sum": 1},
                    "indexed_at": {"$max": "$indexed_at"},
                    "branch": {"$first": "$branch"}
                }},
                {"$sort": {"indexed_at": -1}}
            ])
            repos = []
            async for r in cursor:
                repos.append({
                    "repo": r["_id"],
                    "files": r["file_count"],
                    "branch": r.get("branch"),
                    "indexed_at": r["indexed_at"].isoformat() if r.get("indexed_at") else ""
                })
            return {"status": "success", "repos": repos, "count": len(repos)}

        # ── LIST ──
        elif action == "list":
            repo_full = (args.get("repo") or "").strip()
            if not repo_full:
                return {"status": "error", "reason": "list requires 'repo'"}

            filter_ = args.get("filter", "").strip()
            query = {"user_id": user_id, "repo": repo_full}
            if filter_:
                query["path"] = {"$regex": filter_, "$options": "i"}

            cursor = workspace_col.find(query, {"_id": 0, "path": 1, "size": 1}).sort("path", 1)
            files = [{"path": f["path"], "size": f.get("size", 0)} async for f in cursor]
            return {"status": "success", "repo": repo_full, "files": files, "count": len(files)}

        # ── READ ──
        elif action == "read":
            repo_full = (args.get("repo") or "").strip()
            path = (args.get("path") or "").strip()
            if not repo_full or not path:
                return {"status": "error", "reason": "read requires 'repo' and 'path'"}

            doc = await workspace_col.find_one(
                {"user_id": user_id, "repo": repo_full, "path": path},
                {"_id": 0, "content": 1, "path": 1, "size": 1, "indexed_at": 1}
            )
            if not doc:
                return {"status": "error", "reason": f"File '{path}' not indexed. Run index first or check path."}

            content = doc["content"]
            truncated = len(content) > 8000
            return {
                "status": "success",
                "repo": repo_full,
                "path": path,
                "content": content[:8000],
                "truncated": truncated,
                "size": doc.get("size", 0),
                "message": f"Showing first 8000 chars" if truncated else "Full file"
            }

        # ── SEARCH ──
        elif action == "search":
            repo_full = (args.get("repo") or "").strip()
            query_str = (args.get("query") or message).strip()
            if not query_str:
                return {"status": "error", "reason": "search requires 'query'"}

            filter_ = {"user_id": user_id, "content": {"$regex": query_str, "$options": "i"}}
            if repo_full:
                filter_["repo"] = repo_full

            cursor = workspace_col.find(filter_, {"_id": 0, "path": 1, "repo": 1, "content": 1}).limit(20)
            results = []
            async for doc in cursor:
                content = doc["content"]
                # Find matching lines
                lines = content.split("\n")
                matches = [{"line": i + 1, "text": l.strip()}
                           for i, l in enumerate(lines)
                           if query_str.lower() in l.lower()][:5]
                results.append({"repo": doc["repo"], "path": doc["path"], "matches": matches})

            return {
                "status": "success",
                "query": query_str,
                "results": results,
                "count": len(results),
                "message": f"Found '{query_str}' in {len(results)} file(s)"
            }

        # ── PATCH ──
        elif action == "patch":
            repo_full = (args.get("repo") or "").strip()
            path = (args.get("path") or "").strip()
            new_content = (args.get("content") or message).strip()
            description = (args.get("description") or "").strip()

            if not repo_full or not path or not new_content:
                return {"status": "error", "reason": "patch requires 'repo', 'path', and 'content'"}

            now = datetime.utcnow()
            result = await patches_col.insert_one({
                "user_id": user_id,
                "repo": repo_full,
                "path": path,
                "content": new_content,
                "description": description,
                "status": "pending",
                "created_at": now,
            })

            return {
                "status": "success",
                "patch_id": str(result.inserted_id),
                "repo": repo_full,
                "path": path,
                "message": f"Patch stored for {path}. Review and apply when ready."
            }

        # ── PATCHES ──
        elif action == "patches":
            repo_full = (args.get("repo") or "").strip()
            filter_ = {"user_id": user_id, "status": "pending"}
            if repo_full:
                filter_["repo"] = repo_full

            cursor = patches_col.find(filter_, {"_id": 1, "repo": 1, "path": 1,
                                                "description": 1, "created_at": 1}).sort("created_at", -1)
            patches = []
            async for p in cursor:
                patches.append({
                    "patch_id": str(p["_id"]),
                    "repo": p["repo"],
                    "path": p["path"],
                    "description": p.get("description", ""),
                    "created_at": p["created_at"].isoformat() if p.get("created_at") else ""
                })
            return {"status": "success", "patches": patches, "count": len(patches)}

        # ── APPLY ──
        elif action == "apply":
            from bson import ObjectId
            patch_id = (args.get("patch_id") or "").strip()
            if not patch_id:
                return {"status": "error", "reason": "apply requires 'patch_id'"}

            patch = await patches_col.find_one({"_id": ObjectId(patch_id), "user_id": user_id})
            if not patch:
                return {"status": "error", "reason": "Patch not found"}

            # Apply patch to workspace
            await workspace_col.update_one(
                {"user_id": user_id, "repo": patch["repo"], "path": patch["path"]},
                {"$set": {"content": patch["content"], "patched_at": datetime.utcnow()}}
            )
            await patches_col.update_one(
                {"_id": ObjectId(patch_id)},
                {"$set": {"status": "applied", "applied_at": datetime.utcnow()}}
            )

            return {
                "status": "success",
                "patch_id": patch_id,
                "path": patch["path"],
                "message": f"Patch applied to workspace. Push to GitHub manually to deploy."
            }

        # ── DROP ──
        elif action == "drop":
            repo_full = (args.get("repo") or "").strip()
            if not repo_full:
                return {"status": "error", "reason": "drop requires 'repo'"}

            result = await workspace_col.delete_many({"user_id": user_id, "repo": repo_full})
            await patches_col.delete_many({"user_id": user_id, "repo": repo_full})
            return {
                "status": "success",
                "repo": repo_full,
                "deleted": result.deleted_count,
                "message": f"Removed {result.deleted_count} files from workspace"
            }

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Use: index, repos, list, read, search, patch, patches, apply, drop"
            }

    except ValueError as e:
        return {"status": "error", "reason": str(e)}
    except Exception as e:
        logger.error(f"Workspace error: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}
