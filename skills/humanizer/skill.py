import os
import httpx

AI_TELLS = """
PATTERNS THAT GIVE AWAY AI-GENERATED TEXT (never do these):

VOCABULARY & PHRASES:
- "Delve into", "dive deep", "explore", "navigate", "in the realm of"
- "It's worth noting that", "It's important to note", "It goes without saying"
- "In today's fast-paced world", "In today's digital age", "In today's landscape"
- "Game-changer", "cutting-edge", "leverage", "utilize" (say "use"), "facilitate"
- "Comprehensive", "robust", "seamless", "synergy", "holistic"
- "Moreover", "Furthermore", "Additionally" at sentence starts
- "In conclusion", "To summarize", "In summary" as transitions
- "Unlock", "unleash", "harness", "empower" used metaphorically
- "Shed light on", "at the end of the day", "move the needle"
- Ending with "What do you think?" or "Let me know in the comments"

PUNCTUATION & STRUCTURE:
- Em dashes (—) used constantly as a stylistic crutch
- Bullet point lists for everything
- Headers and sub-headers for short pieces
- Bold text every other sentence
- Perfectly parallel sentence structure throughout
- Every paragraph being exactly the same length
- Three-part lists constantly: "X, Y, and Z"

TONE & RHYTHM:
- Overly formal and polished — no human writes that clean
- Every sentence grammatically perfect with no casual contractions
- No opinion, no personality, no voice
- Bland transitions, no personality shifts
- Starting every paragraph with a topic sentence like an essay
- No fragments. No short punchy lines. Ever.

AI STRUCTURAL PATTERNS:
- Introduction then 3 body points then conclusion for everything
- Numbered lists with explanations that all match the same template
- Qualifying everything with "however", "on the other hand", "while it's true that"
- Never taking a hard stance
- Passive voice overuse: "it can be seen that", "it is important that"
"""

HUMANIZE_SYSTEM = f"""You are a text humanizer. Rewrite text so it reads like a real person wrote it, not an AI.

AI patterns to eliminate:
{AI_TELLS}

What human writing looks like:
- Contractions: don't, it's, I'll, you're, that's
- Sentence variety: mix short punchy lines with longer ones. Sometimes fragments.
- Real opinions and takes
- Occasional informal choices (kinda, a lot, basically, honestly)
- Imperfect transitions
- Specific details over vague generalities
- Actual personality and voice

Rules:
1. Never use any AI tell phrases above
2. Preserve all facts and core meaning
3. Match the requested tone
4. No em dashes unless genuinely needed
5. Output should pass AI detection by having real voice, not by being worse"""


async def _gemini(prompt: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY", "")
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    # SECURITY FIX 2026-08-14: key moved from URL to x-goog-api-key
    # header — was leaking into Render's httpx request logs.
    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
    data = res.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or payload
    action = args.get("action", "humanize")
    text = args.get("text", "") or payload.get("message", "")
    tone = args.get("tone", "natural")
    context = args.get("context", "")
    section_title = args.get("section_title", "")

    if not text:
        return {
            "status": "info",
            "message": (
                "Humanizer — eliminate AI writing patterns.\n\n"
                "Actions: humanize (default), scan, section\n"
                "Tones: natural, casual, professional, academic, social\n"
                "Params: text (required), tone, context, section_title"
            )
        }

    tone_map = {
        "casual":       "Casual: contractions, short lines, real opinions, informal.",
        "professional": "Professional but human — like a competent person writing, not a robot report.",
        "academic":     "Academic but not robotic — like a real student who knows the material.",
        "social":       "Social media: punchy, direct, no fluff.",
        "natural":      "Natural: however a real person would most naturally write this.",
    }
    tone_instruction = tone_map.get(tone, tone_map["natural"])
    context_line = f"\nExtra instruction: {context}" if context else ""

    try:
        if action == "humanize":
            prompt = f"""{HUMANIZE_SYSTEM}

TONE: {tone_instruction}
{context_line}

TEXT TO HUMANIZE:
\"\"\"
{text}
\"\"\"

Output ONLY the rewritten text. No preamble, no explanation."""

            result = await _gemini(prompt)
            for prefix in ["Here's the rewritten", "Here is the", "Rewritten:", "Humanized:"]:
                if result.startswith(prefix):
                    result = result.split("\n", 1)[-1].strip()

            return {
                "status": "success",
                "humanized": result,
                "tone": tone,
                "message": f"Text humanized ({tone} tone)"
            }

        elif action == "scan":
            prompt = f"""You are an AI writing pattern detector.

Patterns that give away AI text:
{AI_TELLS}

Scan this text and list every AI pattern found. Quote the exact phrase from the text. Give an overall risk score: Low / Medium / High.

TEXT:
\"\"\"
{text}
\"\"\"

Be specific. Don't be vague."""

            result = await _gemini(prompt)
            return {
                "status": "success",
                "analysis": result,
                "message": "AI pattern scan complete"
            }

        elif action == "section":
            prompt = f"""{HUMANIZE_SYSTEM}

TONE: Professional but human — like a real expert wrote this.
You are rewriting the section: "{section_title or 'Section'}"
This is part of a larger document. Don't add a new intro or conclusion — just rewrite this section.
{context_line}

SECTION TEXT:
\"\"\"
{text}
\"\"\"

Output ONLY the rewritten section."""

            result = await _gemini(prompt)
            return {
                "status": "success",
                "humanized": result,
                "section": section_title or "Section",
                "message": f"Section humanized"
            }

        else:
            return {"status": "error", "reason": f"Unknown action '{action}'. Use: humanize, scan, section"}

    except Exception as e:
        return {"status": "error", "reason": str(e)}
