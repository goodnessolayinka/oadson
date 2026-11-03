"""
skills/github/skill.py — OADSON V3 GitHub Skill.

Actions:
  list_repos       — list a user's repositories
  repo_info        — details about one repo
  commits          — recent commits
  issues           — open/closed issues
  search           — search public repos
  list_files       — directory listing via Contents API
  read_file        — single file text content via Contents API
  write_file       — create/update one file (single commit)
  create_repo      — create a new repository

  clone_to_workspace — download repo zipball → extract into terminal
                       sandbox workspace (core.oadson_home.get_user_home() /
                       repos/{owner}__{repo}/ — see that module for the
                       2026-08-28 workspace restructure).
                       Model then uses @terminal freely for all edits/reads.
                       Returns the extracted file tree so the model knows
                       what's there without burning extra steps.

  push_workspace   — read changed files from the sandbox, push ALL of them
                     in ONE commit via GitHub's Git Trees API.
                     No write_file-per-file loop. No step explosion.
"""

import httpx
import os
import base64
import zipfile
import io
import re
import logging
from pathlib import Path
from typing import Optional

from core.oadson_home import get_user_home

logger = logging.getLogger("oadson.skill.github")

# Files/dirs to never push back to GitHub (build artifacts, secrets, etc.)
PUSH_IGNORE = {
    ".git", "__pycache__", ".env", ".DS_Store", "node_modules",
    ".pyc", ".pyo", ".pyd", ".so", ".egg-info",
}

# Max file size to read and push via the Trees API (GitHub limit: 100MB,
# but we cap lower to keep context manageable)
MAX_PUSH_FILE_BYTES = 1_000_000  # 1MB


def _repo_sandbox_path(user_id: str, owner: str, repo: str) -> Path:
    """
    Returns the sandbox path where a cloned repo lives.

    CASE-NORMALIZED 2026-09-02 — real production failure: the model
    cloned a repo (e.g. clone_to_workspace owner='your-github-user'),
    which built and correctly extracted into
    repos/your-github-user__example-backend/. A later
    @terminal `cd repos/your-github-user__example-backend`
    then failed with "No such file or directory" three times in a row
    with different follow-on commands, purely because the model typed
    the owner/repo segment with different casing between the two calls
    (GitHub usernames/repo names are case-INsensitive on GitHub itself,
    so the model has no reason to treat casing as significant when
    typing them from memory a few turns apart) — but this function
    built the actual folder name from owner/repo verbatim, with zero
    normalization, so two references to the exact same real repo
    produced two DIFFERENT paths on a case-sensitive Linux filesystem.
    Lowercasing both segments here means every caller (clone, and any
    later terminal command reconstructing the same path from memory)
    resolves to the identical folder regardless of which casing was
    used this particular time.
    """
    home = get_user_home(user_id)
    # Double-underscore separator — owner and repo names cannot contain __
    return home / "repos" / f"{owner.lower()}__{repo.lower()}"


def _should_ignore(path: Path) -> bool:
    for part in path.parts:
        if part in PUSH_IGNORE:
            return True
        if part.endswith((".pyc", ".pyo", ".pyd", ".so")):
            return True
    return False


