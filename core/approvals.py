"""
Approvals for actions that change something outside the sandbox (push, write, create repo).

The only way an approval is given is the user's own chat message "approve <code>".
The model cannot approve for the user, and an approval only covers the exact
arguments it was requested with. It is single-use and expires after 30 minutes.
"""
import hashlib
import json
import re
import secrets
import time

from core.worker_allowlist import APPROVAL_REQUIRED

TTL_S = 1800
_pending: dict = {}
_notices: dict = {}
_APPROVE = re.compile(r"^\s*approve\s+([a-f0-9]{8})\s*$", re.IGNORECASE)


def needs_approval(skill: str, action) -> bool:
    return [skill, action] in APPROVAL_REQUIRED


def _fingerprint(user_id, skill, action, args) -> str:
    clean = {k: v for k, v in (args or {}).items() if k not in ("approval_id", "action")}
    return hashlib.sha256(json.dumps([str(user_id), skill, action, clean],
                                     sort_keys=True, default=str).encode()).hexdigest()


def _sweep() -> None:
    now = time.time()
    for code in [c for c, r in _pending.items() if r["expires"] < now]:
        _pending.pop(code, None)


def request(user_id, skill: str, action, args) -> str:
    _sweep()
    code = secrets.token_hex(4)
    _pending[code] = {"user_id": str(user_id), "skill": skill, "action": action,
                      "fp": _fingerprint(user_id, skill, action, args),
                      "approved": False, "used": False, "expires": time.time() + TTL_S}
    return code


def parse_approve(text) -> str | None:
    m = _APPROVE.match(text or "")
    return m.group(1).lower() if m else None


def approve(user_id, code: str) -> bool:
    _sweep()
    rec = _pending.get(code)
    if not rec or rec["user_id"] != str(user_id) or rec["used"]:
        return False
    rec["approved"] = True
    _notices[str(user_id)] = (
        f"The user approved the action '{rec['action']}' (approval {code}). If they asked for it, "
        f"run it again now with approval_id='{code}' in its args, using exactly the same arguments "
        "as before. Do not change the arguments.")
    return True


def consume(user_id, skill: str, action, args, code: str) -> bool:
    _sweep()
    rec = _pending.get(code)
    if not rec or not rec["approved"] or rec["used"] or rec["user_id"] != str(user_id):
        return False
    if rec["skill"] != skill or rec["action"] != action:
        return False
    if rec["fp"] != _fingerprint(user_id, skill, action, args):
        return False
    rec["used"] = True
    return True


def pop_notice(user_id) -> str | None:
    return _notices.pop(str(user_id), None)
