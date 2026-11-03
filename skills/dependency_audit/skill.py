"""
skills/dependency_audit/skill.py — OADSON V3 Dependency Vulnerability Audit.

Wraps pip-audit (pip) and npm audit (npm) — the same sandbox pattern as
@terminal (core.oadson_home.get_user_home), so this runs in the user's
workspace/ directory and never touches anything outside it.

pip:  writes requirements.txt, runs `pip-audit -r requirements.txt -f json`.
npm:  writes package.json, runs `npm install --package-lock-only` to
      resolve a lockfile WITHOUT downloading/installing the actual
      packages (fast, no need to execute untrusted install scripts),
      then `npm audit --package-lock-only --json` against that lockfile.

Both tools hit their own public vulnerability databases over the
network (PyPI Advisory DB / npm's own registry) — that's the whole
point, this can't work fully offline.
"""

import json
import subprocess
from pathlib import Path

from core.oadson_home import get_user_home

_TIMEOUT = 90


def _run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=_TIMEOUT,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return -1, e.stdout or "", (e.stderr or "") + f"\n[timed out after {_TIMEOUT}s]"
    except FileNotFoundError as e:
        return -1, "", f"Required tool not found: {e}. It may not be installed in this environment."


def _summarize_pip_audit(raw_json: str) -> dict:
    try:
        data = json.loads(raw_json)
    except Exception:
        return {"parsed": False, "raw": raw_json[:4000]}

    deps = data.get("dependencies", data if isinstance(data, list) else [])
    findings = []
    for dep in deps:
        vulns = dep.get("vulns", [])
        if vulns:
            findings.append({
                "package": dep.get("name"),
                "version": dep.get("version"),
                "vulnerabilities": [
                    {"id": v.get("id"), "fix_versions": v.get("fix_versions"), "description": (v.get("description") or "")[:300]}
                    for v in vulns
                ],
            })
    return {"parsed": True, "vulnerable_package_count": len(findings), "findings": findings}


def _summarize_npm_audit(raw_json: str) -> dict:
    try:
        data = json.loads(raw_json)
    except Exception:
        return {"parsed": False, "raw": raw_json[:4000]}

    vulns = data.get("vulnerabilities", {})
    findings = []
    for pkg_name, info in vulns.items():
        findings.append({
            "package": pkg_name,
            "severity": info.get("severity"),
            "fix_available": bool(info.get("fixAvailable")),
            "via": [v if isinstance(v, str) else v.get("title") for v in info.get("via", [])][:5],
        })
    meta = data.get("metadata", {}).get("vulnerabilities", {})
    return {"parsed": True, "vulnerable_package_count": len(findings), "severity_totals": meta, "findings": findings}


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    manager = args.get("manager")
    content = args.get("content")
    project_path = args.get("project_path")
    user_id = payload.get("user_id")

    if not user_id:
        return {"status": "error", "reason": "No user_id in payload — cannot determine sandbox."}
    if manager not in ("pip", "npm"):
        return {"status": "error", "reason": "manager must be 'pip' or 'npm'."}
    if not content and not project_path:
        return {"status": "error", "reason": "Provide either 'content' (file text) or 'project_path' (existing file in sandbox)."}

    home = get_user_home(user_id)
    workdir = home / "dependency_audit"
    workdir.mkdir(parents=True, exist_ok=True)

    if manager == "pip":
        target = workdir / "requirements.txt"
        if content:
            target.write_text(content)
        elif project_path:
            src = (home / project_path).resolve()
            if not str(src).startswith(str(home.resolve())):
                return {"status": "error", "reason": "project_path escapes the sandbox."}
            if not src.exists():
                return {"status": "error", "reason": f"'{project_path}' not found in sandbox."}
            target.write_text(src.read_text())

        rc, out, err = _run(["pip-audit", "-r", "requirements.txt", "-f", "json"], cwd=workdir)
        if rc == -1:
            return {"status": "error", "reason": err or "pip-audit failed to run.", "stdout": out}
        summary = _summarize_pip_audit(out)
        return {
            "status": "success" if summary.get("parsed") else "error",
            "manager": "pip",
            "return_code": rc,
            **summary,
            "reason": None if summary.get("parsed") else "Could not parse pip-audit output.",
            "stderr": err[:1000] if err else None,
        }

    else:  # npm
        target = workdir / "package.json"
        if content:
            target.write_text(content)
        elif project_path:
            src = (home / project_path).resolve()
            if not str(src).startswith(str(home.resolve())):
                return {"status": "error", "reason": "project_path escapes the sandbox."}
            if not src.exists():
                return {"status": "error", "reason": f"'{project_path}' not found in sandbox."}
            target.write_text(src.read_text())

        rc, out, err = _run(["npm", "install", "--package-lock-only", "--no-audit", "--no-fund"], cwd=workdir)
        if rc not in (0, None):
            # still try audit — npm sometimes exits non-zero on peer-dep
            # warnings even when the lockfile was written fine.
            pass

        rc2, out2, err2 = _run(["npm", "audit", "--package-lock-only", "--json"], cwd=workdir)
        if rc2 == -1 and not out2:
            return {"status": "error", "reason": err2 or "npm audit failed to run.", "install_stderr": err[:1000] if err else None}

        summary = _summarize_npm_audit(out2)
        return {
            "status": "success" if summary.get("parsed") else "error",
            "manager": "npm",
            "return_code": rc2,
            **summary,
            "reason": None if summary.get("parsed") else "Could not parse npm audit output.",
            "install_stderr": err[:1000] if err else None,
        }