class GitHubSkill:
    def __init__(self, access_token: str = None):
        self.access_token = access_token or os.getenv("GITHUB_TOKEN")
        self.base_url = "https://api.github.com"
        self.headers = {
            "Authorization": f"token {self.access_token}",
            "Accept": "application/vnd.github+json",
        }
        self.error = None if self.access_token else "GITHUB_TOKEN not set in environment"

    # ── READ ACTIONS ──────────────────────────────────────────────────────────

    async def get_user_repos(self, username: str, limit: int = 20) -> dict:
        if self.error:
            return {"status": "error", "reason": self.error}
        try:
            params = {"per_page": limit, "sort": "updated", "direction": "desc"}
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/user/repos",
                    headers=self.headers,
                    params={**params, "affiliation": "owner,collaborator,organization_member"},
                    timeout=15,
                )
                if response.status_code != 200:
                    response = await client.get(
                        f"{self.base_url}/users/{username}/repos",
                        headers=self.headers,
                        params=params,
                        timeout=15,
                    )
                    if response.status_code != 200:
                        return {"status": "error", "reason": f"User not found: {username}"}

                repos = [
                    {
                        "name": r.get("name"),
                        "url": r.get("html_url"),
                        "description": r.get("description"),
                        "stars": r.get("stargazers_count"),
                        "language": r.get("language"),
                        "updated": r.get("updated_at"),
                        "private": r.get("private"),
                    }
                    for r in response.json()
                ]
                return {
                    "status": "success",
                    "username": username,
                    "repos_count": len(repos),
                    "repos": repos,
                    "message": f"Found {len(repos)} repository(ies)",
                }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def get_repo_info(self, owner: str, repo: str) -> dict:
        if self.error:
            return {"status": "error", "reason": self.error}
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{self.base_url}/repos/{owner}/{repo}", headers=self.headers, timeout=15)
                if r.status_code != 200:
                    return {"status": "error", "reason": "Repository not found"}
                d = r.json()
                return {
                    "status": "success",
                    "repo": {
                        "name": d.get("name"),
                        "owner": d.get("owner", {}).get("login"),
                        "url": d.get("html_url"),
                        "description": d.get("description"),
                        "stars": d.get("stargazers_count"),
                        "forks": d.get("forks_count"),
                        "language": d.get("language"),
                        "default_branch": d.get("default_branch"),
                        "created": d.get("created_at"),
                        "updated": d.get("updated_at"),
                        "is_private": d.get("private"),
                    },
                }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def get_repo_commits(self, owner: str, repo: str, limit: int = 10) -> dict:
        if self.error:
            return {"status": "error", "reason": self.error}
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self.base_url}/repos/{owner}/{repo}/commits",
                    headers=self.headers,
                    params={"per_page": limit},
                    timeout=15,
                )
                if r.status_code != 200:
                    return {"status": "error", "reason": "Repository not found"}
                commits = [
                    {
                        "sha": c.get("sha", "")[:7],
                        "message": c.get("commit", {}).get("message", "").split("\n")[0],
                        "author": c.get("commit", {}).get("author", {}).get("name"),
                        "date": c.get("commit", {}).get("author", {}).get("date"),
                    }
                    for c in r.json()
                ]
                return {
                    "status": "success",
                    "repo": f"{owner}/{repo}",
                    "commits": commits,
                    "message": f"Retrieved {len(commits)} commit(s)",
                }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def get_repo_issues(self, owner: str, repo: str, state: str = "open") -> dict:
        if self.error:
            return {"status": "error", "reason": self.error}
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self.base_url}/repos/{owner}/{repo}/issues",
                    headers=self.headers,
                    params={"state": state, "per_page": 20, "sort": "updated", "direction": "desc"},
                    timeout=15,
                )
                if r.status_code != 200:
                    return {"status": "error", "reason": "Repository not found"}
                issues = [
                    {
                        "number": i.get("number"),
                        "title": i.get("title"),
                        "state": i.get("state"),
                        "url": i.get("html_url"),
                        "author": i.get("user", {}).get("login"),
                    }
                    for i in r.json()
                ]
                return {
                    "status": "success",
                    "repo": f"{owner}/{repo}",
                    "state": state,
                    "issues_count": len(issues),
                    "issues": issues,
                    "message": f"Found {len(issues)} {state} issue(s)",
                }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def list_files(self, owner: str, repo: str, path: str = "", ref: Optional[str] = None) -> dict:
        if self.error:
            return {"status": "error", "reason": self.error}
        try:
            params = {"ref": ref} if ref else {}
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self.base_url}/repos/{owner}/{repo}/contents/{path}",
                    headers=self.headers,
                    params=params,
                    timeout=15,
                )
                if r.status_code == 404:
                    return {"status": "error", "reason": f"Path '{path or '/'}' not found in {owner}/{repo}"}
                if r.status_code != 200:
                    return {"status": "error", "reason": f"GitHub returned {r.status_code}"}
                data = r.json()
                if isinstance(data, dict):
                    return {"status": "error", "reason": f"'{path}' is a file — use action='read_file' instead."}
                entries = [
                    {"name": item.get("name"), "type": item.get("type"), "path": item.get("path"), "size": item.get("size")}
                    for item in data
                ]
                return {
                    "status": "success",
                    "repo": f"{owner}/{repo}",
                    "path": path or "/",
                    "entries": entries,
                    "message": f"Found {len(entries)} item(s) in '{path or '/'}'",
                }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def read_file(self, owner: str, repo: str, path: str, ref: Optional[str] = None) -> dict:
        if self.error:
            return {"status": "error", "reason": self.error}
        if not path:
            return {"status": "error", "reason": "read_file requires 'path'"}
        try:
            params = {"ref": ref} if ref else {}
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self.base_url}/repos/{owner}/{repo}/contents/{path}",
                    headers=self.headers,
                    params=params,
                    timeout=15,
                )
                if r.status_code == 404:
                    return {"status": "error", "reason": f"File '{path}' not found in {owner}/{repo}"}
                if r.status_code != 200:
                    return {"status": "error", "reason": f"GitHub returned {r.status_code}"}
                data = r.json()
                if isinstance(data, list):
                    return {"status": "error", "reason": f"'{path}' is a directory — use action='list_files' instead."}
                size = data.get("size", 0)
                if size > 300_000:
                    return {"status": "error", "reason": f"'{path}' is {size} bytes — too large (>300KB). Clone the repo with clone_to_workspace and read it via @terminal instead."}
                raw = data.get("content", "")
                if data.get("encoding") == "base64":
                    try:
                        content = base64.b64decode(raw).decode("utf-8", errors="replace")
                    except Exception:
                        return {"status": "error", "reason": f"'{path}' appears to be binary."}
                else:
                    content = raw
                return {
                    "status": "success",
                    "repo": f"{owner}/{repo}",
                    "path": path,
                    "size": size,
                    "content": content,
                    "message": f"Read {size} bytes from '{path}'",
                }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def write_file(
        self, owner: str, repo: str, path: str, content: str,
        commit_message: str, branch: Optional[str] = None,
    ) -> dict:
        """Single-file commit via Contents API. For multi-file changes use push_workspace instead."""
        if self.error:
            return {"status": "error", "reason": self.error}
        if not path or content is None:
            return {"status": "error", "reason": "write_file requires 'path' and 'content'"}
        try:
            url = f"{self.base_url}/repos/{owner}/{repo}/contents/{path}"
            params = {"ref": branch} if branch else {}
            sha = None
            async with httpx.AsyncClient() as client:
                existing = await client.get(url, headers=self.headers, params=params, timeout=15)
                if existing.status_code == 200:
                    sha = existing.json().get("sha")
                elif existing.status_code not in (404,):
                    return {"status": "error", "reason": f"Could not check existing file: {existing.status_code}"}
                body = {
                    "message": commit_message or f"Update {path} via OADSON",
                    "content": base64.b64encode(content.encode("utf-8")).decode(),
                }
                if sha:
                    body["sha"] = sha
                if branch:
                    body["branch"] = branch
                res = await client.put(url, headers=self.headers, json=body, timeout=15)
                if res.status_code not in (200, 201):
                    return {"status": "error", "reason": f"GitHub write failed: {res.status_code} {res.text[:300]}"}
                data = res.json()
                return {
                    "status": "success",
                    "repo": f"{owner}/{repo}",
                    "path": path,
                    "action": "updated" if sha else "created",
                    "commit_sha": data.get("commit", {}).get("sha"),
                    "html_url": data.get("content", {}).get("html_url"),
                    "message": f"{'Updated' if sha else 'Created'} '{path}' in {owner}/{repo}",
                }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def create_repo(
        self, name: str, description: str = "", private: bool = False,
        auto_init: bool = True, org: Optional[str] = None,
    ) -> dict:
        if self.error:
            return {"status": "error", "reason": self.error}
        if not name or not name.strip():
            return {"status": "error", "reason": "create_repo requires a 'name'"}
        clean_name = re.sub(r"[^\w.-]", "-", name.strip())
        url = f"{self.base_url}/orgs/{org}/repos" if org else f"{self.base_url}/user/repos"
        body = {"name": clean_name, "description": description or "", "private": bool(private), "auto_init": bool(auto_init)}
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(url, headers=self.headers, json=body, timeout=15)
                if res.status_code == 422:
                    return {"status": "error", "reason": f"Repo '{clean_name}' may already exist or name is invalid: {res.text[:200]}"}
                if res.status_code not in (200, 201):
                    return {"status": "error", "reason": f"GitHub repo creation failed: {res.status_code} {res.text[:300]}"}
                data = res.json()
                return {
                    "status": "success",
                    "name": data.get("name"),
                    "full_name": data.get("full_name"),
                    "private": data.get("private"),
                    "html_url": data.get("html_url"),
                    "clone_url": data.get("clone_url"),
                    "default_branch": data.get("default_branch"),
                    "message": f"Created repo '{data.get('full_name')}' ({'private' if data.get('private') else 'public'}).",
                }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def search_repos(self, query: str, limit: int = 10) -> dict:
        if self.error:
            return {"status": "error", "reason": self.error}
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self.base_url}/search/repositories",
                    headers=self.headers,
                    params={"q": query, "per_page": limit, "sort": "stars", "order": "desc"},
                    timeout=15,
                )
                if r.status_code != 200:
                    return {"status": "error", "reason": "Search failed"}
                repos = [
                    {
                        "name": repo.get("full_name"),
                        "url": repo.get("html_url"),
                        "description": repo.get("description"),
                        "stars": repo.get("stargazers_count"),
                        "language": repo.get("language"),
                    }
                    for repo in r.json().get("items", [])
                ]
                return {
                    "status": "success",
                    "query": query,
                    "results": len(repos),
                    "repos": repos,
                    "message": f"Found {len(repos)} repo(s) matching '{query}'",
                }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    # ── WORKSPACE ACTIONS ─────────────────────────────────────────────────────

    # MEMORY SAFETY 2026-08-28 — this app runs on a 512MB box. Before
    # this fix, clone_to_workspace had NO size limit at all: httpx's
    # plain client.get() buffers the ENTIRE response body into one
    # Python bytes object before returning, so a repo with a large
    # committed binary, a monorepo, or an accidentally-committed
    # node_modules/build dir could pull tens or hundreds of MB into RAM
    # in one shot — on a box this size, shared with the live API
    # server, Mongo driver, and every other in-flight request, that's a
    # real OOM path, not a theoretical one (Render's OOM killer acts on
    # the WHOLE container, not per-request — see api/remote_workspace.py
    # for a previous real incident of exactly this). Matters more now
    # that terminal's restart-recovery (see its maybe_reclone()) can
    # trigger a clone automatically, without the model choosing to.
    # SIZE CAP LOWERED 2026-09-01: was 40MB compressed / 100MB free-
    # memory floor. Lowered together, proportionally (same ~2.5x ratio
    # between the zip cap and the required headroom — see the memory
    # check just below), so the actual safety margin this pair
    # protects doesn't get thinner, just the whole threshold moves
    # down: 16MB compressed source commonly decompresses to 50-80MB
    # once extracted (see the memory-check comment below for the same
    # reasoning at the old numbers) — still needs real headroom, just
    # less of it, making clone_to_workspace usable more often on this
    # box without weakening the actual protection against a genuinely
    # oversized repo (huge committed binaries, an accidental
    # node_modules/build dir, a monorepo) still OOMing the whole
    # container. 16MB still comfortably covers the large majority of
    # real, non-binary-heavy source repos.
    MAX_ZIPBALL_BYTES = 16 * 1024 * 1024  # 16MB compressed

    async def delete_workspace(self, owner: str, repo: str, user_id: str) -> dict:
        """
        Delete one repo cloned by clone_to_workspace from this user's sandbox.

        Only removes repos/<owner>__<repo> (same path _repo_sandbox_path builds),
        and only if that folder sits directly inside the user's repos dir.
        Local edits in that folder that were never pushed are lost. The
        GitHub repo itself is not touched.
        """
        import shutil
        workspace = _repo_sandbox_path(user_id, owner, repo).resolve()
        allowed_repos_dir = get_user_home(user_id).joinpath("repos").resolve()
        if workspace.parent != allowed_repos_dir or "__" not in workspace.name:
            return {"status": "error", "reason": "refusing to delete: path is not a cloned repo folder"}
        if not workspace.exists():
            return {
                "status": "success",
                "deleted": False,
                "repo": f"{owner}/{repo}",
                "message": "No cloned copy of this repo exists in the workspace. Nothing deleted.",
            }
        try:
            shutil.rmtree(workspace)
        except Exception as e:
            return {"status": "error", "reason": f"delete failed: {e}"}
        return {
            "status": "success",
            "deleted": True,
            "repo": f"{owner}/{repo}",
            "message": f"Deleted local clone of {owner}/{repo} from the workspace.",
        }

    async def clone_to_workspace(
        self, owner: str, repo: str, user_id: str, ref: Optional[str] = None, db=None,
    ) -> dict:
        """
        Download the repo as a zipball from GitHub and extract it into the
        terminal sandbox workspace (core.oadson_home.get_user_home() /
        repos/{owner}__{repo}/).

        After this returns, the model should use @terminal to work on the
        files directly — no more read_file-per-file API calls.

        Returns the extracted file tree (paths only, not content) so the
        model knows the structure without burning extra steps.

        ref: branch, tag, or commit SHA. Defaults to the repo's default branch.
        db: optional Mongo handle, used only for the memory pre-flight
        check's crash-surviving event log (core.oadson_home.log_memory_event)
        — the check itself still runs and still blocks correctly with
        db=None, it just won't have anywhere durable to record that it
        did. Callers that have db in payload (run() below) should pass
        it; callers that don't (e.g. terminal's restart-recovery, which
        doesn't have a request payload to read db from) are fine
        omitting it — the crash-prevention behavior is unaffected either way.
        """
        if self.error:
            return {"status": "error", "reason": self.error}

        from core.oadson_home import has_memory_headroom, log_memory_event
        # LOWERED 2026-09-01 alongside MAX_ZIPBALL_BYTES above — see that
        # constant's comment for the full reasoning on why these two move
        # together, proportionally, rather than one getting lowered alone.
        ok, headroom_mb = has_memory_headroom(min_mb=40)
        await log_memory_event(db, "clone_to_workspace_start", user_id, {"owner": owner, "repo": repo, "headroom_mb": headroom_mb})

        # RETRY-WITH-BACKOFF 2026-08-31: "not enough memory right now" is
        # very often transient — another in-flight request finishing, a
        # GC pass, a prior clone's temp data getting released — not a
        # permanent state of the box. The old behavior gave up on the
        # very first check and told the model to "try again in a
        # moment", but never actually tried again itself within the
        # same call — meaning the model had to notice that instruction,
        # decide to re-call clone_to_workspace itself, and burn a whole
        # extra step-budget step doing so, for a condition that a couple
        # seconds of waiting frequently resolves on its own. Three short
        # checks over ~6 seconds, still well within a tool call's normal
        # timeout, catches the common transient case without the model
        # needing to do anything — only a genuinely, persistently low-
        # memory box falls through to the hard refusal below.
        if not ok:
            import asyncio
            for _ in range(2):
                await asyncio.sleep(3)
                ok, headroom_mb = has_memory_headroom(min_mb=40)
                if ok:
                    break

        if not ok:
            await log_memory_event(db, "clone_to_workspace_blocked_low_memory", user_id, {"owner": owner, "repo": repo, "headroom_mb": headroom_mb})
            return {
                "status": "error",
                "reason": (
                    f"Not enough free memory to clone {owner}/{repo} right now "
                    f"({headroom_mb}MB available, need 40MB — checked 3 times over ~6s, "
                    "still not enough). This server runs on a tight memory budget. "
                    "DO NOT just tell the user cloning failed and stop there — do this instead:\n"
                    f"1. Use @github action='list_files' owner='{owner}' repo='{repo}' to see the "
                    "repo's structure without downloading anything.\n"
                    f"2. Use @github action='read_file' owner='{owner}' repo='{repo}' path='...' on "
                    "the SPECIFIC file(s) you actually need — this reads one file at a time via the "
                    "API directly, no bulk download, works fine even when a full clone can't.\n"
                    "3. If you need to WRITE a change back, @github action='write_file' commits a "
                    "single file directly the same way — you don't need a full workspace clone for "
                    "a small, targeted edit to one or two files.\n"
                    "Only tell the user cloning genuinely isn't possible right now if the task truly "
                    "needs bulk multi-file work (e.g. a full push_workspace-based restructure) that "
                    "list_files/read_file/write_file can't substitute for."
                ),
            }

        workspace = _repo_sandbox_path(user_id, owner, repo)

        # AUTO-CLEANUP 2026-09-19 — companion to the 2026-09-02 case-
        # normalization fix above. That fix made every NEW path
        # correctly lowercased, but did nothing about folders that
        # already existed under the OLD, unnormalized casing from
        # before it shipped (or from terminal/skill.py's maybe_reclone,
        # which independently rebuilt this same path without the
        # lowercasing — see that file's matching fix) — those old
        # folders just sat there forever, side by side with the new
        # correct one, so the model could reference either depending
        # on which casing it happened to guess. Confirmed in production:
        # repos/ held BOTH your-github-user__example-repo/ (stale,
        # original casing) and your-github-user__example-repo/
        # (correct) simultaneously.
        #
        # Runs on every clone, unconditionally — not a one-time manual
        # cleanup — so this class of duplicate can never re-accumulate,
        # regardless of how it happened (this fix, a future bug, or a
        # manually-created folder). Scans by case-insensitive match on
        # the SAME owner/repo only — never touches an unrelated repo's
        # folder.
        try:
            repos_dir = workspace.parent
            target_name = workspace.name  # already lowercased by _repo_sandbox_path
            if repos_dir.is_dir():
                for existing in repos_dir.iterdir():
                    if existing.is_dir() and existing.name.lower() == target_name and existing.name != target_name:
                        import shutil as _shutil
                        _shutil.rmtree(existing, ignore_errors=True)
        except Exception:
            # Best-effort — a failure here should never block the actual
            # clone the model asked for; worst case, a stale duplicate
            # survives one more cycle and gets caught next clone.
            pass

        # If already cloned this session, wipe and re-clone so the
        # model is always working on a clean, known-good copy.
        if workspace.exists():
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True, exist_ok=True)

        zip_ref = ref or ""  # empty string → GitHub uses default branch
        zip_url = f"{self.base_url}/repos/{owner}/{repo}/zipball/{zip_ref}"

        try:
            # Stream instead of client.get() — read in chunks and abort
            # the moment the cap is exceeded, instead of buffering the
            # whole (possibly huge) body first and checking size after
            # the RAM is already spent.
            zip_chunks = []
            total = 0
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                async with client.stream("GET", zip_url, headers=self.headers) as r:
                    if r.status_code == 404:
                        return {"status": "error", "reason": f"Repo {owner}/{repo} not found or branch '{ref}' doesn't exist."}
                    if r.status_code != 200:
                        return {"status": "error", "reason": f"GitHub zipball download failed: {r.status_code}"}
                    content_length = r.headers.get("content-length")
                    if content_length and int(content_length) > self.MAX_ZIPBALL_BYTES:
                        return {
                            "status": "error",
                            "reason": (
                                f"{owner}/{repo} zipball is {int(content_length) // (1024*1024)}MB, "
                                f"over the {self.MAX_ZIPBALL_BYTES // (1024*1024)}MB clone limit for this "
                                "server's memory budget. Use @github read_file/list_files on specific "
                                "paths instead of cloning the whole repo."
                            ),
                        }
                    async for chunk in r.aiter_bytes():
                        total += len(chunk)
                        if total > self.MAX_ZIPBALL_BYTES:
                            return {
                                "status": "error",
                                "reason": (
                                    f"{owner}/{repo} zipball exceeded the "
                                    f"{self.MAX_ZIPBALL_BYTES // (1024*1024)}MB clone limit for this "
                                    "server's memory budget (stopped mid-download). Use @github "
                                    "read_file/list_files on specific paths instead of cloning the whole repo."
                                ),
                            }
                        zip_chunks.append(chunk)
            zip_bytes = b"".join(zip_chunks)
            del zip_chunks

        except Exception as e:
            return {"status": "error", "reason": f"Download failed: {e}"}

        # Extract — GitHub zips have a top-level prefix dir like
        # "owner-repo-{sha}/" — strip it so workspace/ IS the repo root.
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                members = zf.namelist()
                # Find the common prefix (always exactly one top-level dir)
                prefix = members[0].split("/")[0] + "/" if members else ""
                extracted_count = 0
                for member in members:
                    if not member.startswith(prefix):
                        continue
                    relative = member[len(prefix):]
                    if not relative:  # the prefix dir itself
                        continue
                    target = workspace / relative
                    if member.endswith("/"):
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(zf.read(member))
                        extracted_count += 1
        except Exception as e:
            return {"status": "error", "reason": f"Extraction failed: {e}"}

        # Build a file tree for the model (paths only, no content)
        tree = []
        for p in sorted(workspace.rglob("*")):
            if _should_ignore(p.relative_to(workspace)):
                continue
            rel = str(p.relative_to(workspace))
            tree.append({"path": rel, "type": "dir" if p.is_dir() else "file", "size": p.stat().st_size if p.is_file() else None})

        workspace_path = str(workspace)

        # RESTART RESILIENCE 2026-08-28 — mark this user's workspace as
        # known-good for the CURRENT process now that a real clone just
        # succeeded, so terminal's auto-reclone check (see its
        # _find_repo_reference/needs_reclone usage) doesn't immediately
        # treat this fresh clone as stale on the very next call.
        from core.oadson_home import mark_fresh
        mark_fresh(user_id)

        # REPO INDEX — index the extracted clone so chat turns can retrieve
        # relevant files. Best-effort: skipped on low memory, never fails the clone.
        try:
            from core import repo_index as _ri
            import asyncio as _aio
            ok_mem, _mb = has_memory_headroom(min_mb=80)
            if ok_mem:
                key = f"{user_id}:gh:{owner}/{repo}@{ref or 'default'}".lower()
                idx = await _aio.to_thread(_ri.ingest_dir, key, workspace_path, user_id)
                await _ri.save(db, idx, user_id)
        except Exception as _e:
            import logging as _lg
            _lg.getLogger("oadson.github").warning(f"repo index skipped for {owner}/{repo}: {_e}")

        return {
            "status": "success",
            "repo": f"{owner}/{repo}",
            "ref": ref or "default branch",
            "workspace": workspace_path,
            "files_extracted": extracted_count,
            "tree": tree[:200],
            "tree_truncated": len(tree) > 200,
            "message": (
                f"Cloned {owner}/{repo} ({extracted_count} files) into workspace at {workspace_path}. "
                f"Use @terminal to read, edit, and run files in that directory. "
                f"IMPORTANT: this workspace has NO .git directory — git log/status/diff will fail. "
                f"For commit history use github action='commits'. For edits use @terminal then push_workspace. "
                f"When done with all edits, call github action='push_workspace' to commit everything in one shot."
            ),
        }

    async def push_workspace(
        self, owner: str, repo: str, user_id: str,
        commit_message: str, branch: Optional[str] = None,
        paths: Optional[list] = None,
    ) -> dict:
        """
        Read all changed files from the terminal sandbox and push them to
        GitHub in ONE commit using the Git Trees API.

        This replaces the old write_file-per-file loop that burned a step
        per file and created a separate commit for each one.

        paths: optional list of relative file paths to push. If omitted,
               pushes ALL non-ignored files in the workspace. Use this
               when you only changed a subset and don't want to re-push
               unchanged files.

        Flow:
          1. Get current HEAD commit SHA + tree SHA
          2. Build a new tree with all changed file blobs
          3. Create a new commit pointing at that tree
          4. Fast-forward the branch ref to the new commit
        """
        if self.error:
            return {"status": "error", "reason": self.error}

        workspace = _repo_sandbox_path(user_id, owner, repo)
        if not workspace.exists():
            return {
                "status": "error",
                "reason": (
                    f"No workspace found for {owner}/{repo}. "
                    "Run action='clone_to_workspace' first."
                ),
            }

        commit_message = commit_message or f"OADSON: update {owner}/{repo}"

        try:
            async with httpx.AsyncClient(timeout=30) as client:

                # Step 1 — get the repo's default branch and current HEAD
                repo_r = await client.get(
                    f"{self.base_url}/repos/{owner}/{repo}",
                    headers=self.headers,
                )
                if repo_r.status_code != 200:
                    return {"status": "error", "reason": f"Could not fetch repo info: {repo_r.status_code}"}
                repo_data = repo_r.json()
                target_branch = branch or repo_data.get("default_branch", "main")

                # Get the current HEAD commit on the target branch
                ref_r = await client.get(
                    f"{self.base_url}/repos/{owner}/{repo}/git/ref/heads/{target_branch}",
                    headers=self.headers,
                )
                if ref_r.status_code != 200:
                    return {"status": "error", "reason": f"Branch '{target_branch}' not found: {ref_r.status_code}"}
                base_commit_sha = ref_r.json()["object"]["sha"]

                # Get the tree SHA of the base commit
                commit_r = await client.get(
                    f"{self.base_url}/repos/{owner}/{repo}/git/commits/{base_commit_sha}",
                    headers=self.headers,
                )
                if commit_r.status_code != 200:
                    return {"status": "error", "reason": f"Could not fetch base commit: {commit_r.status_code}"}
                base_tree_sha = commit_r.json()["tree"]["sha"]

                # Step 2 — collect files to push
                if paths:
                    # Explicit list — only push these
                    files_to_push = []
                    for p in paths:
                        full = workspace / p
                        if full.is_file():
                            files_to_push.append(full)
                        else:
                            logger.warning(f"push_workspace: path '{p}' not found in workspace, skipping")
                else:
                    # Push everything non-ignored
                    files_to_push = [
                        p for p in workspace.rglob("*")
                        if p.is_file() and not _should_ignore(p.relative_to(workspace))
                    ]

                if not files_to_push:
                    return {"status": "error", "reason": "No files found in workspace to push."}

                # Step 3 — build the tree payload
                # Files <= 1MB: inline blob in the tree
                # Files > 1MB: create a blob object first, reference by SHA
                tree_items = []
                skipped = []

                for file_path in files_to_push:
                    rel = str(file_path.relative_to(workspace))

                    try:
                        raw_bytes = file_path.read_bytes()
                    except Exception as e:
                        skipped.append({"path": rel, "reason": f"read error: {e}"})
                        continue

                    if len(raw_bytes) > MAX_PUSH_FILE_BYTES:
                        skipped.append({"path": rel, "reason": f"too large ({len(raw_bytes)} bytes, max {MAX_PUSH_FILE_BYTES})"})
                        continue

                    # Try UTF-8 first — text file → inline content
                    try:
                        text_content = raw_bytes.decode("utf-8")
                        tree_items.append({
                            "path": rel,
                            "mode": "100644",
                            "type": "blob",
                            "content": text_content,
                        })
                    except UnicodeDecodeError:
                        # Binary file — must create a blob first
                        blob_r = await client.post(
                            f"{self.base_url}/repos/{owner}/{repo}/git/blobs",
                            headers=self.headers,
                            json={"content": base64.b64encode(raw_bytes).decode(), "encoding": "base64"},
                        )
                        if blob_r.status_code not in (200, 201):
                            skipped.append({"path": rel, "reason": f"blob creation failed: {blob_r.status_code}"})
                            continue
                        tree_items.append({
                            "path": rel,
                            "mode": "100644",
                            "type": "blob",
                            "sha": blob_r.json()["sha"],
                        })

                if not tree_items:
                    return {
                        "status": "error",
                        "reason": "All files were skipped (too large or unreadable). Nothing pushed.",
                        "skipped": skipped,
                    }

                # Step 4 — create the new tree
                new_tree_r = await client.post(
                    f"{self.base_url}/repos/{owner}/{repo}/git/trees",
                    headers=self.headers,
                    json={"base_tree": base_tree_sha, "tree": tree_items},
                )
                if new_tree_r.status_code not in (200, 201):
                    return {"status": "error", "reason": f"Tree creation failed: {new_tree_r.status_code} {new_tree_r.text[:300]}"}
                new_tree_sha = new_tree_r.json()["sha"]

                # Step 5 — create the commit
                new_commit_r = await client.post(
                    f"{self.base_url}/repos/{owner}/{repo}/git/commits",
                    headers=self.headers,
                    json={
                        "message": commit_message,
                        "tree": new_tree_sha,
                        "parents": [base_commit_sha],
                    },
                )
                if new_commit_r.status_code not in (200, 201):
                    return {"status": "error", "reason": f"Commit creation failed: {new_commit_r.status_code} {new_commit_r.text[:300]}"}
                new_commit_sha = new_commit_r.json()["sha"]

                # Step 6 — fast-forward the branch ref
                update_ref_r = await client.patch(
                    f"{self.base_url}/repos/{owner}/{repo}/git/refs/heads/{target_branch}",
                    headers=self.headers,
                    json={"sha": new_commit_sha, "force": False},
                )
                if update_ref_r.status_code not in (200, 201):
                    return {"status": "error", "reason": f"Ref update failed: {update_ref_r.status_code} {update_ref_r.text[:300]}"}

                pushed_paths = [item["path"] for item in tree_items]
                return {
                    "status": "success",
                    "repo": f"{owner}/{repo}",
                    "branch": target_branch,
                    "commit_sha": new_commit_sha,
                    "commit_url": f"https://github.com/{owner}/{repo}/commit/{new_commit_sha}",
                    "files_pushed": len(pushed_paths),
                    "pushed": pushed_paths,
                    "skipped": skipped,
                    "message": (
                        f"Pushed {len(pushed_paths)} file(s) to {owner}/{repo}:{target_branch} "
                        f"in one commit ({new_commit_sha[:7]}). "
                        + (f"{len(skipped)} file(s) skipped." if skipped else "")
                    ),
                }

        except Exception as e:
            return {"status": "error", "reason": str(e)}


