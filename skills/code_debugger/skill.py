"""
code_debugger — the user-facing twin of project_creator's internal
validation. Same engine (core/code_validator.py), different purpose:
this debugs code YOU already have (pasted, or read via github's
read_file), not code OADSON just generated. Kept as a separate skill
from project_creator (rather than one skill doing both) so the
description/triggering stays clear for Gemini: "debug this file" and
"build me a project" are different intents even though they share the
same underlying repair engine.

Also distinct from `bug_analyzer` — that skill explains an error message
you already have. This one actually runs the code and iterates on fixing
it, per your request: "make bug analyser actually read the code and
iterate and improve it... duplicated for code generated and bug analyser
for user files." This is that second half.
"""

import os
import logging

from core.code_validator import iterative_repair, check_python_syntax

logger = logging.getLogger("oadson.skill.code_debugger")


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    files = args.get("files")
    entrypoint = (args.get("entrypoint") or "").strip() or None

    if not files or not isinstance(files, list):
        return {"status": "error", "reason": "files (a non-empty list) is required"}

    clean_files = []
    for f in files:
        if not isinstance(f, dict) or not f.get("path"):
            continue
        clean_files.append({"path": f["path"], "content": f.get("content", "")})

    if not clean_files:
        return {"status": "error", "reason": "No valid files provided (each needs a 'path')"}

    try:
        api_key = os.getenv("GEMINI_API_KEY", "")
        result = await iterative_repair(
            clean_files, api_key=api_key, run_entrypoint=entrypoint,
        )

        if result["clean"]:
            message = (
                "No issues found — code is syntactically valid" +
                (" and runs cleanly." if entrypoint else ".")
                if result["iterations"] == 0
                else f"Found and fixed issues after {result['iterations']} round(s) of repair."
            )
        else:
            message = f"Still has unresolved issues after {result['iterations']} repair round(s): {result.get('remaining_errors')}"

        return {
            "status": "success",
            "clean": result["clean"],
            "repair_iterations": result["iterations"],
            "remaining_errors": result.get("remaining_errors", []),
            "files": result["files"],
            "trace": result.get("trace", []),
            "message": message,
        }

    except Exception as e:
        logger.error(f"code_debugger error: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}
