import subprocess
from typing import Optional

class GitHelperSkill:
    def __init__(self):
        pass

    async def get_status(self, repo_path: str = ".") -> dict:
        """Get git status."""
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return {"status": "error", "reason": "Not a git repository"}

            changes = result.stdout.strip().split("\n") if result.stdout.strip() else []

            return {
                "status": "success",
                "changes": changes,
                "total": len(changes),
                "message": f"{len(changes)} file(s) changed"
            }

        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def get_log(self, repo_path: str = ".", limit: int = 10) -> dict:
        """Get commit history."""
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "log", f"-{limit}", "--oneline"],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return {"status": "error", "reason": "Failed to get log"}

            commits = result.stdout.strip().split("\n") if result.stdout.strip() else []

            return {
                "status": "success",
                "commits": commits,
                "total": len(commits),
                "message": f"Retrieved {len(commits)} commit(s)"
            }

        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def get_branches(self, repo_path: str = ".") -> dict:
        """List all branches."""
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "branch", "-a"],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return {"status": "error", "reason": "Failed to list branches"}

            branches = [b.strip() for b in result.stdout.strip().split("\n")]

            return {
                "status": "success",
                "branches": branches,
                "total": len(branches),
                "message": f"Found {len(branches)} branch(es)"
            }

        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def get_remote(self, repo_path: str = ".") -> dict:
        """Get remote URL."""
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "remote", "-v"],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return {"status": "error", "reason": "Failed to get remote"}

            remotes = result.stdout.strip()

            return {
                "status": "success",
                "remotes": remotes,
                "message": "Remote(s) retrieved"
            }

        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def get_diff(self, repo_path: str = ".") -> dict:
        """Get differences in working directory."""
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "diff"],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return {"status": "error", "reason": "Failed to get diff"}

            diff = result.stdout[:2000]

            return {
                "status": "success",
                "diff": diff,
                "message": "Diff retrieved"
            }

        except Exception as e:
            return {"status": "error", "reason": str(e)}

    async def get_stash(self, repo_path: str = ".") -> dict:
        """Get stashed changes."""
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "stash", "list"],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return {"status": "error", "reason": "Failed to get stash"}

            stashes = result.stdout.strip().split("\n") if result.stdout.strip() else []

            return {
                "status": "success",
                "stashes": stashes,
                "total": len(stashes),
                "message": f"Found {len(stashes)} stash(es)"
            }

        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute git_helper skill with structured args.

    REWRITTEN: previously repo_path was never settable from run() —
    always hardcoded to "." — so this could only ever inspect whatever
    repo happened to be the current working directory, regardless of
    what the user actually asked about. Now exposed as a real arg.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")
    repo_path = (args.get("repo_path") or ".").strip()

    if not action:
        return {
            "status": "info",
            "message": "Git actions: status, log, branches, remote, diff, stash"
        }

    try:
        skill = GitHelperSkill()

        if action == "status":
            return await skill.get_status(repo_path)
        elif action == "log":
            limit = int(args.get("limit") or 10)
            return await skill.get_log(repo_path, limit)
        elif action == "branches":
            return await skill.get_branches(repo_path)
        elif action == "remote":
            return await skill.get_remote(repo_path)
        elif action == "diff":
            return await skill.get_diff(repo_path)
        elif action == "stash":
            return await skill.get_stash(repo_path)
        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: status, log, branches, remote, diff, stash"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