# ── ENTRY POINT ──────────────────────────────────────────────────────────────

async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    action = str(args.get("action") or "").strip().lower()
    user_id = payload.get("user_id")
    if action == "search_code":
        return await _search_code(user_id, args)
    if action == "index_archive":
        return await _index_archive(user_id, args)
    if action == "repo_map":
        return await _repo_map(user_id, args)
    if action == "read_file":
        return await _read_file(user_id, args)
    return await _run_api(payload)


async def _run_api(payload: dict) -> dict:
    github_token = payload.get("github_token")
    user_id = payload.get("user_id")
    db = payload.get("db")
    args = payload.get("args", {}) or {}

    try:
        skill = GitHubSkill(access_token=github_token)
        if skill.error:
            return {"status": "error", "reason": skill.error}

        action = args.get("action")
        if not action:
            return {
                "status": "info",
                "message": (
                    "GitHub actions: list_repos, repo_info, commits, issues, search, "
                    "list_files, read_file, write_file (single file), create_repo, "
                    "clone_to_workspace (download whole repo → terminal sandbox), "
                    "push_workspace (push all sandbox changes in one commit)."
                ),
            }

        if action == "list_repos":
            username = (args.get("username") or "").strip()
            if not username:
                return {"status": "error", "reason": "list_repos requires 'username'"}
            return await skill.get_user_repos(username)

        elif action == "repo_info":
            owner = (args.get("owner") or "").strip()
            repo  = (args.get("repo")  or "").strip()
            if not owner or not repo:
                return {"status": "error", "reason": "repo_info requires 'owner' and 'repo'"}
            return await skill.get_repo_info(owner, repo)

        elif action == "commits":
            owner = (args.get("owner") or "").strip()
            repo  = (args.get("repo")  or "").strip()
            if not owner or not repo:
                return {"status": "error", "reason": "commits requires 'owner' and 'repo'"}
            return await skill.get_repo_commits(owner, repo)

        elif action == "issues":
            owner = (args.get("owner") or "").strip()
            repo  = (args.get("repo")  or "").strip()
            state = (args.get("state") or "open").strip()
            if not owner or not repo:
                return {"status": "error", "reason": "issues requires 'owner' and 'repo'"}
            return await skill.get_repo_issues(owner, repo, state)

        elif action == "list_files":
            owner = (args.get("owner") or "").strip()
            repo  = (args.get("repo")  or "").strip()
            path  = (args.get("path")  or "").strip()
            ref   = (args.get("ref")   or "").strip() or None
            if not owner or not repo:
                return {"status": "error", "reason": "list_files requires 'owner' and 'repo'"}
            return await skill.list_files(owner, repo, path, ref)

        elif action == "read_file":
            owner = (args.get("owner") or "").strip()
            repo  = (args.get("repo")  or "").strip()
            path  = (args.get("path")  or "").strip()
            ref   = (args.get("ref")   or "").strip() or None
            if not owner or not repo:
                return {"status": "error", "reason": "read_file requires 'owner' and 'repo'"}
            if not path:
                return {"status": "error", "reason": "read_file requires 'path'"}
            return await skill.read_file(owner, repo, path, ref)

        elif action == "write_file":
            owner   = (args.get("owner")          or "").strip()
            repo    = (args.get("repo")           or "").strip()
            path    = (args.get("path")           or "").strip()
            content = args.get("content", "")
            msg     = (args.get("commit_message") or "").strip()
            branch  = (args.get("branch")         or "").strip() or None
            if not owner or not repo:
                return {"status": "error", "reason": "write_file requires 'owner' and 'repo'"}
            if not path:
                return {"status": "error", "reason": "write_file requires 'path'"}
            return await skill.write_file(owner, repo, path, content, msg, branch)

        elif action == "create_repo":
            name        = (args.get("name")        or "").strip()
            description = (args.get("description") or "").strip()
            private     = bool(args.get("private", False))
            org         = (args.get("org")         or "").strip() or None
            if not name:
                return {"status": "error", "reason": "create_repo requires 'name'"}
            return await skill.create_repo(name, description, private, auto_init=True, org=org)

        elif action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires 'query'"}
            return await skill.search_repos(query)

        elif action == "clone_to_workspace":
            owner = (args.get("owner") or "").strip()
            repo  = (args.get("repo")  or "").strip()
            ref   = (args.get("ref")   or "").strip() or None
            if not owner or not repo:
                return {"status": "error", "reason": "clone_to_workspace requires 'owner' and 'repo'"}
            if not user_id:
                return {"status": "error", "reason": "clone_to_workspace requires user_id (not available in this context)"}
            return await skill.clone_to_workspace(owner, repo, user_id, ref, db=db)

        elif action == "delete_workspace":
            owner = (args.get("owner") or "").strip()
            repo  = (args.get("repo")  or "").strip()
            if not owner or not repo:
                return {"status": "error", "reason": "delete_workspace requires 'owner' and 'repo'"}
            if not user_id:
                return {"status": "error", "reason": "delete_workspace requires user_id"}
            return await skill.delete_workspace(owner, repo, user_id)
        elif action == "push_workspace":
            owner   = (args.get("owner")          or "").strip()
            repo    = (args.get("repo")           or "").strip()
            msg     = (args.get("commit_message") or "").strip()
            branch  = (args.get("branch")         or "").strip() or None
            paths   = args.get("paths") or None  # optional list[str]
            if not owner or not repo:
                return {"status": "error", "reason": "push_workspace requires 'owner' and 'repo'"}
            if not msg:
                return {"status": "error", "reason": "push_workspace requires 'commit_message'"}
            if not user_id:
                return {"status": "error", "reason": "push_workspace requires user_id"}
            return await skill.push_workspace(owner, repo, user_id, msg, branch, paths)

        else:
            return {
                "status": "error",
                "reason": (
                    f"Unknown action '{action}'. Valid actions: list_repos, repo_info, "
                    "commits, issues, search, list_files, read_file, write_file, "
                    "create_repo, clone_to_workspace, push_workspace, delete_workspace."
                ),
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}



