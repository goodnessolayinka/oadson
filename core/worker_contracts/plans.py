"""
The plan contract: an approved sequence of steps, run one after another on the worker.
"""
from contracts.jobs import _check_id

MAX_STEPS = 50
MAX_HOURS = 24


def validate_plan(d) -> dict:
    if not isinstance(d, dict):
        raise ValueError("the plan must be a JSON object")
    plan_id = _check_id("plan_id", d.get("plan_id"))
    key = _check_id("idempotency_key", d.get("idempotency_key"))
    user_id = _check_id("user_id", d.get("user_id"))
    steps = d.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"'steps' must be a list of 1 to {MAX_STEPS} steps")
    clean = []
    for i, st in enumerate(steps):
        if not isinstance(st, dict):
            raise ValueError(f"step {i + 1} must be an object")
        skill = _check_id(f"steps[{i}].skill", st.get("skill"))
        action = st.get("action")
        if not isinstance(action, str) or not action.strip() or len(action) > 64:
            raise ValueError(f"step {i + 1}: 'action' must be text, up to 64 characters")
        args = st.get("args", {})
        if not isinstance(args, dict):
            raise ValueError(f"step {i + 1}: 'args' must be an object")
        timeout = st.get("timeout_s", 300)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            raise ValueError(f"step {i + 1}: 'timeout_s' must be a whole number from 1 to 3600")
        clean.append({
            "skill": skill, "action": action.strip(), "args": args, "timeout_s": timeout,
            # idempotent: safe to run again if the worker restarts mid-step (reads, lookups).
            "idempotent": bool(st.get("idempotent", False)),
            # continue_on_failure: keep going if this step fails. Default: stop the plan.
            "continue_on_failure": bool(st.get("continue_on_failure", False)),
        })
    hours = d.get("max_hours", 12)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0.1 <= hours <= MAX_HOURS:
        raise ValueError(f"'max_hours' must be a number from 0.1 to {MAX_HOURS}")
    return {"plan_id": plan_id, "idempotency_key": key, "user_id": user_id, "steps": clean,
            "max_hours": float(hours), "approved_by_user": bool(d.get("approved_by_user", False))}
