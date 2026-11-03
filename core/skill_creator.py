"""
core/skill_creator.py — Phase 10-adjacent, Engineering Roadmap Problem 3,
built to your explicit safety requirement: "should only create skills by
my command and fully explain them before creation."

Two-step flow, deliberately NOT autonomous:
  1. propose_skill() — one Gemini call designs a skill (name, manifest,
     full skill.py source) and returns a COMPLETE EXPLANATION plus a
     proposal_id. Nothing is written to disk yet. This is only ever
     invoked when you explicitly ask for it via the skill_creator skill
     — nothing in the orchestrator auto-detects "missing capability" and
     calls this on its own. If you never ask, this never fires.
  2. create_skill_from_proposal() — a SEPARATE, later call, given the
     proposal_id from step 1, that actually writes the files and
     registers the skill. Requires you to have seen the explanation from
     step 1 and explicitly ask to proceed — there is no single call that
     both proposes and creates.

Persistence, because Render's filesystem is ephemeral: a skill written
to skills/<name>/ would vanish on the next spin-down/restart/redeploy if
that were the only copy. So create_skill_from_proposal() ALSO writes the
same manifest+code into a `generated_skills` Mongo collection, and
bootstrap.py re-materializes every doc in that collection back onto disk
BEFORE the normal skill scan runs on every startup — so a self-created
skill survives exactly like everything else in this system does (state
lives in Mongo, disk is just a cache of it).
"""

import ast
import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

import httpx

logger = logging.getLogger("oadson.skill_creator")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _valid_identifier(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{2,40}", name or ""))


async def _commit_skill_to_github(skill_name: str, manifest: dict, code: str) -> dict | None:
    """
    Commits the new skill's two files to GitHub — same repo write_file
    added to the github skill. Configured via
    SKILL_CREATOR_GITHUB_OWNER / SKILL_CREATOR_GITHUB_REPO env vars.

    Returns None (not a dict) if no target repo is configured at all —
    that's a deliberate "feature not set up" state, distinct from an
    actual failed attempt. Never raises — a GitHub commit failure must
    never undo or block the actual skill creation, which already
    succeeded on the running server and in Mongo by the time this runs.
    """
    owner = os.getenv("SKILL_CREATOR_GITHUB_OWNER")
    repo = os.getenv("SKILL_CREATOR_GITHUB_REPO")
    if not owner or not repo:
        return None

    try:
        from skills.github.skill import GitHubSkill
        github = GitHubSkill(access_token=os.getenv("GITHUB_TOKEN"))

        commit_message = f"Add auto-generated skill: {skill_name} (via skill_creator)"

        code_result = await github.write_file(
            owner, repo, f"skills/{skill_name}/skill.py", code, commit_message,
        )
        if code_result.get("status") != "success":
            return code_result

        manifest_result = await github.write_file(
            owner, repo, f"skills/{skill_name}/manifest.json",
            json.dumps(manifest, indent=2), commit_message,
        )
        if manifest_result.get("status") != "success":
            return manifest_result

        return {"status": "success", "repo": f"{owner}/{repo}"}

    except Exception as e:
        logger.warning(f"GitHub commit failed for skill '{skill_name}' (non-fatal): {e}")
        return {"status": "error", "reason": str(e)}


async def propose_skill(api_key: str, model: str, db, user_id: str, capability_description: str) -> dict:
    """
    Designs a new skill via one Gemini call. Writes nothing to disk.
    Returns a full explanation the calling skill should relay to you
    BEFORE anything is created — that's the actual point of this being
    a separate step from create_skill_from_proposal().
    """
    if not capability_description or not capability_description.strip():
        return {"status": "error", "reason": "capability_description is required"}

    prompt = f"""Design a new OADSON skill for this capability request:

"{capability_description}"

Output ONLY valid JSON (no markdown fences, no commentary) matching:
{{
  "skill_name": "snake_case_name_under_40_chars",
  "explanation": "2-4 plain-English sentences: what this skill does, how it does it, and any real limitations or risks it has",
  "manifest": {{
    "name": "same as skill_name",
    "category": "a short category like dev_tools, productivity, etc.",
    "description": "what Gemini should read to know when to call this skill",
    "keywords": ["a few trigger words"],
    "version": "1.0.0",
    "triggers": ["@same_as_skill_name"],
    "permissions": [],
    "dependencies": ["any pip packages beyond the stdlib this needs, or empty list"],
    "internet_access": true or false,
    "filesystem_access": false,
    "shell_access": false,
    "parameters": {{
      "type": "object",
      "properties": {{ "...": {{"type": "string", "description": "..."}} }},
      "required": ["..."]
    }}
  }},
  "code": "the FULL skill.py source, starting with an async def run(payload: dict) -> dict function. payload contains: message, user_id, session_id, surface, args, db, google_token, github_token. Return a dict with at least a 'status' key ('success' or 'error')."
}}

The code must be complete, syntactically valid Python, and self-contained
(only stdlib + whatever's declared in dependencies)."""

    url = GEMINI_URL.format(model=model)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 3000, "temperature": 0.3},
    }

    try:
        # SECURITY FIX 2026-08-14: params={"key": api_key} still renders
        # as ?key=... on the wire — moved to x-goog-api-key header,
        # which was leaking into Render's httpx request logs.
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(url, headers={"x-goog-api-key": api_key}, json=payload)
        if res.status_code != 200:
            return {"status": "error", "reason": f"Design call failed: {res.status_code}"}

        data = res.json()
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        design = json.loads(text.strip())

    except Exception as e:
        return {"status": "error", "reason": f"Design generation failed: {e}"}

    skill_name = (design.get("skill_name") or "").strip().lower()
    manifest = design.get("manifest")
    code = design.get("code", "")
    explanation = design.get("explanation", "")

    if not _valid_identifier(skill_name):
        return {"status": "error", "reason": f"Generated an invalid skill name: '{skill_name}'"}
    if not isinstance(manifest, dict) or not manifest.get("parameters"):
        return {"status": "error", "reason": "Generated manifest is malformed"}
    if not code or "async def run(" not in code:
        return {"status": "error", "reason": "Generated code is missing an async def run() function"}

    try:
        ast.parse(code)
    except SyntaxError as e:
        return {"status": "error", "reason": f"Generated code has a syntax error before you even see it: line {e.lineno}: {e.msg}"}

    proposal_id = str(uuid.uuid4())
    await db["skill_proposals"].insert_one({
        "proposal_id": proposal_id,
        "user_id": user_id,
        "skill_name": skill_name,
        "manifest": manifest,
        "code": code,
        "explanation": explanation,
        "status": "pending",
        "created_at": datetime.utcnow(),
    })

    return {
        "status": "success",
        "proposal_id": proposal_id,
        "skill_name": skill_name,
        "explanation": explanation,
        "manifest": manifest,
        "code_preview": code[:1500] + ("..." if len(code) > 1500 else ""),
        "message": (
            f"Proposed skill '{skill_name}': {explanation}\n\n"
            f"This has NOT been created yet. If this looks right, call skill_creator "
            f"again with action='create' and proposal_id='{proposal_id}' to actually build it."
        ),
    }