ARCHIVE_HOSTS = ("res.cloudinary.com",)
MAX_ARCHIVE_BYTES = 60 * 1024 * 1024
MAX_MEMBER_BYTES = 2 * 1024 * 1024


def _safe_name(text: str) -> str:
    import re as _re
    return _re.sub(r"[^A-Za-z0-9._-]", "_", text)[:60].strip("._") or "upload"


def _workspace_root(user_id, repo_arg: str):
    from core.oadson_home import get_user_home
    if repo_arg.startswith("upload:"):
        return get_user_home(str(user_id)) / "workspace" / "uploads" / _safe_name(repo_arg[len("upload:"):])
    return get_user_home(str(user_id)) / "workspace" / "repos" / repo_arg.replace("/", "__")


async def _index_archive(user_id, args: dict) -> dict:
    """Extract an uploaded zip into the user's workspace. Only code files, no path escapes."""
    import base64
    import io
    import os as _os
    import zipfile
    from pathlib import Path as _Path
    from urllib.parse import urlparse
    import httpx
    from core.repo_index import CODE_EXTS, SKIP_DIRS
    if not user_id:
        return {"status": "error", "reason": "user_id is required"}
    name = _safe_name(str(args.get("name") or "upload.zip").rsplit("/", 1)[-1])
    data = None
    if args.get("archive_b64"):
        try:
            data = base64.b64decode(str(args["archive_b64"]))
        except Exception:
            return {"status": "error", "reason": "archive_b64 is not valid base64"}
    elif args.get("archive_url"):
        url = str(args["archive_url"])
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ARCHIVE_HOSTS:
            return {"status": "error", "reason": "archive_url must be an https link from the upload host"}
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return {"status": "error", "reason": f"download failed: HTTP {r.status_code}"}
        data = r.content
    if not data:
        return {"status": "error", "reason": "no archive was given"}
    if len(data) > MAX_ARCHIVE_BYTES:
        return {"status": "error", "reason": "the archive is larger than 60 MB"}
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return {"status": "error", "reason": "that file is not a zip archive"}
    root = _workspace_root(user_id, f"upload:{name}")
    root.mkdir(parents=True, exist_ok=True)
    base = str(root.resolve())
    extracted = skipped = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        parts = _Path(info.filename).parts
        target = (root / info.filename).resolve()
        if not str(target).startswith(base + _os.sep):      # zip-slip: refuse path escapes
            skipped += 1
            continue
        if _Path(info.filename).suffix.lower() not in CODE_EXTS or info.file_size > MAX_MEMBER_BYTES \
                or any(p in SKIP_DIRS for p in parts):
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(info))
        extracted += 1
    return {"status": "success", "folder": f"upload:{name}",
            "files_extracted": extracted, "files_skipped": skipped}


