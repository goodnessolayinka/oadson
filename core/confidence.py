"""
core/confidence.py — Phase 10, Group 1: Tool Confidence Scoring.

Scores how much the orchestrator (and eventually the model) should trust
a skill's result, separate from the skill's own "status" field. A skill
can report "status": "success" and still hand back something worth
distrusting — an empty list where a real answer was expected, or a
result that only succeeded after retries.

Purely additive right now: nothing in the actual tool-calling decision
loop reads this yet. It feeds reflection.py and the step log/SSE stream.
This is groundwork for later self-review, not a rewrite of the loop.
"""


def score_skill_result(skill_name: str, args: dict, exec_result: dict, retry_count: int = 0) -> dict:
    """
    Returns {"confidence": 0.0-1.0, "label": "high"|"medium"|"low", "reasons": [...]}.
    Never raises — worst case it returns a neutral score, since this must
    never be allowed to break the actual skill-execution path.
    """
    try:
        reasons = []
        confidence = 1.0
        status = (exec_result or {}).get("status")

        if status == "error":
            return {
                "confidence": 0.0,
                "label": "low",
                "reasons": [exec_result.get("reason", "skill reported an error")],
            }

        if status not in ("success", "info"):
            confidence -= 0.4
            reasons.append(f"unexpected status '{status}'")

        # Common "technically succeeded but came back empty" shapes across
        # skills — gmail, google_calendar, google_drive, memory_manager,
        # session_log, etc. all nest their payload under "data" or "result".
        data = (exec_result or {}).get("data")
        if data is None:
            data = (exec_result or {}).get("result")

        if isinstance(data, dict):
            empty_signal_keys = (
                "count", "total", "unread_emails", "emails", "events",
                "files", "memories", "sessions",
            )
            for k in empty_signal_keys:
                if k in data and not data[k]:
                    confidence -= 0.25
                    reasons.append(f"'{k}' came back empty")
                    break
        elif data in (None, "", [], {}):
            confidence -= 0.3
            reasons.append("skill returned no data")

        if retry_count > 0:
            confidence -= 0.15 * retry_count
            reasons.append(f"took {retry_count} retr{'y' if retry_count == 1 else 'ies'} to succeed")

        confidence = max(0.0, min(1.0, confidence))
        label = "high" if confidence >= 0.75 else "medium" if confidence >= 0.4 else "low"

        if not reasons:
            reasons.append("clean success")

        return {"confidence": round(confidence, 2), "label": label, "reasons": reasons}

    except Exception:
        # Scoring is best-effort UX/telemetry, never a gate on execution.
        return {"confidence": 0.5, "label": "medium", "reasons": ["confidence scoring failed"]}
