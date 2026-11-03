"""
core/hallucination_firewall.py — OADSON V3 Hallucination Firewall.

Architecture from EDD (July 2026):

  User → Intent → Tools → Evidence Collection → Answer → Reviewer → Confidence → Final

The firewall has two components:

1. EvidenceStore (per-turn, in-memory)
   Built during the agent loop. Every tool call appends a receipt.
   The final answer generation receives the full evidence ledger.

2. Reviewer (async Gemini call, lightweight)
   After the model generates a final answer, the reviewer:
   - Breaks the answer into factual claims
   - Checks each claim against the evidence ledger
   - Returns a verdict: PASS / REWRITE with specific unsupported claims flagged
   - If REWRITE: the orchestrator either strips unsupported claims or
     appends an honest uncertainty note

Cost: one extra lightweight Gemini call per final answer (~100 tokens).
Benefit: catches the most common hallucination pattern (tool failed silently,
model fills from context/training, user gets confident wrong answer).

The reviewer does NOT require Claude Opus. gemini-3.1-flash-lite handles
claim verification at this granularity perfectly — it's just pattern matching
against a structured evidence list, not deep reasoning.
"""

import json
import logging
import os
import httpx

logger = logging.getLogger("oadson.hallucination_firewall")


# ── Evidence Store ─────────────────────────────────────────────────────────────

def _output_preview(result: dict, limit: int = 1200) -> str:
    """
    Capped copy of what the tool actually returned. The reviewer used to see
    only short key facts, so correct claims about file contents (\"13 files\")
    looked unsupported and got rewritten. Giving it the real output fixes that.
    """
    import json as _json
    for k in ("stdout", "output", "text", "content", "data", "result", "files", "tree"):
        v = result.get(k)
        if v:
            s = v if isinstance(v, str) else _json.dumps(v, default=str)
            return s[:limit]
    try:
        return _json.dumps(result, default=str)[:limit]
    except Exception:
        return ""


class EvidenceStore:
    """
    Tracks tool receipts and verified facts for a single agent turn.
    Passed through the agent loop and injected into the final answer prompt.
    """

    def __init__(self):
        self.receipts: list[dict] = []   # [{tool, action, status, key_facts: [str]}]
        self.failed_tools: list[str] = []
        self.verified_facts: list[str] = []

    def record_tool(self, tool: str, args: dict, result: dict) -> None:
        """Called after every skill execution."""
        status = result.get("status", "unknown")
        action = (args or {}).get("action", "")

        receipt = {
            "tool": tool,
            "action": action,
            "status": status,
        }

        if status in ("error", "timeout", "blocked"):
            self.failed_tools.append(f"{tool}/{action or 'call'}")
            reason = (
                result.get("reason")
                or result.get("error")
                or result.get("stderr")
                or "unknown error"
            )
            receipt["failure_reason"] = reason
        else:
            # Extract key facts from successful results
            facts = _extract_facts(tool, action, result)
            receipt["key_facts"] = facts
            self.verified_facts.extend(facts)
            receipt["output_preview"] = _output_preview(result)

        self.receipts.append(receipt)

    def tool_receipt_block(self) -> str:
        """
        Formatted evidence ledger injected into the final answer prompt.
        The model MUST only make claims that appear here.
        """
        if not self.receipts:
            return "\n\nTOOL RECEIPTS: No tools were called this turn."

        lines = ["\n\nTOOL RECEIPTS (evidence ledger for this turn):"]
        for r in self.receipts:
            icon = "✓" if r["status"] not in ("error", "timeout", "blocked") else "✗"
            facts = r.get("key_facts", [])
            failure = r.get("failure_reason", "")
            tool_str = f"{r['tool']}"
            if r.get("action"):
                tool_str += f"/{r['action']}"
            if facts:
                lines.append(f"  {icon} {tool_str}: {'; '.join(facts[:3])}")
                if r.get("output_preview"):
                    lines.append("      output: " + r["output_preview"].replace("\n", " | "))
            elif failure:
                lines.append(f"  {icon} {tool_str}: FAILED — {failure[:200]}")
            else:
                lines.append(f"  {icon} {tool_str}: completed")

        if self.failed_tools:
            lines.append(
                f"\nFAILED TOOLS THIS TURN: {', '.join(self.failed_tools)}"
                f"\nYou have NO data from these tools. Do not answer questions "
                f"that require their output."
            )

        lines.append(
            "\nEVIDENCE RULE: Every factual claim in your answer must come from "
            "a ✓ receipt above, from the user's message, or from well-established "
            "general knowledge (clearly marked as such). "
            "If a claim has no source in this ledger, say 'I couldn't verify that' "
            "instead of guessing."
        )
        return "\n".join(lines)

    def has_failures(self) -> bool:
        return len(self.failed_tools) > 0

    def to_dict(self) -> dict:
        return {
            "receipts": self.receipts,
            "failed_tools": self.failed_tools,
            "verified_facts": self.verified_facts,
        }