async def _repo_map(user_id, args: dict) -> dict:
    from core import repo_index as ri
    repo_arg = str(args.get("repo") or "").strip()
    if not user_id or not repo_arg:
        return {"status": "error", "reason": "repo_map needs 'repo' (owner/repo or upload:<name>)"}
    root = _workspace_root(user_id, repo_arg)
    if not root.is_dir():
        return {"status": "error", "reason": f"{repo_arg} is not in this workspace yet"}
    idx = ri.ingest_dir(f"{user_id}:{root.name}", str(root), str(user_id))
    return {"status": "success", "files_indexed": len(idx.files), "map": idx.repo_map()}


async def _search_code(user_id, args: dict) -> dict:
    """Search a repo or upload already in the user's workspace. Local only: no GitHub call."""
    from core import repo_index as ri
    repo_arg = str(args.get("repo") or "").strip()
    query = str(args.get("query") or "").strip()
    if not user_id or not repo_arg or not query:
        return {"status": "error", "reason": "search_code needs 'repo' (owner/repo or upload:<name>) and 'query'"}
    root = _workspace_root(user_id, repo_arg)
    if not root.is_dir():
        return {"status": "error", "reason": f"{repo_arg} is not in this workspace yet",
                "explanation": "Clone it with github clone_to_workspace, or upload the zip again."}
    idx = ri.ingest_dir(f"{user_id}:{root.name}", str(root), str(user_id))
    snippets = idx.snippets_for(query)
    return {"status": "success", "repo": repo_arg, "files_indexed": len(idx.files),
            "snippets": snippets or "(no matches for that query)"}


