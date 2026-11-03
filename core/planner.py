"""
core/planner.py — Phase 10, Group 2: Planner separate from Executor.

Previously one Gemini function-calling loop did BOTH "what should happen
this turn" and "make it happen" in the same call, every single step —
pure reaction, one tool call at a time, no deliberate multi-step
reasoning about the whole turn before starting.

This adds a genuinely separate planning pass: one call, before the
tool-calling loop starts, producing a short structured plan (which
skills it expects to need, in what order, and why). The execution loop
still calls skills for real, one at a time, and still makes its own
per-step decisions — a plan can't know what Gmail actually returns
before Gmail runs, so this isn't a script the loop blindly follows. But
every decision call in that loop now sees the plan as context instead of
deciding blind at every single step.

OFF by default (ENABLE_PLANNER=false in orchestrator.py) — like the
thinking call, this is one extra Gemini call per turn, real quota cost
on a free-tier key, not something to turn on silently.
"""

import json
import logging
import httpx

logger = logging.getLogger("oadson.planner")


async def generate_plan(
    url_base: str, api_key: str, model: str, objective: str, skill_names: list,
) -> dict | None:
    """
    Returns {"final_goal": str, "steps": [{"skill": str|None, "goal": str}, ...]}
    or None on failure. Planning failure must never block the execution
    loop — it works fine with no plan at all, which is today's behavior,
    unchanged if this returns None.
    """
    skill_list = ", ".join(skill_names) if skill_names else "none"

    prompt = f"""You are the planning stage for OADSON, before any tool is actually called.

User's request: "{objective}"

Available skills: {skill_list}

STEP CAP: The execution loop has a hard cap of 30 tool-call steps. Plan to use the minimum steps possible.

BATCHING RULES — your plan MUST follow these:
- For any task involving a GitHub repo: plan exactly 3 github steps total:
    1. github clone_to_workspace  (downloads whole repo in one call)
    2. terminal  (one call with && chains for ALL reads, edits, tests)
    3. github push_workspace  (one commit, all files)
  Never plan a read_file or write_file per file — that's N*2 wasted steps.
- Chain terminal commands with && — plan ONE terminal step that does multiple things,
  not separate terminal steps for each command.
- For multi-file edits: plan one terminal step using heredocs or a shell script.
- Only plan a skill call when its output is actually needed to proceed.

Output ONLY valid JSON (no markdown fences, no commentary) matching this shape:
{{
  "final_goal": "<one sentence: what 'done' looks like for this request>",
  "steps": [
    {{"skill": "<skill name from the list, or null if no tool needed>", "goal": "<what this step accomplishes — be specific about what gets batched here>"}}
  ]
}}

Keep it to the minimum steps actually needed — usually 1-3, max 5.
If no tool is needed at all, return exactly one step with "skill": null."""

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 300, "temperature": 0.2},
    }
    url = f"{url_base}{model}:generateContent"

    # SECURITY FIX 2026-08-14: key moved from URL to x-goog-api-key
    # header — was leaking into Render's httpx request logs.
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
        if res.status_code != 200:
            logger.warning(f"Planner call failed: {res.status_code} {res.text[:200]}")
            return None

        data = res.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return None

        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        plan = json.loads(text)
        if not isinstance(plan, dict) or "steps" not in plan or not isinstance(plan["steps"], list):
            logger.warning(f"Planner returned malformed shape, ignoring: {text[:200]}")
            return None

        return plan

    except Exception as e:
        logger.warning(f"Planner call failed (non-fatal): {e}")
        return None


def format_plan_for_prompt(plan: dict) -> str:
    """Render the plan as a short block for the system prompt. Empty string if no plan."""
    if not plan:
        return ""

    steps = plan.get("steps", [])
    if not steps:
        return ""

    step_lines = []
    for i, s in enumerate(steps, 1):
        skill = s.get("skill") or "no tool — reason/answer directly"
        step_lines.append(f"  {i}. [{skill}] {s.get('goal', '')}")

    plan_text = "\n".join(step_lines)
    return (
        f"\n\nPlan for this turn (goal: {plan.get('final_goal', 'n/a')}):\n{plan_text}\n"
        "This plan is a guide based on your best judgment before seeing any "
        "real tool results — adapt if what a tool actually returns changes "
        "what's needed, but don't ignore the plan without a real reason to."
    )
