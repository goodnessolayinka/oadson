"""
memory/vault.py — OADSON V3 MemoryVault.

Two-tier memory system:
  - CORE SUMMARY: one flowing paragraph per user (set_core_summary /
    get_core_summary / extend_core_summary / bootstrap_core_summary).
    Injected into every conversation like Claude's memory panel.
    No keys visible, no structure — just prose.

  - EXTENDED: key-value entries (general, project, reflection categories).
    Retrieved semantically on demand via the reasoning engine.

store() / get_core_memories() kept for backward compat — old pinned entries
still exist, they just aren't the primary path anymore once a summary exists.

EMBEDDING SEARCH (added):
  store() now embeds each memory's content+key at write time using
  gemini-embedding-001 (same model as code_index.py, same batch API).
  search() uses cosine similarity against stored vectors when available,
  falling back to token-overlap for memories that predate this change
  or when the embed call fails. list_by_categories() accepts an optional
  query and returns scored+sorted results when one is given.

  Vectors are stored as `embedding: [float, ...]` (768-dim) directly on
  each memory doc — no Atlas Vector Search needed, brute-force cosine in
  Python same as code_index.py (one person's memory vault, not a scale
  problem). Embeddings are generated fire-and-forget on store() — a
  failed embed never blocks the write, the doc just falls back to
  token-overlap on search until it gets a vector on next update.
"""

import os
import re
import logging
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger("oadson.memory.vault")


def _tokenise(text: str) -> set:
    # Normalize underscores and hyphens to spaces first so compound
    # key names like academic_comeback_timetable_v2 are split correctly.
    # Without this, \b word boundaries don't fire on underscores and the
    # whole key matches as one unknown token — search returns 0 every time.
    text = text.replace("_", " ").replace("-", " ")
    return set(re.findall(r"\b[a-z]{3,}\b", text.lower()))


def _semantic_overlap(query: str, text: str) -> float:
    q = _tokenise(query)
    t = _tokenise(text)
    if not q or not t:
        return 0.0
    overlap = len(q & t)
    return overlap / max(len(q | t), 1)


# ── Embedding helpers ─────────────────────────────────────────────────────────

_EMBED_MODEL = "gemini-embedding-001"
_EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_EMBED_MODEL}:batchEmbedContents"
)
_EMBED_DIMS = 768