async def _read_file(user_id, args: dict) -> dict:
    """Read a file, or part of one, from a workspace folder. Read-only, and never outside it."""
    import os as _os
    repo_arg = str(args.get("repo") or "").strip()
    rel = str(args.get("path") or "").strip().lstrip("/")
    if not user_id or not repo_arg or not rel:
        return {"status": "error", "reason": "read_file needs 'repo' and 'path'"}
    root = _workspace_root(user_id, repo_arg)
    if not root.is_dir():
        return {"status": "error", "reason": f"{repo_arg} is not in this workspace yet"}
    target = (root / rel).resolve()
    if not str(target).startswith(str(root.resolve()) + _os.sep) or not target.is_file():
        return {"status": "error", "reason": "that path is not a file in this workspace"}
    if target.stat().st_size > 400_000:
        return {"status": "error", "reason": "that file is too large to read in one piece"}
    try:
        start = max(1, int(args.get("start_line") or 1))
        count = min(400, max(1, int(args.get("max_lines") or 200)))
    except (TypeError, ValueError):
        return {"status": "error", "reason": "start_line and max_lines must be whole numbers"}
    lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
    chunk = lines[start - 1:start - 1 + count]
    numbered = "\n".join(f"{start + i}: {line}" for i, line in enumerate(chunk))
    return {"status": "success", "path": rel, "start_line": start, "lines_returned": len(chunk),
            "total_lines": len(lines), "content": numbered[:20000]}