def _extract_facts(tool: str, action: str, result: dict) -> list[str]:
    """Extract human-readable key facts from a successful tool result."""
    facts = []
    r = result if isinstance(result, dict) else {}

    # Memory manager
    if tool == "memory_manager":
        if action == "list":
            count = len(r.get("memories", r.get("items", [])))
            if count:
                facts.append(f"{count} memories returned")
        elif action in ("recall", "search"):
            content = r.get("content") or r.get("value") or r.get("result")
            if content:
                facts.append(f"retrieved: {str(content)[:150]}")
            else:
                facts.append("search returned empty")

    # GitHub
    elif tool == "github":
        if action == "clone_to_workspace":
            facts.append(f"cloned {r.get('repo','?')} ({r.get('files_extracted','?')} files)")
        elif action == "push_workspace":
            facts.append(f"pushed {r.get('files_pushed','?')} files to {r.get('repo','?')}")
        elif action == "commits":
            commits = r.get("commits", [])
            if commits:
                facts.append(f"{len(commits)} commits retrieved")
        elif action == "read_file":
            facts.append(f"read {r.get('path','?')} ({r.get('size','?')} bytes)")

    # Terminal
    elif tool == "terminal":
        stdout = (r.get("stdout") or "").strip()
        return_code = r.get("return_code")
        if return_code == 0 and stdout:
            facts.append(f"exit 0, output: {stdout[:120]}")
        elif return_code != 0:
            facts.append(f"exit {return_code}: {(r.get('stderr') or '')[:100]}")

    # File generation
    elif tool == "file_generation":
        url = r.get("download_url") or r.get("url")
        filename = r.get("filename")
        if url:
            facts.append(f"generated {filename or 'file'}: {url}")

    # Google Drive
    elif tool == "google_drive":
        if action in ("read", "read_by_name", "read_url"):
            res = r.get("result", {})
            name = res.get("name") or res.get("title") or res.get("url", "")
            content_len = len(res.get("content") or "")
            facts.append(f"read '{name}' — {content_len} chars")
        elif action in ("list", "search", "recent"):
            files = r.get("files", [])
            facts.append(f"{len(files)} files found")

    # Generic fallback
    message = r.get("message") or r.get("summary")
    if message and not facts:
        facts.append(str(message)[:150])

    return facts or ["completed"]


# ── Reviewer ───────────────────────────────────────────────────────────────────

REVIEWER_SYSTEM = """You are OADSON's Hallucination Reviewer. Your only job is to check whether an AI answer is grounded in the provided evidence.

You will receive:
1. TOOL RECEIPTS — what tools actually ran and what they returned
2. THE ANSWER — what the AI said

Check each factual claim in the answer against the receipts.
A claim is SUPPORTED if it appears in a ✓ receipt or in well-established general knowledge.
A claim is UNSUPPORTED if it comes from a ✗ failed tool, from nothing, or contradicts the receipts.

Reply in JSON only:
{
  "verdict": "PASS" | "REWRITE",
  "confidence": 0-100,
  "unsupported_claims": ["claim 1", "claim 2"],
  "rewrite_instruction": "short instruction for what to fix (empty string if PASS)"
}

Be strict about tool failures. If a tool failed and the answer uses data that only that tool could have provided, that is UNSUPPORTED."""


async def review_answer(
    answer: str,
    evidence: EvidenceStore,
    api_key: str,
    model: str = "gemini-3.1-flash-lite",
) -> dict:
    """
    Run the reviewer on a generated answer.
    Returns {"verdict": "PASS"|"REWRITE", "confidence": int,
             "unsupported_claims": [...], "rewrite_instruction": str}

    Non-fatal — any error returns PASS so the original answer is used.
    The reviewer should never block a response; it should only flag issues.
    """
    if not answer or not answer.strip():
        return {"verdict": "PASS", "confidence": 100, "unsupported_claims": [], "rewrite_instruction": ""}

    # Skip reviewer if no tools were called and no failures (pure conversation)
    if not evidence.receipts:
        return {"verdict": "PASS", "confidence": 95, "unsupported_claims": [], "rewrite_instruction": ""}

    receipts_text = evidence.tool_receipt_block()

    user_prompt = f"""TOOL RECEIPTS:
{receipts_text}

THE ANSWER TO REVIEW:
{answer[:3000]}

Check every factual claim. Reply in JSON only."""

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "system_instruction": {"parts": [{"text": REVIEWER_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "maxOutputTokens": 400,
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }
        # SECURITY FIX 2026-08-14: key moved from URL to x-goog-api-key
        # header — was leaking into Render's httpx request logs.
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
            if r.status_code != 200:
                logger.warning(f"Reviewer HTTP {r.status_code} — skipping")
                return {"verdict": "PASS", "confidence": 80, "unsupported_claims": [], "rewrite_instruction": ""}

            raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            # Strip markdown fences if present
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            result = json.loads(raw)
            logger.info(f"Reviewer verdict: {result.get('verdict')} (confidence {result.get('confidence')}%)")
            return result

    except Exception as e:
        logger.warning(f"Reviewer failed (non-fatal): {e}")
        return {"verdict": "PASS", "confidence": 75, "unsupported_claims": [], "rewrite_instruction": ""}


def apply_rewrite_instruction(answer: str, review: dict) -> str:
    """
    If reviewer says REWRITE and confidence < 70, append a transparent
    uncertainty note rather than silently modifying the answer.
    The note is honest — it tells the user which claims couldn't be verified.
    """
    if review.get("verdict") != "REWRITE":
        return answer

    confidence = review.get("confidence", 100)
    unsupported = review.get("unsupported_claims", [])

    if not unsupported:
        return answer

    # Only append note if confidence is low enough to warrant it
    if confidence > 70:
        return answer

    note = (
        "\n\n---\n⚠️ *Verification note: I couldn't confirm the following from actual "
        f"tool results this turn: {'; '.join(unsupported[:3])}. "
        "If these are important, let me retry the relevant checks.*"
    )
    return answer + note
