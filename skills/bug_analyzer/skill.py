"""
Bug Analyzer Skill.

REWRITTEN: previously analyze_error() returned a hardcoded, generic
suggestion list ("Check logs for context", "Review recent changes", "Test
in isolation") regardless of what the actual error said — not real
analysis, just a canned response wrapped in a dict. Now routes the real
error/stack trace through Gemini for actual analysis specific to what was
provided, same pattern as password_check's suggest_password.
"""

import os


class BugAnalyzerSkill:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        from google import genai
        self.client = genai.Client(api_key=self.api_key)

    async def analyze_error(self, error_message: str) -> dict:
        """Analyze a real error message/stack trace via Gemini."""
        try:
            prompt = f"""Analyze this error/stack trace for a software developer:

{error_message}

Provide:
1. Likely root cause(s)
2. Specific next debugging steps (not generic advice)
3. Common fixes for this type of error, if applicable

Be concrete and specific to the actual error shown, not generic debugging tips."""

            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )

            return {
                "status": "success",
                "error_analyzed": error_message[:200],
                "analysis": response.text,
                "message": "Error analysis complete"
            }

        except Exception as e:
            return {"status": "error", "reason": str(e)}


async def run(payload):
    """Execute bug_analyzer skill with structured args."""
    args = payload.get("args", {}) or {}
    error_message = (args.get("error_message") or "").strip()

    if not error_message:
        return {
            "status": "info",
            "message": "Bug Analyzer: provide an error message or stack trace to analyze"
        }

    try:
        skill = BugAnalyzerSkill()
        return await skill.analyze_error(error_message)
    except Exception as e:
        return {"status": "error", "reason": str(e)}
