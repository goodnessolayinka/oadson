"""
core/skill_registry.py — OADSON V3 Skill Registry.

One registry. One instance. Lives in bootstrap._registry["skill_registry"].
No other code instantiates skills independently.

Each skill entry:
{
    "name": str,
    "description": str,
    "category": str,
    "permissions": list[str],
    "version": str,
    "module": <loaded module with async run(payload) function>,
    "manifest": dict,
}
"""

import logging
from typing import Any

logger = logging.getLogger("oadson.skill_registry")


class SkillRegistry:

    def __init__(self) -> None:
        self._skills: dict[str, dict] = {}

    def register(self, name: str, entry: dict) -> None:
        """
        Register a skill. Entry must contain at least:
          - name, description, category, module, manifest
        """
        required = {"name", "description", "category", "module", "manifest"}
        missing = required - set(entry.keys())
        if missing:
            logger.warning(f"Skill '{name}' missing fields {missing} — skipping.")
            return

        self._skills[name] = entry
        logger.debug(f"Registered skill: {name} [{entry.get('category', '?')}]")

    def get(self, name: str) -> dict | None:
        return self._skills.get(name)

    def all(self) -> list[dict]:
        """Return list of all skill metadata (no module reference)."""
        return [
            {k: v for k, v in s.items() if k != "module"}
            for s in self._skills.values()
        ]

    def names(self) -> list[str]:
        return list(self._skills.keys())

    def count(self) -> int:
        return len(self._skills)

    def has(self, name: str) -> bool:
        return name in self._skills

    async def execute(self, name: str, payload: dict) -> dict:
        """
        Execute a skill by name.
        Returns normalised output:
        {
            "skill": str,
            "status": "success" | "error",
            "data": dict | None,
            "error": str | None,
        }
        """
        entry = self._skills.get(name)
        if not entry:
            return {
                "skill": name,
                "status": "error",
                "data": None,
                "error": f"Skill '{name}' not found in registry.",
            }

        module = entry["module"]
        if not hasattr(module, "run"):
            return {
                "skill": name,
                "status": "error",
                "data": None,
                "error": f"Skill '{name}' has no run() function.",
            }

        try:
            result = await module.run(payload)
            return self._normalise(name, result)
        except Exception as e:
            logger.error(f"Skill '{name}' raised: {e}")
            return {
                "skill": name,
                "status": "error",
                "data": None,
                "error": str(e),
            }

    def _normalise(self, name: str, raw: Any) -> dict:
        if isinstance(raw, dict):
            status = raw.get("status", "success")

            if status in ("success", "ok", "done", "info"):
                normalised_status = "info" if status == "info" else "success"
                data = {k: v for k, v in raw.items() if k not in ("status",)}
                return {"skill": name, "status": normalised_status, "data": data, "error": None}

            # Any other status ("error", "timeout", "blocked") — pass the
            # full dict through verbatim. Previously re-wrapped into:
            #   {"error": raw.get("error") or raw.get("reason") or f"status={status}"}
            # which threw away stdout/stderr/return_code and fell through to
            # "status=error" when neither key was set — exactly what terminal
            # returns on a failed subprocess (uses "stderr"/"return_code").
            passthrough = dict(raw)
            passthrough["skill"] = name
            if not passthrough.get("reason") and not passthrough.get("error"):
                passthrough["reason"] = (
                    raw.get("stderr")
                    or (raw.get("stdout", "")[:200] if raw.get("stdout") else None)
                    or f"skill returned status={status}"
                )
            return passthrough

        # Non-dict — wrap it
        return {"skill": name, "status": "success", "data": {"result": raw}, "error": None}
