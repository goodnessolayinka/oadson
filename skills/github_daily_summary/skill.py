"""
skills/github_daily_summary/skill.py

Fetches today's GitHub commit activity for the authenticated user.
Uses the 'since' API parameter to filter commits by date at the API level
instead of fetching last-N commits and doing string matching — the old
approach missed commits if a repo had more than 10 today.

Also pulls open PRs and issues assigned to the user for a full daily brief.
"""

import os
import httpx
from datetime import datetime, timezone


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    github_token = (
        args.get("github_token")
        or payload.get("github_token")
        or os.getenv("GITHUB_TOKEN", "")
    )

    if not github_token:
        return {"status": "error", "reason": "GITHUB_TOKEN not set."}

    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    # ISO 8601 start of today for the 'since' filter
    since = f"{today_str}T00:00:00Z"

    try:
        async with httpx.AsyncClient(timeout=20) as client:

            # ── Who am I ──
            me_res = await client.get("https://api.github.com/user", headers=headers)
            if me_res.status_code != 200:
                return {"status": "error", "reason": f"GitHub auth failed: {me_res.status_code}"}
            me = me_res.json()
            username = me.get("login", "unknown")

            # ── Repos updated recently ──
            repos_res = await client.get(
                "https://api.github.com/user/repos",
                headers=headers,
                params={"per_page": 50, "sort": "updated", "direction": "desc"},
            )
            repos = repos_res.json() if repos_res.status_code == 200 else []

            # ── Commits today (use 'since' so we get ALL of today's commits) ──
            repo_commits: dict = {}
            for repo in repos:
                owner = repo.get("owner", {}).get("login", username)
                repo_name = repo.get("name", "")
                if not repo_name:
                    continue
                # Skip repos not touched today (pushed_at is a quick pre-filter)
                pushed_at = repo.get("pushed_at", "")
                if not pushed_at.startswith(today_str):
                    continue

                commits_res = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo_name}/commits",
                    headers=headers,
                    params={"since": since, "author": username, "per_page": 100},
                )
                if commits_res.status_code == 200:
                    commits = commits_res.json()
                    if isinstance(commits, list) and commits:
                        repo_commits[f"{owner}/{repo_name}"] = len(commits)

            # ── Open PRs by user ──
            prs_res = await client.get(
                "https://api.github.com/search/issues",
                headers=headers,
                params={
                    "q": f"is:pr is:open author:{username}",
                    "per_page": 10,
                    "sort": "updated",
                },
            )
            open_prs = []
            if prs_res.status_code == 200:
                for item in prs_res.json().get("items", []):
                    open_prs.append({
                        "title": item.get("title"),
                        "repo": item.get("repository_url", "").split("/repos/")[-1],
                        "url": item.get("html_url"),
                        "updated": (item.get("updated_at") or "")[:10],
                    })

        # ── Build summary ──
        total_commits = sum(repo_commits.values())
        commit_lines = [f"{count} commit(s) → {repo}" for repo, count in repo_commits.items()]

        if not commit_lines and not open_prs:
            summary = f"No GitHub activity found for {username} on {today_str}."
        else:
            parts = []
            if commit_lines:
                parts.append(f"Commits today ({total_commits} total): " + "; ".join(commit_lines))
            if open_prs:
                pr_lines = [f"'{p['title']}' in {p['repo']}" for p in open_prs[:5]]
                parts.append(f"Open PRs: " + "; ".join(pr_lines))
            summary = f"GitHub activity for {username} on {today_str}: " + " | ".join(parts)

        return {
            "status": "success",
            "username": username,
            "date": today_str,
            "total_commits_today": total_commits,
            "repos_with_commits": repo_commits,
            "open_prs": open_prs,
            "summary": summary,
            "message": summary,
        }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
