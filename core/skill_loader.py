"""
core/skill_loader.py — OADSON V3 Skill Loader.

Scans the skills directory. Loads each skill module.
Registers it in the SkillRegistry.

Each skill folder must contain:
  - skill.py     (with async def run(payload: dict) -> dict)
  - manifest.json (metadata + permissions)

Skills that fail to load are logged and skipped. One bad skill
never blocks the rest.
"""

import os
import json
import importlib.util
import logging
from pathlib import Path
from core.skill_registry import SkillRegistry

logger = logging.getLogger("oadson.skill_loader")


class SkillLoader:

    def __init__(self, skills_dir: str, registry: SkillRegistry) -> None:
        self.skills_dir = Path(skills_dir)
        self.registry = registry

    def load_all(self) -> None:
        if not self.skills_dir.exists():
            logger.warning(f"Skills directory not found: {self.skills_dir}. No skills loaded.")
            return

        loaded = 0
        failed = 0

        for skill_folder in sorted(self.skills_dir.iterdir()):
            if not skill_folder.is_dir():
                continue
            if skill_folder.name.startswith("_"):
                continue   # skip __pycache__ etc.

            success = self._load_skill(skill_folder)
            if success:
                loaded += 1
            else:
                failed += 1

        logger.info(f"Skill loading complete. Loaded: {loaded}, Failed: {failed}")

    def _load_skill(self, folder: Path) -> bool:
        name = folder.name
        skill_py = folder / "skill.py"
        manifest_json = folder / "manifest.json"

        # Validate required files exist
        if not skill_py.exists():
            logger.warning(f"Skill '{name}': missing skill.py — skipping.")
            return False

        if not manifest_json.exists():
            logger.warning(f"Skill '{name}': missing manifest.json — skipping.")
            return False

        # Load manifest
        try:
            with open(manifest_json) as f:
                manifest = json.load(f)
        except Exception as e:
            logger.error(f"Skill '{name}': bad manifest.json — {e}")
            return False

        # Load Python module
        try:
            spec = importlib.util.spec_from_file_location(
                f"skills.{name}", str(skill_py)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            logger.error(f"Skill '{name}': failed to import skill.py — {e}")
            return False

        # Validate run() exists
        if not hasattr(module, "run"):
            logger.warning(f"Skill '{name}': skill.py has no async run() — skipping.")
            return False

        # Register
        entry = {
            "name": name,
            "description": manifest.get("description", ""),
            "category": manifest.get("category", "general"),
            "permissions": manifest.get("permissions", []),
            "version": manifest.get("version", "1.0.0"),
            "module": module,
            "manifest": manifest,
        }
        self.registry.register(name, entry)
        return True

    def reload(self, skill_name: str) -> bool:
        """Hot-reload a single skill. Useful without full restart."""
        folder = self.skills_dir / skill_name
        if not folder.exists():
            logger.error(f"Cannot reload '{skill_name}': folder not found.")
            return False
        return self._load_skill(folder)