async def _embed_batch(texts: list[str]) -> list[list[float] | None]:
    """
    Embed a batch of texts using gemini-embedding-001.
    Returns a parallel list — None in any slot means that text failed.
    Never raises; logs and returns all-None on total failure.
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or not texts:
        return [None] * len(texts)

    requests_body = [
        {
            "model": f"models/{_EMBED_MODEL}",
            "content": {"parts": [{"text": t[:8000]}]},
            "outputDimensionality": _EMBED_DIMS,
        }
        for t in texts
    ]
    try:
        # SECURITY FIX 2026-08-14: was params={"key": api_key}, which
        # httpx still renders as a ?key=... query string on the wire —
        # exactly what showed up leaking in Render's httpx request logs.
        # Moved to the x-goog-api-key header instead.
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                _EMBED_URL,
                headers={"x-goog-api-key": api_key},
                json={"requests": requests_body},
            )
        if res.status_code != 200:
            logger.warning(f"Embedding API error {res.status_code}: {res.text[:200]}")
            return [None] * len(texts)
        data = res.json()
        embeddings = data.get("embeddings", [])
        result = []
        for i, e in enumerate(embeddings):
            values = e.get("values")
            result.append(values if values and len(values) == _EMBED_DIMS else None)
        # Pad if API returned fewer embeddings than requested
        while len(result) < len(texts):
            result.append(None)
        return result
    except Exception as exc:
        logger.warning(f"Embedding batch failed: {exc}")
        return [None] * len(texts)


async def _embed_one(text: str) -> list[float] | None:
    """Convenience wrapper for a single text."""
    results = await _embed_batch([text])
    return results[0]


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class MemoryVault:

    def __init__(self, db, user_id: str):
        self.db = db
        self.user_id = user_id
        self.collection = db["memory"]

    # ── CORE SUMMARY (structured JSON, rendered for injection) ────────────────
    # Stored as JSON with defined fields. Rendered to ~500-1000 words per turn.
    # Target: ~700 words injected. Hard ceiling: ~1200 words.
    # High-value persistent facts ONLY. Everything else goes in the vault.
    #
    # Schema:
    #   identity            — name, academic status, location, relationships
    #   active_projects     — what's being built right now
    #   long_term_goals     — major objectives
    #   preferences         — communication style, tools, workflows
    #   persistent_constraints — hardware limits, budget, things to always account for
    #   behavioral_rules    — HOW OADSON should respond to this user
    #   version             — incremented on each structured update

    async def get_core_summary(self) -> Optional[dict]:
        """Fetch the core memory doc. May be structured (JSON) or legacy prose."""
        return await self.collection.find_one(
            {"user_id": self.user_id, "key": "user_summary", "category": "core_summary"},
            {"_id": 0, "versions": 0},
        )

    async def set_core_summary(self, data) -> None:
        """
        Upsert core summary.
        data = dict  → stored as JSON (structured=True)
        data = str   → stored as prose (structured=False, legacy compat)
        """
        now = datetime.utcnow()
        if isinstance(data, dict):
            content = __import__("json").dumps(data, ensure_ascii=False)
            structured = True
            version = data.get("version", 1)
        else:
            content = str(data).strip()
            structured = False
            version = 1

        await self.collection.update_one(
            {"user_id": self.user_id, "key": "user_summary", "category": "core_summary"},
            {
                "$set": {
                    "content": content,
                    "structured": structured,
                    "version": version,
                    "category": "core_summary",
                    "key": "user_summary",
                    "type": "user",
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    def render_core_for_injection(self, doc: dict) -> str:
        """
        Convert the core summary doc into the text block injected every turn.
        Structured (JSON) → formatted sections, ~500-1000 words.
        Legacy prose → returned as-is.
        """
        if not doc:
            return ""
        content = doc.get("content", "").strip()
        if not content:
            return ""

        if not doc.get("structured", False):
            return content  # legacy prose fallback

        try:
            import json as _json
            data = _json.loads(content)
        except Exception:
            return content  # bad JSON, fall back to raw

        version = data.get("version", "?")
        updated = doc.get("updated_at")
        date_str = updated.strftime("%Y-%m-%d") if updated and hasattr(updated, "strftime") else ""
        header = f"CORE MEMORY [v{version}{' · ' + date_str if date_str else ''}]"

        _SECTIONS = [
            ("identity",               "IDENTITY"),
            ("active_projects",        "ACTIVE PROJECTS"),
            ("long_term_goals",        "LONG-TERM GOALS"),
            ("preferences",            "PREFERENCES"),
            ("persistent_constraints", "PERSISTENT CONSTRAINTS"),
            ("behavioral_rules",       "BEHAVIORAL RULES"),
        ]

        parts = [header, ""]
        for field, title in _SECTIONS:
            val = data.get(field)
            if not val:
                continue
            parts.append(title)
            if isinstance(val, str):
                parts.append(val)
            elif isinstance(val, list):
                for item in val:
                    parts.append(f"• {item}")
            elif isinstance(val, dict):
                for k, v in val.items():
                    parts.append(f"• {k}: {v}")
            parts.append("")

        return "\n".join(parts).strip()

    async def extend_core_summary(self, new_facts: list, api_key: str) -> str:
        """
        Update the core summary with new facts.
        REPLACE/UPDATE semantics — old facts are overwritten, not appended.
        After update, if rendered output > 1200 words, triggers compression.
        Never raises.
        """
        current = await self.get_core_summary()
        current_content = (current.get("content") or "") if current else ""
        structured = (current.get("structured", False)) if current else False

        import json as _json
        facts_text = "\n".join(f"- {f}" for f in new_facts)

        if structured and current_content:
            try:
                current_data = _json.loads(current_content)
                current_version = current_data.get("version", 1)
            except Exception:
                current_data = {}
                current_version = 1

            today_str = datetime.utcnow().strftime("%A, %Y-%m-%d")
            prompt = (
                f"Update this structured core memory JSON with new facts.\n\n"
                f"CURRENT STRUCTURE:\n{current_content}\n\n"
                f"NEW FACTS:\n{facts_text}\n\n"
                f"Today is {today_str}.\n\n"
                "Rules:\n"
                "- Return ONLY valid JSON, no markdown, no explanation\n"
                "- REPLACE or UPDATE existing entries — do not just append\n"
                "- If a project is completed or abandoned, REMOVE it from active_projects\n"
                "- If a constraint is resolved, REMOVE it from persistent_constraints\n"
                "- If a preference changed, UPDATE it — don't keep both old and new\n"
                "- Merge similar items instead of duplicating\n"
                "- Keep each list to 4-6 items max — current over historical\n"
                f"- Increment version to {current_version + 1}\n"
                "- Total rendered output must stay under 1000 words\n"
                # FIX 2026-08-29: real bug, confirmed live — the proactive
                # agent repeated "yesterday's exam" for multiple days in
                # a row. Root cause: this summary is DURABLE (survives
                # until the next extend call happens to touch that fact
                # again, which could be days or never), but nothing here
                # ever told the model writing it to avoid relative-time
                # language. "has an exam tomorrow" is only true the day
                # it's written — every day after, it's silently wrong,
                # and nothing about a wrong date trips any error, so it
                # just sits there being read as current forever. Fixed
                # at the source: convert any relative-time fact into an
                # absolute date NOW, while the current date is known, so
                # the phrase stays correct regardless of how long it
                # goes unedited after this.\n"
                "- NEVER write relative time words into any field —\n"
                "  \"today\", \"tomorrow\", \"yesterday\", \"this week\", \"next\n"
                "  week\", \"in 3 days\", etc. This summary is durable and\n"
                "  may be read unchanged for days or weeks — a relative-\n"
                "  time phrase becomes SILENTLY WRONG the moment the day\n"
                "  changes, with nothing to catch it. If a new fact\n"
                "  mentions relative time, convert it to an absolute date\n"
                "  using today's date above (e.g. \"exam tomorrow\" on a\n"
                "  Wednesday becomes \"exam Thursday, 2026-08-27\", not\n"
                "  \"exam tomorrow\"). If a fact has already passed relative\n"
                "  to today's date (e.g. an exam date that's now in the\n"
                "  past), REMOVE it rather than carry it forward as a\n"
                "  future event.\n"
            )
        else:
            today_str = datetime.utcnow().strftime("%A, %Y-%m-%d")
            prompt = (
                "Update this personal memory summary with new facts.\n\n"
                f"CURRENT SUMMARY:\n{current_content}\n\n"
                f"NEW FACTS:\n{facts_text}\n\n"
                f"Today is {today_str}.\n\n"
                "Rules:\n"
                "- Prose only, third person, no bullets\n"
                "- REPLACE outdated facts — don't keep old and new versions together\n"
                "- Remove completed projects and resolved issues\n"
                "- Stay under 300 words\n"
                # Same fix as the structured branch above — see its comment.
                "- NEVER use relative time words (\"today\", \"tomorrow\",\n"
                "  \"yesterday\", \"this week\", etc.) — this summary is read\n"
                "  unchanged for days, so convert any relative time to an\n"
                "  absolute date using today's date above, and remove any\n"
                "  fact whose date has already passed.\n\n"
                "Output ONLY the updated summary."
            )

        try:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-3.5-flash-lite:generateContent"
            )
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 1200, "temperature": 0.2},
            }
            # SECURITY FIX 2026-08-14: key moved from URL to
            # x-goog-api-key header — was leaking into Render's httpx
            # request logs.
            async with httpx.AsyncClient(timeout=20) as client:
                res = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})

                # BUG FIXED 2026-08-14: 'raw' was used on the next line
                # but never assigned anywhere in this function — every
                # single call hit a NameError-equivalent
                # (UnboundLocalError) here, was silently swallowed by
                # the except below, and fell through to the fallback
                # every time. This is why extend_core_summary never
                # actually updated anything and always logged
                # "cannot access local variable 'raw'". Every other
                # Gemini-calling function in this file extracts the
                # response text the same way — this one was just
                # missing the line.
                if res.status_code != 200:
                    raise RuntimeError(f"Gemini call failed: {res.status_code} {res.text[:200]}")
                raw = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

                if structured:
                    raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
                    try:
                        parsed = _json.loads(raw)
                        await self.set_core_summary(parsed)
                    except Exception:
                        await self.set_core_summary(raw)
                else:
                    await self.set_core_summary(raw)

                # Word count ceiling — compress if grown too large
                doc = await self.get_core_summary()
                rendered = self.render_core_for_injection(doc)
                if len(rendered.split()) > 1200:
                    rendered = await self._compress_core(api_key)
                return rendered

        except Exception as e:
            logger.warning(f"extend_core_summary failed (non-fatal): {e}")

        # Fallback
        fallback = (current_content + "\n" + "; ".join(new_facts)).strip()
        await self.set_core_summary(fallback)
        return fallback

    async def _compress_core(self, api_key: str) -> str:
        """
        Triggered when core summary exceeds 1200 words.
        Compresses to 600-800 words — removes completed/stale items,
        merges duplicates, keeps only what's currently true.
        """
        doc = await self.get_core_summary()
        if not doc:
            return ""

        import json as _json
        content = doc.get("content", "")
        structured = doc.get("structured", False)
        rendered = self.render_core_for_injection(doc)

        if structured:
            try:
                current_data = _json.loads(content)
                current_version = current_data.get("version", 1)
            except Exception:
                current_version = 1

            prompt = (
                f"This core memory has grown too large. Compress it.\n\n"
                f"CURRENT (JSON):\n{content}\n\n"
                "Rules:\n"
                "- Return ONLY valid JSON (same schema)\n"
                "- Remove completed or abandoned projects from active_projects\n"
                "- Remove resolved constraints from persistent_constraints\n"
                "- Merge similar preference or rule items\n"
                "- Keep each list to 4 items max\n"
                "- identity: 2 sentences max\n"
                f"- Increment version to {current_version + 1}\n"
                "- Target: 600-800 words when rendered\n"
            )
        else:
            prompt = (
                f"Compress this memory summary to 300 words max.\n\n"
                f"CURRENT:\n{rendered}\n\n"
                "Remove completed projects, outdated facts, resolved issues. "
                "Keep current identity, active projects, persistent preferences. "
                "Output ONLY the compressed summary."
            )

        try:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-3.5-flash-lite:generateContent"
            )
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 900, "temperature": 0.1},
            }
            # SECURITY FIX 2026-08-14: key moved from URL to
            # x-goog-api-key header — was leaking into Render's httpx
            # request logs.
            async with httpx.AsyncClient(timeout=20) as client:
                res = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
            if res.status_code == 200:
                raw = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                if structured:
                    raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
                    try:
                        parsed = _json.loads(raw)
                        await self.set_core_summary(parsed)
                        doc2 = await self.get_core_summary()
                        return self.render_core_for_injection(doc2)
                    except Exception:
                        pass
                await self.set_core_summary(raw)
                return raw
        except Exception as e:
            logger.warning(f"_compress_core failed: {e}")

        return rendered  # return uncompressed if compression itself fails


    async def bootstrap_core_summary(self, api_key: str) -> dict:
        """
        Build / rebuild structured core memory from the entire vault.
        Produces a JSON object with defined fields, rendered to ~700 words.
        Target: 500-1000 words injected. Hard ceiling: ~1200 words.
        Reads all categories except reflection, working, core_summary.

        BUG FIXED 2026-08-16 (real user report: "rebuild memory is
        [degrading] core memory"). This function and deduplicate() were
        two completely disconnected code paths — deduplicate() is only
        ever called manually via memory_manager's own separate action,
        while THIS function (the one the "Rebuild Memory" button in
        Settings actually calls, via api/memory.py) never ran it first.
        If near-duplicate or superseding facts had piled up in the vault
        since the last manual deduplicate() call — which for most users
        is "never", since nothing prompts them to run it — pressing
        Rebuild Memory baked all of that unmerged mess into the new core
        summary with zero cleanup. Fixed by always deduplicating first,
        automatically, as the first step of every rebuild — not an
        optional separate action the user has to remember to run.
        Non-fatal: a dedup failure logs and continues with the rebuild
        rather than blocking it, since a rebuild with some remaining
        duplicates is still strictly better than no rebuild at all.
        """
        try:
            dedup_result = await self.deduplicate()
            if dedup_result.get("merged"):
                logger.info(
                    f"bootstrap_core_summary: deduplicated {dedup_result['merged']} "
                    f"pair(s) before rebuilding core memory for {self.user_id}"
                )
        except Exception as e:
            logger.warning(f"bootstrap_core_summary: pre-rebuild deduplicate failed (non-fatal): {e}")

        SKIP_CATEGORIES = {"core_summary", "reflection", "working"}
        PRIORITY_ORDER = ["core", "general", "project", "personal"]

        # BUG FIXED 2026-08-16 (part 2): no sort at all meant entries
        # within a category came back in whatever order Mongo happened
        # to store them (insertion order, not relevance or recency) —
        # so within the 200-entry cap below, older facts could just as
        # easily win a slot over a newer, more accurate one. Sorting by
        # updated_at descending means the most recently confirmed/
        # corrected facts are the ones kept when the cap trims the list,
        # matching every other read path in this file (search(),
        # get_recent(), etc. all already sort this way — this was the
        # one exception).
        cursor = self.collection.find(
            {
                "user_id": self.user_id,
                "category": {"$nin": list(SKIP_CATEGORIES)},
            },
            {"key": 1, "content": 1, "category": 1, "updated_at": 1, "_id": 0},
        ).sort("updated_at", -1)

        by_category: dict = {}
        async for doc in cursor:
            cat = doc.get("category", "general")
            key = (doc.get("key") or "").replace("_", " ").strip()
            content = (doc.get("content") or "").strip()
            if not content:
                continue
            entry = f"{key}: {content}" if key and key != "user summary" else content
            by_category.setdefault(cat, []).append(entry)

        if not by_category:
            return {
                "status": "info",
                "message": "No memories in the vault yet.",
                "summary": "",
            }

        entries = []
        for cat in PRIORITY_ORDER:
            entries.extend(by_category.pop(cat, []))
        for rest in by_category.values():
            entries.extend(rest)

        total = len(entries)
        entries = entries[:200]
        facts_text = "\n".join(f"- {e}" for e in entries)

        import json as _json

        # Get current version if exists
        current = await self.get_core_summary()
        current_version = 1
        if current and current.get("structured"):
            try:
                current_version = _json.loads(current["content"]).get("version", 1) + 1
            except Exception:
                pass

        prompt = f"""You are building a structured core memory for an AI assistant called OADSON.

The user has these facts in their memory vault:
{facts_text}

Return a JSON object with EXACTLY these fields:

{{
  "identity": "2-3 sentences: name, university, course, year, location, key relationships. Dense, no filler.",
  "active_projects": [
    "OADSON V3 — description of what it is, current state, stack",
    "Other project — description"
  ],
  "long_term_goals": [
    "Goal 1 — 1 sentence",
    "Goal 2 — 1 sentence"
  ],
  "preferences": [
    "Communication style preference",
    "Tool/workflow preference",
    "Important recurring habit or constraint"
  ],
  "persistent_constraints": [
    "Hardware or budget constraint",
    "Deadline or academic obligation that affects work",
    "Platform constraint (free tier, mobile-only, etc.)"
  ],
  "behavioral_rules": [
    "How OADSON should respond to this user",
    "Things OADSON must always remember when helping them"
  ],
  "version": {current_version}
}}

Rules:
- Return ONLY the JSON — no markdown fences, no explanation, no prose outside the JSON
- Total rendered output: target 700 words, hard ceiling 1000 words
- Each list item: 1-2 dense sentences, no padding
- HIGH-VALUE persistent facts ONLY — skip granular dates, one-off events, specific times
- active_projects: only things currently being built
- preferences: stable patterns, not one-time choices
- behavioral_rules: specific to how OADSON should behave with THIS user"""

        try:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-3.5-flash-lite:generateContent"
            )
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 1800, "temperature": 0.1},
            }
            # SECURITY FIX 2026-08-14: key moved from URL to
            # x-goog-api-key header — was leaking into Render's httpx
            # request logs.
            async with httpx.AsyncClient(timeout=45) as client:
                res = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})

            if res.status_code == 200:
                raw = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
                parsed = _json.loads(raw)
                await self.set_core_summary(parsed)
                doc = await self.get_core_summary()
                rendered = self.render_core_for_injection(doc)
                return {
                    "status": "success",
                    "message": f"Built structured core memory from {len(entries)} memories ({total} total in vault). v{current_version}.",
                    "summary": rendered,
                    "sources_count": len(entries),
                }

        except _json.JSONDecodeError as e:
            logger.warning(f"bootstrap JSON parse failed: {e} — falling back to prose")
        except Exception as e:
            logger.warning(f"bootstrap_core_summary failed: {e}")

        # Fallback: prose via flash-lite
        try:
            prose_prompt = (
                f"Write a concise personal memory summary (600-800 words, prose, third person, no bullets) "
                f"from these facts. Cover identity, projects, preferences, constraints, behavioral rules.\n\n"
                f"Facts:\n{facts_text}\n\nOutput ONLY the summary."
            )
            url_lite = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-3.5-flash-lite:generateContent"
            )
            payload2 = {
                "contents": [{"role": "user", "parts": [{"text": prose_prompt}]}],
                "generationConfig": {"maxOutputTokens": 1200, "temperature": 0.2},
            }
            # SECURITY FIX 2026-08-14: key moved from URL to
            # x-goog-api-key header — was leaking into Render's httpx
            # request logs.
            async with httpx.AsyncClient(timeout=30) as client:
                res2 = await client.post(url_lite, json=payload2, headers={"x-goog-api-key": api_key})
            if res2.status_code == 200:
                prose = res2.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                await self.set_core_summary(prose)
                return {
                    "status": "partial",
                    "message": "Structured build failed — prose fallback used. Try rebuilding.",
                    "summary": prose,
                    "sources_count": len(entries),
                }
        except Exception as e2:
            logger.warning(f"bootstrap prose fallback also failed: {e2}")

        return {
            "status": "error",
            "message": "Both synthesis attempts failed. Check GEMINI_API_KEY.",
            "summary": "",
        }

    # ── LEGACY KEY-VALUE (backward compat) ───────────────────────────────────

    async def store(
        self,
        key: str,
        value,
        category: str = "general",
        supersede_check: bool = True,
    ) -> dict:
        """
        Store a memory with version tracking and contradiction detection.
        Still used for EXTEND-tier facts and project/reflection memories.
        CORE-tier facts should now go through extend_core_summary() instead.

        Embedding: embeds content+key text after the DB write (fire-and-forget).
        A failed embed never blocks the write — the doc just uses token-overlap
        fallback in search() until it gets a vector on next update.
        """
        now = datetime.utcnow()
        content = str(value) if not isinstance(value, str) else value

        existing = await self.collection.find_one(
            {"user_id": self.user_id, "key": key}
        )

        if existing:
            old_content = existing.get("content", "")
            contradiction = (
                supersede_check
                and old_content
                and old_content.strip().lower() != content.strip().lower()
            )

            update = {
                "$set": {
                    "content": content,
                    "category": category,
                    "type": "user",
                    "updated_at": now,
                    "superseded": False,
                },
            }
            if old_content:
                update["$push"] = {
                    "versions": {
                        "content": old_content,
                        "category": existing.get("category", category),
                        "recorded_at": existing.get("updated_at", now),
                        "superseded": contradiction,
                    }
                }
            await self.collection.update_one(
                {"user_id": self.user_id, "key": key},
                update,
            )
            result = {
                "status": "updated",
                "key": key,
                "contradiction_detected": contradiction,
                "previous_value": old_content if contradiction else None,
            }
        else:
            await self.collection.insert_one({
                "user_id": self.user_id,
                "key": key,
                "content": content,
                "category": category,
                "type": "user",
                "created_at": now,
                "updated_at": now,
                "superseded": False,
                "versions": [],
            })
            result = {"status": "stored", "key": key}

        # Embed fire-and-forget — write succeeded regardless of embed outcome
        embed_text = f"{key}: {content}"
        vector = await _embed_one(embed_text)
        if vector:
            try:
                await self.collection.update_one(
                    {"user_id": self.user_id, "key": key},
                    {"$set": {"embedding": vector, "embedding_model": _EMBED_MODEL}},
                )
            except Exception as e:
                logger.warning(f"Failed to persist embedding for key '{key}': {e}")

        return result

    async def get_core_memories(self, limit: int = 30) -> list:
        """
        Legacy: fetch old-style pinned key-value core memories.
        Used as a fallback when no core_summary exists yet.
        """
        cursor = self.collection.find(
            {"user_id": self.user_id, "category": "core"},
            {"_id": 0, "versions": 0}
        ).sort("updated_at", -1).limit(limit)
        return [doc async for doc in cursor]

    async def pin(self, key: str) -> dict:
        """Promote an existing memory to core (always-in-context) tier."""
        result = await self.collection.update_one(
            {"user_id": self.user_id, "key": key},
            {"$set": {"category": "core", "updated_at": datetime.utcnow()}},
        )
        if result.matched_count == 0:
            return {"status": "error", "reason": f"No memory found with key '{key}'"}
        return {"status": "pinned", "key": key}

    async def unpin(self, key: str, new_category: str = "general") -> dict:
        """Demote a core memory back to extended memory tier."""
        result = await self.collection.update_one(
            {"user_id": self.user_id, "key": key, "category": "core"},
            {"$set": {"category": new_category, "updated_at": datetime.utcnow()}},
        )
        if result.matched_count == 0:
            return {"status": "error", "reason": f"No core memory found with key '{key}'"}
        return {"status": "unpinned", "key": key, "moved_to": new_category}

    async def recall(self, key: str) -> Optional[str]:
        doc = await self.collection.find_one({"user_id": self.user_id, "key": key})
        return doc.get("content") if doc else None

    async def delete(self, key: str) -> bool:
        result = await self.collection.delete_one({"user_id": self.user_id, "key": key})
        return result.deleted_count > 0

    async def delete_matching(self, query: str, min_score: float = 0.55, limit: int = 10) -> dict:
        """
        ADDED 2026-08-22 — real fix for a genuine production incident:
        a user told OADSON in chat "remove anything GST from memory,
        I've finished the exams" multiple times, and the proactive
        agent kept mentioning GST in daily check-ins for DAYS afterward
        anyway — until the user deleted the entire chat. Traced back to
        this: the model called memory_manager's `delete` action with an
        exact `key`, and picked the WRONG key (a completely unrelated
        one, `completed_exams`) despite `search` having just returned
        the correct GST-related keys moments earlier in the same
        conversation. The delete-by-exact-key skill itself worked
        exactly as designed; the model's own key-transcription across
        two separate tool calls was the actual point of failure.

        This removes that failure mode structurally rather than relying
        on the model to copy a key correctly: the model describes what
        to forget in plain language (e.g. "anything about GST"), this
        method runs the SAME search().search() the model would have
        used to look it up, and deletes exactly what that search
        matched — no key transcription step for the model to get wrong
        at all. min_score mirrors search()'s own relevance threshold
        (kept in sync — see that method) so this doesn't casually nuke
        loosely-related memories on a vague query.

        Returns {"deleted": [...], "count": N} so the caller (see
        skills/memory_manager/skill.py's new delete_matching action)
        can show the user exactly what got removed — critical given
        this is a genuinely destructive, multi-record operation.
        """
        results = await self.search(
            query, limit=limit, exclude_categories=["reflection", "core_summary"],
            min_relevance=min_score,
        )
        if not results:
            return {"deleted": [], "count": 0}
        deleted = []
        for m in results:
            key = m.get("key")
            if key and await self.delete(key):
                deleted.append({"key": key, "content": str(m.get("content", ""))[:120]})
        return {"deleted": deleted, "count": len(deleted)}

    async def list(self, category: str = None) -> list:
        """Alias for list_all for backward compat with reflection.py."""
        return await self.list_all(category)

    async def list_all(self, category: str = None) -> list:
        filter_ = {"user_id": self.user_id}
        if category:
            filter_["category"] = category
        cursor = self.collection.find(filter_, {"_id": 0, "versions": 0}).sort("updated_at", -1)
        return [doc async for doc in cursor]

    async def list_by_categories(
        self,
        categories: list,
        limit_each: int = 20,
        query: str = None,
    ) -> list:
        """
        Pull memories from multiple categories in one method call.
        Used by the reasoning engine for category-aware retrieval.
        Excludes core_summary — that's injected separately via get_core_summary().

        If query is given, results are scored and sorted by cosine similarity
        (with token-overlap fallback for docs without vectors), so the reasoning
        engine gets the most relevant memories first rather than most-recent.
        """
        results = []
        seen = set()
        for cat in categories:
            if cat == "core_summary":
                continue
            cursor = self.collection.find(
                {"user_id": self.user_id, "category": cat},
                {"_id": 0, "versions": 0}
            ).sort("updated_at", -1).limit(limit_each)
            async for doc in cursor:
                key = doc.get("key", "")
                if key not in seen:
                    seen.add(key)
                    results.append(doc)

        if not query or not results:
            return results

        # Score and sort by semantic relevance to query
        query_vec = await _embed_one(query)
        scored = []
        for doc in results:
            vec = doc.get("embedding")
            if query_vec and vec and len(vec) == _EMBED_DIMS:
                score = _cosine(query_vec, vec)
            else:
                # Fallback: token overlap for docs without embeddings yet
                content = str(doc.get("content", ""))
                key = str(doc.get("key", ""))
                score = _semantic_overlap(query, content + " " + key)
            scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        # Strip embedding vector from results before returning (large payload)
        out = []
        for _, doc in scored:
            doc.pop("embedding", None)
            doc.pop("embedding_model", None)
            out.append(doc)
        return out

    async def search(
        self,
        query: str,
        limit: int = 10,
        exclude_categories: list = None,
        min_relevance: float = 0.0,
    ) -> list:
        """
        Semantic memory search using cosine similarity on stored embeddings.
        Falls back to token-overlap for any doc that doesn't have a vector yet
        (memories written before the embedding upgrade, or ones whose embed
        call failed at write time).

        Excludes reflection and core_summary by default — core_summary is
        injected separately into the prompt, not searched.

        min_relevance: ADDED 2026-08-22 alongside delete_matching() below —
        filters out weak matches BEFORE the _relevance field gets stripped
        from the returned docs (see the strip a few lines down). Defaults
        to 0.0, meaning every existing caller of search() is completely
        unaffected — this is purely additive. delete_matching() is the
        one caller that actually needs a real threshold, since it's the
        difference between "delete exactly what was asked" and "delete
        anything vaguely related", a much higher-stakes distinction for a
        destructive operation than for search()'s normal read-only use.
        """
        if exclude_categories is None:
            exclude_categories = ["reflection", "core_summary"]

        filter_ = {"user_id": self.user_id}
        if exclude_categories:
            filter_["category"] = {"$nin": exclude_categories}

        # Fetch all candidates — embedding comparison is brute-force in Python.
        # One user's memory vault is never large enough for this to be slow.
        cursor = self.collection.find(
            filter_,
            {"_id": 0, "versions": 0},
        ).sort("updated_at", -1).limit(300)
        docs = [doc async for doc in cursor]

        if not docs:
            return []

        # Get query embedding once
        query_vec = await _embed_one(query)

        scored = []
        fallback_count = 0
        for doc in docs:
            vec = doc.get("embedding")
            if query_vec and vec and len(vec) == _EMBED_DIMS:
                score = _cosine(query_vec, vec)
            else:
                # Token-overlap fallback for un-embedded docs
                content = str(doc.get("content", ""))
                key = str(doc.get("key", ""))
                score = _semantic_overlap(query, content + " " + key)
                fallback_count += 1

            if score >= min_relevance and score > 0.0:
                doc["_relevance"] = score
                scored.append(doc)

        if fallback_count:
            logger.debug(
                f"search(): {fallback_count}/{len(docs)} docs used token-overlap fallback "
                f"(no embedding yet). They'll get vectors on next store() call."
            )

        scored.sort(key=lambda d: d["_relevance"], reverse=True)
        out = []
        for doc in scored[:limit]:
            doc.pop("_relevance", None)
            doc.pop("embedding", None)
            doc.pop("embedding_model", None)
            out.append(doc)
        return out

    async def deduplicate(self, similarity_threshold: float = 0.92) -> dict:
        """
        Find and merge semantically duplicate memories.

        Two memories are duplicates when cosine similarity >= threshold
        (default 0.92 — very close in meaning). Keeps the newer entry,
        deletes the older, appends older content to versions for history.

        Returns: {merged: int, checked: int, pairs: list[dict]}
        Safe to call multiple times — idempotent at same threshold.
        """
        cursor = self.collection.find(
            {"user_id": self.user_id, "embedding": {"$exists": True},
             "category": {"$ne": "core_summary"}},
            {"key": 1, "content": 1, "embedding": 1, "updated_at": 1, "category": 1}
        )
        docs = [doc async for doc in cursor]

        if len(docs) < 2:
            return {"merged": 0, "checked": len(docs), "pairs": []}

        merged = 0
        pairs = []
        deleted_ids = set()

        for i in range(len(docs)):
            if str(docs[i]["_id"]) in deleted_ids:
                continue
            vec_a = docs[i].get("embedding")
            if not vec_a or len(vec_a) != _EMBED_DIMS:
                continue

            for j in range(i + 1, len(docs)):
                if str(docs[j]["_id"]) in deleted_ids:
                    continue
                vec_b = docs[j].get("embedding")
                if not vec_b or len(vec_b) != _EMBED_DIMS:
                    continue

                score = _cosine(vec_a, vec_b)
                if score >= similarity_threshold:
                    # Keep newer, delete older
                    newer = docs[i] if (docs[i].get("updated_at") or 0) >= (docs[j].get("updated_at") or 0) else docs[j]
                    older = docs[j] if newer is docs[i] else docs[i]

                    pairs.append({
                        "kept": newer.get("key"),
                        "deleted": older.get("key"),
                        "similarity": round(score, 4),
                    })

                    # Preserve older content in versions history
                    await self.collection.update_one(
                        {"_id": newer["_id"]},
                        {"$push": {"versions": {
                            "content": older.get("content", ""),
                            "category": older.get("category", "general"),
                            "recorded_at": older.get("updated_at"),
                            "superseded": True,
                            "merged_from": older.get("key"),
                        }}}
                    )
                    await self.collection.delete_one({"_id": older["_id"]})
                    deleted_ids.add(str(older["_id"]))
                    merged += 1
                    break

        logger.info(f"deduplicate(): checked {len(docs)}, merged {merged} pairs")
        return {"merged": merged, "checked": len(docs), "pairs": pairs}
