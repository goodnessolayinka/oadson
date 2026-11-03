"""
The job contract. Owner sends JobRequest; worker answers with JobResult.

Identifiers are checked against a strict pattern because they become file
names on the worker's volume and, for skill, a Python import path.
"""
import re
from dataclasses import dataclass, field

STATES = {"queued", "running", "done", "failed", "blocked", "cancelled", "interrupted", "paused"}
TERMINAL_STATES = {"done", "failed", "blocked", "cancelled", "interrupted", "paused"}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_TIMEOUT_S = 3600


def _check_id(name: str, value) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"'{name}' must be 1-128 letters, digits, '-' or '_'")
    return value


@dataclass
class JobRequest:
    job_id: str
    idempotency_key: str
    user_id: str
    skill: str
    action: str
    args: dict = field(default_factory=dict)
    timeout_s: int = 300

    @classmethod
    def from_dict(cls, d) -> "JobRequest":
        if not isinstance(d, dict):
            raise ValueError("the job must be a JSON object")
        job_id = _check_id("job_id", d.get("job_id"))
        key = _check_id("idempotency_key", d.get("idempotency_key"))
        user_id = _check_id("user_id", d.get("user_id"))
        skill = _check_id("skill", d.get("skill"))
        action = d.get("action")
        if not isinstance(action, str) or not action.strip() or len(action) > 64:
            raise ValueError("'action' must be text, up to 64 characters")
        args = d.get("args", {})
        if not isinstance(args, dict):
            raise ValueError("'args' must be an object")
        timeout = d.get("timeout_s", 300)
        if isinstance(timeout, bool) or not isinstance(timeout, int) \
                or not 1 <= timeout <= MAX_TIMEOUT_S:
            raise ValueError(f"'timeout_s' must be a whole number from 1 to {MAX_TIMEOUT_S}")
        return cls(job_id=job_id, idempotency_key=key, user_id=user_id, skill=skill,
                   action=action.strip(), args=args, timeout_s=timeout)


@dataclass
class JobResult:
    job_id: str
    state: str
    result: dict | None = None
    error: str | None = None
    explanation: str | None = None
    artifacts: list = field(default_factory=list)

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError(f"unknown state: {self.state}")

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "state": self.state,
            "result": self.result,
            "error": self.error,
            "explanation": self.explanation,
            "artifacts": self.artifacts,
        }
