"""
core/failure_explainer.py — say what a tool call did and what to do next, in plain words.

Two things every tool result gets, so neither the model nor the user has to
decode a raw status field:
  call_summary  one line describing the call ("Run command: ls repos/")
  explanation   for failures only: what went wrong and the next step

Patterns are matched on the reason text the skills already produce, so this
works without changing each skill.
"""

_RULES = [
    (("outside the sandbox",),
     "The call tried to reach a path outside your workspace. Use a relative path such as "
     "repos/owner__repo/ instead of a path starting with ../ or /."),
    (("degenerate path", "empty/degenerate"),
     "A path argument was empty. Build the path from a real folder or file name."),
    (("timed out", "timeout", "time limit"),
     "The command ran past its time limit. Split it into smaller steps, or run the slow part "
     "separately."),
    (("429", "rate limit", "quota", "resource_exhausted"),
     "A provider rate limit was hit. Retrying straight away will fail again. Wait, or use a "
     "different provider."),
    (("401", "403", "unauthorized", "forbidden", "not set", "invalid token", "bad credentials",
      "token"),
     "A credential is missing or was rejected. Retrying will not fix it. The owner needs to "
     "check the setting."),
    (("404", "not found", "no such file", "does not exist"),
     "The target was not found. Check the name or path, then try again."),
    (("connection", "network", "temporarily", "502", "503", "unreachable"),
     "A network call failed, which is usually temporary. One retry is reasonable."),
]


def describe_call(skill: str, args: dict) -> str:
    """One plain-language line for a tool call, shown next to its result."""
    args = args or {}
    action = str(args.get("action") or "").strip()
    if skill == "terminal":
        cmd = str(args.get("command") or args.get("cmd") or "").strip()
        return f"Run command: {cmd[:80]}" if cmd else "Run a command in the workspace"
    if skill == "github":
        repo = f"{args.get('owner', '?')}/{args.get('repo', '?')}"
        if action == "clone_to_workspace":
            return f"Clone {repo} into the workspace"
        if action == "push_workspace":
            return f"Commit and push workspace changes to {repo}"
        return f"GitHub {action or 'request'} for {repo}"
    label = action.replace("_", " ") if action else "request"
    return f"{skill}: {label}"


def explain_failure(result: dict) -> str:
    """Plain sentence for a failed result, with the next step."""
    if not isinstance(result, dict):
        return "The call failed without a readable result. Try once more with a simpler input."
    status = str(result.get("status") or "").lower()
    text = " ".join(str(result.get(k) or "") for k in ("reason", "message", "error", "stderr")).lower()
    if status == "timeout":
        return _RULES[2][1]
    for keys, explanation in _RULES:
        if any(k in text for k in keys):
            return explanation
    if status == "blocked":
        return "The call was blocked by a safety rule. Read the reason, then change the call."
    return ("The call failed for a reason this list doesn't cover. Read the reason text, change "
            "the input or the approach, then try again.")
