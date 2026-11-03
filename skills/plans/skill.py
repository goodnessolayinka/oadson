"""
Plans: an approved list of steps that runs overnight on the worker.
Actions: submit (start a plan) and status (check one).
A plan that changes anything outside the sandbox needs the user's approval first.
"""
from core import approvals
from core import remote_jobs as _rj
from core import worker_client
from core.worker_allowlist import APPROVAL_REQUIRED


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    action = str(args.get("action") or "").strip().lower()
    user_id = str(payload.get("user_id") or "")
    if not user_id:
        return {"status": "error", "reason": "user_id is required"}
    if action == "submit":
        return await _submit(user_id, args)
    if action == "status":
        rec = _rj.jobs.get(str(args.get("plan_id") or ""))
        if not rec:
            return {"status": "error", "reason": "no such plan"}
        return {"status": "success", "state": rec.get("state"), "progress": rec.get("progress"),
                "explanation": rec.get("explanation")}
    return {"status": "error", "reason": f"unknown plans action '{action}'",
            "explanation": "Use action 'submit' or 'status'."}


async def _submit(user_id: str, args: dict) -> dict:
    steps = args.get("steps")
    max_hours = args.get("max_hours", 12)
    if not isinstance(steps, list) or not steps:
        return {"status": "error", "reason": "submit needs 'steps': a list of {skill, action, args}"}
    plan_args = {"steps": steps, "max_hours": max_hours}
    risky = any(isinstance(s, dict) and [s.get("skill"), s.get("action")] in APPROVAL_REQUIRED
                for s in steps)
    approved = False
    if risky:
        code = args.get("approval_id")
        if code and approvals.consume(user_id, "plans", "submit", plan_args, code):
            approved = True
        else:
            req = approvals.request(user_id, "plans", "submit", plan_args)
            return {"status": "needs_approval", "approval_id": req,
                    "reason": "this plan changes something outside the sandbox and needs your approval",
                    "explanation": f"Reply 'approve {req}' to allow the whole plan. It expires in 30 minutes."}
    return await worker_client.submit_plan(user_id, steps, approved_by_user=approved,
                                           max_hours=float(max_hours))