async def create_skill_from_proposal(db, skills_dir: str, skill_loader, user_id: str, proposal_id: str) -> dict:
    """
    The actual creation step — only reachable by a second, explicit call
    with a proposal_id already shown to and confirmed by you.
    """
    proposal = await db["skill_proposals"].find_one({"proposal_id": proposal_id, "user_id": user_id})
    if not proposal:
        return {"status": "error", "reason": f"No proposal found for id '{proposal_id}'"}
    if proposal.get("status") == "created":
        return {"status": "error", "reason": f"Proposal '{proposal_id}' was already created."}

    skill_name = proposal["skill_name"]
    manifest = proposal["manifest"]
    code = proposal["code"]

    try:
        ast.parse(code)  # re-validate — the proposal could be old/stale
    except SyntaxError as e:
        return {"status": "error", "reason": f"Stored code no longer valid: line {e.lineno}: {e.msg}"}

    folder = Path(skills_dir) / skill_name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "skill.py").write_text(code)
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Persist so this survives Render's ephemeral filesystem across
    # restarts/redeploys — see module docstring.
    await db["generated_skills"].update_one(
        {"skill_name": skill_name},
        {"$set": {
            "skill_name": skill_name, "manifest": manifest, "code": code,
            "user_id": user_id, "created_at": datetime.utcnow(),
        }},
        upsert=True,
    )

    success = skill_loader.reload(skill_name)
    if not success:
        return {
            "status": "error",
            "reason": f"Files written but registration failed — check server logs for '{skill_name}'.",
        }

    await db["skill_proposals"].update_one(
        {"proposal_id": proposal_id},
        {"$set": {"status": "created", "created_at": datetime.utcnow()}},
    )

    github_result = await _commit_skill_to_github(skill_name, manifest, code)

    message = f"Skill '{skill_name}' created and registered — callable immediately, no restart needed."
    if github_result:
        if github_result.get("status") == "success":
            message += f" Committed to GitHub ({github_result.get('repo')})."
        else:
            message += (
                f" NOT committed to GitHub — {github_result.get('reason')}. "
                f"It's live on the running server and in Mongo either way; "
                f"your GitHub repo just doesn't have it yet."
            )

    return {
        "status": "success",
        "skill_name": skill_name,
        "github": github_result,
        "message": message,
    }


async def rematerialize_generated_skills(db, skills_dir: str) -> int:
    """
    Called once at bootstrap, before the normal skill directory scan.
    Writes every previously-created skill back to disk from Mongo, since
    Render's filesystem doesn't survive a restart/redeploy but Mongo does.
    """
    count = 0
    try:
        cursor = db["generated_skills"].find({})
        async for doc in cursor:
            skill_name = doc.get("skill_name")
            manifest = doc.get("manifest")
            code = doc.get("code")
            if not skill_name or not manifest or not code:
                continue

            folder = Path(skills_dir) / skill_name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "skill.py").write_text(code)
            (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
            count += 1

        if count:
            logger.info(f"Re-materialized {count} previously-created skill(s) from Mongo.")
    except Exception as e:
        logger.warning(f"Skill re-materialization failed (non-fatal, normal skills still load): {e}")

    return count
