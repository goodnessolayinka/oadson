"""
core/capability_manifest.py — OADSON V3 Capability Manifest.

Implements the core of the "WhatsApp Personality & Capability Manifest"
EED: the model was occasionally falling back to pretrained assumptions
about what it can/can't do ("I cannot process audio") even on turns
where the exact same capability had worked correctly the day before.
The fix isn't a new skill — it's making sure every single turn tells the
model, explicitly and structurally, what's actually available right now,
instead of leaving it to infer or half-remember from earlier context.

This is scoped to the highest-value part of the EED (Phase 1): a real,
dynamically-derived manifest injected into every request, plus an
explicit "verify before refusing" instruction. It does NOT implement the
EED's proposed prompts/*.md file-per-surface architecture — the whole
system prompt is still built in _build_system_prompt() (core/orchestrator.py).
Splitting that into separate files is a real, larger restructuring with
its own risk (multiple files to keep in sync, load order, caching) that
wasn't attempted here; this module is the data source such a split could
read from later, without needing to be rebuilt.

Capabilities are derived from what's ACTUALLY registered/configured right
now, not hardcoded — a skill that's added or removed changes this
manifest automatically, which is also what the EED's Phase 3 ("live
capability discovery") is ultimately after; this gets partway there for
the common cases without needing the full dynamic query-the-skill-manager
pipeline.
"""

import os

# Skills whose mere presence in the registry implies a whole capability
# category, not just that one skill. Checked against skill_registry.names().
_CAPABILITY_SKILLS = {
    "documents": ["document_reader"],
    "memory": ["memory_manager"],
    "terminal": ["terminal"],
    "github": ["github"],
    "can_create_skills": ["skill_creator"],
    "can_edit_code": ["code_debugger", "code_validator"],
}


def build_capability_manifest(surface: str, skill_registry) -> dict:
    """
    Returns the structured capability manifest for this turn.

    surface: "web" | "whatsapp" | "telegram" | "terminal" | "cli"
    skill_registry: the live SkillRegistry instance (core/skill_registry.py)
                     — queried directly, not a cached/stale snapshot.
    """
    names = set(skill_registry.names())

    def _has_any(skill_names: list) -> bool:
        return any(n in names for n in skill_names)

    manifest = {
        "surface": surface,
        # Images/audio are always available on any surface that can pass
        # inline media through — the pipeline itself is mime-agnostic
        # (see core/orchestrator.py's image_data/extra_images handling).
        # Not gated on a specific skill because there isn't one; it's a
        # core capability of the run() pipeline itself.
        "images": True,
        "audio": True,
        "documents": _has_any(_CAPABILITY_SKILLS["documents"]),
        "memory": _has_any(_CAPABILITY_SKILLS["memory"]),
        "terminal": _has_any(_CAPABILITY_SKILLS["terminal"]),
        "github": _has_any(_CAPABILITY_SKILLS["github"]),
        "planner": os.getenv("ENABLE_PLANNER", "false").lower() == "true",
        "skills": len(names),
        "can_create_skills": _has_any(_CAPABILITY_SKILLS["can_create_skills"]),
        "can_edit_code": _has_any(_CAPABILITY_SKILLS["can_edit_code"]),
        "can_push_github": _has_any(_CAPABILITY_SKILLS["github"]),
    }
    return manifest


def format_manifest_for_prompt(manifest: dict) -> str:
    """
    Plain-language block for the system prompt — deliberately not raw
    JSON. The presentation_guard elsewhere in the system prompt already
    tells the model never to paste raw JSON into its OWN replies; the
    manifest itself should model the behavior it wants back, not
    contradict it by being JSON itself.
    """
    lines = [
        "\n\n=== YOUR CURRENT CAPABILITIES (verified, not assumed) ===",
        f"You are running on the {manifest['surface']} surface right now.",
        "The following are ACTUALLY available to you this turn — this list is",
        "generated fresh from what's really registered, not a guess:",
        f"- Images: {'yes' if manifest['images'] else 'no'} — sent as real inline image data, you can actually see them.",
        f"- Audio/voice notes: {'yes' if manifest['audio'] else 'no'} — sent as real inline audio data, you can actually hear/transcribe them.",
        f"- Reading documents (PDF/TXT/PY/JSON/MD): {'yes' if manifest['documents'] else 'no'} (@document_reader).",
        f"- Memory (save/recall facts about the user): {'yes' if manifest['memory'] else 'no'} (@memory_manager).",
        f"- Terminal / shell commands: {'yes' if manifest['terminal'] else 'no'} (@terminal).",
        f"- GitHub (read repos, commits, push): {'yes' if manifest['github'] else 'no'} (@github). "
        "To push code to GitHub, ALWAYS use `github action='push_workspace'` — never raw "
        "git commands in @terminal (no git identity/auth is configured there; it will fail "
        "every time on 'Author identity unknown' or similar, not just sometimes).",
        f"- Multi-step planning before acting: {'yes' if manifest['planner'] else 'no'}.",
        f"- Creating new skills: {'yes' if manifest['can_create_skills'] else 'no'} (@skill_creator).",
        f"- {manifest['skills']} total skills available — if you're unsure whether something",
        "  is possible, that's a reason to check the skill list, not a reason to assume it isn't.",
        "",
        "RULE: never tell the user you can't do something the list above says",
        "you can. If a capability shows 'yes' here, you have it right now — don't",
        "fall back to a pretrained assumption about what AI models 'usually' can't",
        "do (e.g. claiming you can't process audio when audio is marked yes above).",
        "If something shows 'no' here, say so plainly rather than guessing at a",
        "workaround that doesn't exist.",
    ]
    return "\n".join(lines)
