"""
core/model_policy.py — decides when Claude takes a turn, and trims what it reads.

Gemini stays the default for everyday chat (free). Claude Haiku 5.5 is used
only for HEAVY turns: repo / codebase / debugging / refactor work, large
requests, or code attachments — the turns where a cheap model is most likely
to break. It is also the LAST paid fallback in provider_registry, so it is
never used just because a free provider hiccupped.

trim_tool_results() keeps only the most recent tool outputs at full size.
Older ones are cut down, because a multi-step repo turn otherwise re-sends
every file it ever read on every step. Anthropic has no prompt caching over
its OpenAI-compatible endpoint, so input size is the cost lever there.
"""
import re

from core import spend_tracker

CLAUDE_MODEL = "claude-haiku-5-5"
CLAUDE_SURFACES = {"web", "app", "pwa", "telegram", "whatsapp"}  # never terminal
PER_TURN_RESERVE_USD = 0.05   # refuse a Claude call if less than this remains in cap
LONG_TURN_CHARS = 1500

_HEAVY_KEYWORDS = re.compile(
    r"\b(repo|repository|codebase|code base|monorepo|refactor\w*|debug\w*|traceback|"
    r"stack ?trace|exception|bug|failing test|unit test|migrat\w*|architecture|"
    r"pull request|across (?:the )?(?:files|project)|entire (?:project|app|codebase)|"
    r"whole (?:project|app|codebase)|build error|compile error|type ?error)\b",
    re.I,
)
_CODE_SIGNAL = re.compile(
    r"```|\b[\w/.-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|kt|swift|rb|php|cs|cpp|c|h|sql|"
    r"ya?ml|toml|json)\b",
    re.I,
)
_CODE_EXTS = (".zip", ".tar", ".tar.gz", ".py", ".js", ".ts", ".tsx", ".jsx", ".go",
              ".rs", ".java", ".kt", ".swift", ".rb", ".php", ".cs", ".cpp", ".c",
              ".h", ".sql", ".yaml", ".yml", ".toml", ".json")


def heavy_reason(ctx) -> str | None:
    """Return why this turn is heavy, or None if Gemini should handle it."""
    text = getattr(ctx, "objective", "") or ""
    if len(text) > LONG_TURN_CHARS:
        return "long request"
    m = _HEAVY_KEYWORDS.search(text)
    if m:
        return f"keyword: {m.group(0).lower()}"
    if _CODE_SIGNAL.search(text):
        return "code in message"
    fn = (getattr(ctx, "attachment_filename", None) or "").lower()
    if fn.endswith(_CODE_EXTS):
        return "code attachment"
    if len(getattr(ctx, "extra_attachment_urls", None) or []) >= 2:
        return "multiple attachments"
    return None


def pause_notice(model_ref: str | None) -> str | None:
    """
    If the user picked a paid model that can't be used right now, return an
    instruction telling the model to say so, in plain words, before answering.
    Returns None when the model is free, unknown, or available.
    """
    if not model_ref:
        return None
    # Match the whole ID, or "provider/ID". Model IDs can contain "/" themselves
    # (e.g. coreweave's "zai-org/GLM-5.3-Flash"), so splitting on "/" is wrong.
    ref = str(model_ref)
    from core.provider_registry import PROVIDERS
    owner = None
    model_id = ref
    for p in PROVIDERS:
        for m in p.get("models", []):
            if ref == m["id"] or ref == f"{p['name']}/{m['id']}":
                owner, model_id = p, m["id"]
                break
        if owner:
            break
    if not owner or not owner.get("paid"):
        return None
    name = owner["name"]
    if spend_tracker.can_spend(name, est_usd=PER_TURN_RESERVE_USD):
        return None
    _, today = spend_tracker.spent(name)
    cap_day = spend_tracker.CAPS.get(name, {}).get("daily", 0)
    if today + PER_TURN_RESERVE_USD > cap_day:
        reason = (f"today's {name} spend limit is used up, and it resets at midnight UTC")
    else:
        reason = f"the {name} lifetime spend limit is used up"
    return (f"The user picked {model_id}, but it is paused because {reason}. "
            "You are answering with a free model instead. Start your reply with one plain "
            "sentence saying this, then answer the question normally.")


def auto_model_for(ctx) -> str | None:
    """Model to try first for this turn, or None to leave routing unchanged."""
    if getattr(ctx, "surface", None) not in CLAUDE_SURFACES:
        return None
    if not heavy_reason(ctx):
        return None
    if not spend_tracker.can_spend("anthropic", est_usd=PER_TURN_RESERVE_USD):
        return None
    return CLAUDE_MODEL


def trim_tool_results(messages: list, keep_last: int = 3, max_chars: int = 1500) -> list:
    """Return a copy where all but the last `keep_last` tool outputs are capped."""
    tool_idx = [i for i, m in enumerate(messages)
                if isinstance(m, dict) and m.get("role") == "tool"]
    old = set(tool_idx[:-keep_last]) if keep_last and len(tool_idx) > keep_last else set()
    out = []
    for i, m in enumerate(messages):
        c = m.get("content") if isinstance(m, dict) else None
        if i in old and isinstance(c, str) and len(c) > max_chars:
            cut = len(c) - max_chars
            out.append({**m, "content": c[:max_chars] +
                        f"\n…[{cut} chars trimmed to save credits; re-run the tool if needed]"})
        else:
            out.append(m)
    return out
