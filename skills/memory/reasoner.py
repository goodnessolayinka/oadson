"""
memory/reasoner.py — OADSON V3 Memory Reasoning Engine.

This is the core of the Memory Intelligence EDD.

Transforms raw retrieved memories into condensed, reasoned evidence
before it ever reaches the LLM — so the LLM reasons FROM conclusions,
not FROM raw data dumps.

Pipeline:
  1. Intent Classification      — what does this question actually need?
  2. Category-Aware Retrieval   — pull from vault, sessions, notes, reminders
  3. Semantic Scoring           — rank by relevance without vector DB
  4. Memory Grouping            — cluster by theme
  5. Pattern Detection          — infer behaviours from clusters
  6. Evidence Building          — link every inference to its sources
  7. Confidence Scoring         — how much evidence backs each claim?
  8. Reflection                 — "what do these collectively imply?"
  9. Context Compression        — strip noise, keep signal

The output is a MemoryContext object:
  - evidence: list of sourced facts
  - patterns: inferred behaviours
  - reflection: one-paragraph internal synthesis
  - confidence: 0-100
  - sources_used: count breakdown
  - raw_memories: for transparency

Nothing in here fabricates. Every pattern points to real source keys.
If evidence is thin, confidence is low and the reasoner says so explicitly.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import httpx

logger = logging.getLogger("oadson.memory.reasoner")

# ── Memory categories with retrieval priority and semantic hints ──────────────
MEMORY_CATEGORIES = {
    "identity":       {"priority": 1, "hints": ["name", "age", "location", "university", "school", "level", "year"]},
    "preferences":    {"priority": 2, "hints": ["like", "love", "prefer", "favourite", "enjoy", "hate", "dislike"]},
    "projects":       {"priority": 1, "hints": ["build", "built", "project", "app", "backend", "frontend", "deploy", "repo"]},
    "goals":          {"priority": 2, "hints": ["goal", "target", "want", "plan", "aim", "achieve", "launch", "finish"]},
    "habits":         {"priority": 2, "hints": ["always", "usually", "every", "routine", "pattern", "often", "late", "night", "morning"]},
    "skills":         {"priority": 3, "hints": ["know", "learn", "expert", "skill", "language", "framework", "tool"]},
    "decisions":      {"priority": 2, "hints": ["decided", "chose", "picked", "switched", "moved", "dropped"]},
    "achievements":   {"priority": 3, "hints": ["built", "completed", "finished", "deployed", "shipped", "passed", "won"]},
    "failures":       {"priority": 3, "hints": ["failed", "broke", "bug", "error", "didn't work", "struggled"]},
    "lessons":        {"priority": 3, "hints": ["learned", "lesson", "realised", "now know", "mistake"]},
    "relationships":  {"priority": 3, "hints": ["friend", "collaborator", "team", "partner", "person", "met"]},
    "facts":          {"priority": 4, "hints": []},  # catch-all
}

# ── Intent → category mapping ─────────────────────────────────────────────────
# Maps intent signals in user questions to the categories most likely to contain
# relevant evidence. Checked in order — first match wins for primary intent.
INTENT_CATEGORY_MAP = [
    # Behavioural / personality questions
    (["obsess", "disciplin", "focus", "motivation", "dedicat", "commit", "persist",
      "habit", "routine", "pattern", "am i", "am i the", "would you say"],
     ["habits", "projects", "goals", "decisions", "achievements", "failures"]),

    # Project / work questions
    (["project", "build", "built", "app", "backend", "frontend", "deploy", "code",
      "work on", "working on", "repo", "github", "skill", "feature"],
     ["projects", "goals", "decisions", "skills", "achievements"]),

    # Identity / personal info
    (["who am i", "my name", "where am i", "university", "school", "course", "level",
      "how old", "location", "background"],
     ["identity", "facts", "preferences"]),

    # Goals / future
    (["goal", "plan", "want to", "trying to", "aim", "target", "next", "future",
      "launch", "when"],
     ["goals", "projects", "decisions"]),

    # Skills / learning
    (["know how", "skill", "learn", "language", "framework", "tool", "tech",
      "stack", "use for"],
     ["skills", "projects", "lessons"]),

    # Relationships / people
    (["who is", "collaborat", "team", "partner", "friend", "person", "people",
      "work with"],
     ["relationships", "projects", "facts"]),

    # Preferences / likes
    (["like", "love", "prefer", "favourite", "enjoy", "best", "worst", "hate"],
     ["preferences", "identity", "facts"]),

    # Reflection / self-assessment
    (["reflect", "summar", "overall", "tell me about myself", "what do you know",
      "how have i been", "progress"],
     ["habits", "goals", "achievements", "failures", "lessons", "projects"]),
]

# Minimum confidence to include an inference in the output
MIN_CONFIDENCE_THRESHOLD = 25


@dataclass
class Evidence:
    """A single sourced fact — never invented."""
    source_type: str          # "memory", "session", "note", "reminder"
    source_key: str           # memory key or session_id
    content: str              # the actual text
    category: str             # which memory category
    relevance_score: float    # 0.0–1.0


@dataclass
class Pattern:
    """An inferred behavioural pattern derived from multiple evidence items."""
    label: str                # short label e.g. "deep work tendency"
    inference: str            # the inference e.g. "Frequently works in long uninterrupted sessions"
    evidence_keys: list[str]  # which Evidence.source_key items support this
    confidence: int           # 0-100


@dataclass
class MemoryContext:
    """The complete output of the reasoning pipeline."""
    evidence: list[Evidence] = field(default_factory=list)
    patterns: list[Pattern] = field(default_factory=list)
    reflection: str = ""      # internal synthesis paragraph
    confidence: int = 0       # overall confidence 0-100
    needs_memory: bool = True
    intent_categories: list[str] = field(default_factory=list)
    sources_used: dict = field(default_factory=dict)
    raw_memories: list[dict] = field(default_factory=list)
    reasoning_skipped: bool = False
    skip_reason: str = ""

    def to_prompt_block(self) -> str:
        """
        Serialise to a structured text block for injection into the system prompt.
        This replaces the old "top 5 memories" dump.
        """
        if not self.needs_memory or self.reasoning_skipped:
            if self.skip_reason:
                return f"\n\n[Memory Engine]: {self.skip_reason}\n"
            return ""

        if not self.evidence and not self.patterns:
            return "\n\n[Memory Engine]: No relevant memories found for this question.\n"

        lines = ["\n\n=== MEMORY INTELLIGENCE CONTEXT ==="]
        lines.append(f"(Confidence: {self.confidence}% | "
                     f"Sources: {self.sources_used.get('total', 0)} memories, "
                     f"{self.sources_used.get('sessions', 0)} sessions)\n")

        if self.reflection:
            lines.append(f"INTERNAL REFLECTION:\n{self.reflection}\n")

        if self.patterns:
            lines.append("INFERRED PATTERNS:")
            for p in self.patterns:
                lines.append(f"  • {p.inference} (confidence: {p.confidence}%)")
            lines.append("")

        if self.evidence:
            lines.append("EVIDENCE:")
            for e in self.evidence[:12]:  # cap at 12 to avoid bloating context
                lines.append(f"  [{e.source_type.upper()}] {e.source_key}: {e.content}")
            lines.append("")

        lines.append("Use this evidence and these patterns to inform your answer. "
                     "Cite specific evidence when making claims. "
                     "If confidence is below 60%, be explicit about uncertainty. "
                     "CRITICAL: this evidence is drawn from PAST conversations and sessions, not "
                     "a live check you just ran right now. Never present a remembered detail "
                     "(a project path, a status, a setup fact) as something you just 'checked' or "
                     "that is 'currently' true — say it's something the user mentioned before. If "
                     "this turn also includes a genuine live tool result, keep the two clearly "
                     "separate rather than blending them into one 'current findings' narrative.")
        lines.append("=== END MEMORY CONTEXT ===")
        return "\n".join(lines)


# ── Intent Classification ─────────────────────────────────────────────────────

def classify_intent(objective: str) -> tuple[bool, list[str]]:
    """
    Returns (needs_memory, [relevant_categories]).

    Fast path: if the question clearly doesn't need memory (pure factual,
    skill invocation, transient task) skip the whole pipeline.
    """
    obj_lower = objective.lower()

    # Definite skip — skill invocations and transient tasks
    skip_signals = [
        "@gmail", "@github", "@terminal", "@spotify", "@web_search",
        "search for", "find me", "open ", "play ", "set a reminder",
        "what is the weather", "translate ", "convert ",
    ]
    if any(s in obj_lower for s in skip_signals):
        return False, []

    # Definite need — personal/behavioural questions
    personal_signals = [
        "am i", "do i", "have i", "my ", "i've", "i have", "i am",
        "what do you know", "tell me about myself", "how have i",
        "my project", "my goal", "my habit", "my skill", "what am i",
        "who am i", "obsess", "disciplin", "motivat", "what's my",
    ]
    needs = any(s in obj_lower for s in personal_signals)
    if not needs:
        # General questions about the world don't need personal memory
        general_signals = ["what is", "how does", "explain ", "who is ", "when was", "why does"]
        if any(s in obj_lower for s in general_signals) and not any(s in obj_lower for s in ["my", "i "]):
            return False, []
        # Default: attempt memory retrieval but at lower priority
        needs = True

    # Determine relevant categories
    categories = []
    for signals, cats in INTENT_CATEGORY_MAP:
        if any(s in obj_lower for s in signals):
            categories = cats
            break
    if not categories:
        categories = ["facts", "projects", "goals"]  # safe default

    return needs, categories


# ── Semantic Relevance Scoring ────────────────────────────────────────────────

def _tokenise(text: str) -> set[str]:
    """Lowercase word tokens, strip punctuation."""
    return set(re.findall(r"\b[a-z]{3,}\b", text.lower()))

def _semantic_score(objective: str, content: str) -> float:
    """
    TF-IDF-inspired overlap score without any external libraries.
    Returns 0.0–1.0. Higher = more relevant to the objective.
    """
    obj_tokens = _tokenise(objective)
    mem_tokens = _tokenise(content)
    if not obj_tokens or not mem_tokens:
        return 0.0
    overlap = len(obj_tokens & mem_tokens)
    # Jaccard similarity, weighted toward objective coverage
    jaccard = overlap / len(obj_tokens | mem_tokens)
    coverage = overlap / max(len(obj_tokens), 1)
    return round((jaccard * 0.4 + coverage * 0.6), 3)


# ── Pattern Detection ─────────────────────────────────────────────────────────

_PATTERN_RULES = [
    {
        "label": "deep_work_tendency",
        "triggers": ["3am", "late night", "overnight", "hours", "continuously", "all night",
                     "hyperfocus", "deep work", "marathon"],
        "inference": "Frequently enters long, uninterrupted deep-work sessions (including late nights)",
    },
    {
        "label": "iterative_builder",
        "triggers": ["rebuilt", "redesign", "refactor", "v2", "v3", "rewrite", "again",
                     "new version", "iteration", "migrat"],
        "inference": "Iterates heavily — regularly rebuilds and improves rather than shipping once",
    },
    {
        "label": "constraint_driven",
        "triggers": ["phone", "android", "termux", "no laptop", "mobile", "limited",
                     "zero", "without", "from scratch"],
        "inference": "Builds under significant constraints without letting them stop progress",
    },
    {
        "label": "high_output",
        "triggers": ["built", "deployed", "shipped", "completed", "finished", "launched",
                     "endpoint", "skill", "feature"],
        "inference": "Produces high output — consistently ships working products",
    },
    {
        "label": "engineering_dominant",
        "triggers": ["fastapi", "mongodb", "gemini", "python", "backend", "frontend",
                     "api", "database", "code", "skill", "deploy"],
        "inference": "Engineering is a dominant interest — vocabulary and focus skew heavily technical",
    },
    {
        "label": "self_directed",
        "triggers": ["alone", "solo", "myself", "self-taught", "no course", "figured out",
                     "taught myself", "independent"],
        "inference": "Largely self-directed — learns and builds independently",
    },
    {
        "label": "goal_oriented",
        "triggers": ["goal", "target", "plan", "launch", "october", "deadline", "revenue",
                     "milestone", "roadmap", "sprint"],
        "inference": "Operates with clear goals and timelines — not just building for building's sake",
    },
]

def detect_patterns(evidence_items: list[Evidence]) -> list[Pattern]:
    """
    Scan evidence for behavioural signals and produce inferred patterns.
    Each pattern lists the evidence keys that triggered it.
    """
    patterns = []
    combined_text = " ".join(e.content.lower() for e in evidence_items)

    for rule in _PATTERN_RULES:
        matching_sources = []
        for e in evidence_items:
            text = e.content.lower()
            if any(trigger in text for trigger in rule["triggers"]):
                matching_sources.append(e.source_key)

        if not matching_sources:
            continue

        # Confidence scales with number of independent sources
        n = len(matching_sources)
        confidence = min(95, 40 + (n * 10))  # 1 source=50%, 6+=100% cap at 95

        patterns.append(Pattern(
            label=rule["label"],
            inference=rule["inference"],
            evidence_keys=list(set(matching_sources)),
            confidence=confidence,
        ))

    # Sort by confidence descending
    return sorted(patterns, key=lambda p: p.confidence, reverse=True)


# ── Reflection Generation (Gemini call) ──────────────────────────────────────

async def _generate_reflection(
    objective: str,
    evidence: list[Evidence],
    patterns: list[Pattern],
    api_key: str,
) -> str:
    """
    One focused Gemini call: given the evidence and patterns,
    what do they collectively imply about this specific question?

    This is the step that turns storage into understanding.
    The output is a 2-4 sentence internal synthesis — not the final answer,
    just the reasoning that informs it.
    """
    if not evidence and not patterns:
        return ""

    evidence_text = "\n".join(
        f"- [{e.source_type}] {e.source_key}: {e.content}"
        for e in evidence[:15]
    )
    pattern_text = "\n".join(
        f"- {p.inference} (from {len(p.evidence_keys)} sources)"
        for p in patterns[:5]
    )

    prompt = f"""You are OADSON's internal Memory Reflection Engine. You are NOT responding to the user.

Your job: given raw evidence and detected patterns about a user, produce a 2-4 sentence internal synthesis that directly addresses what the evidence implies about their question. Be specific and evidence-based. Never invent facts not in the evidence.

User's question: "{objective}"

Evidence retrieved:
{evidence_text if evidence_text else "(none)"}

Detected patterns:
{pattern_text if pattern_text else "(none)"}

Write the internal reflection (2-4 sentences, direct, evidence-grounded):"""

    try:
        model = "gemini-3.5-flash-lite"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 200, "temperature": 0.3},
        }
        # SECURITY FIX 2026-08-14: key moved from URL to x-goog-api-key
        # header — was leaking into Render's httpx request logs.
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
            if r.status_code == 200:
                data = r.json()
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.warning(f"Reflection generation failed (non-fatal): {e}")

    # Fallback: synthesise from patterns without an extra API call
    if patterns:
        top = patterns[:2]
        return " ".join(p.inference + "." for p in top)
    return ""


# ── Session Evidence Extraction ───────────────────────────────────────────────

async def _extract_session_evidence(
    db, user_id: str, objective: str, limit: int = 20
) -> list[Evidence]:
    """
    Pull recent session messages and score them for relevance.
    Returns the top relevant messages as Evidence items.
    """
    evidence = []
    try:
        cursor = db["ai_sessions"].find(
            {"user_id": user_id},
            {"session_id": 1, "messages": 1, "created_at": 1}
        ).sort("updated_at", -1).limit(limit)

        async for doc in cursor:
            session_id = doc.get("session_id", "unknown")
            for msg in doc.get("messages", []):
                if msg.get("role") != "user":
                    continue
                content = (msg.get("content") or "").strip()
                if len(content) < 10:
                    continue
                score = _semantic_score(objective, content)
                if score > 0.08:
                    evidence.append(Evidence(
                        source_type="session",
                        source_key=session_id[:8],
                        content=content[:200],
                        category="sessions",
                        relevance_score=score,
                    ))
    except Exception as e:
        logger.warning(f"Session evidence extraction failed: {e}")

    return sorted(evidence, key=lambda e: e.relevance_score, reverse=True)[:8]


# ── Main Reasoning Pipeline ───────────────────────────────────────────────────

async def reason(
    db,
    user_id: str,
    objective: str,
    api_key: str,
    skip_reflection_call: bool = False,
) -> MemoryContext:
    """
    Full memory reasoning pipeline. Called by the orchestrator before
    building the system prompt — replaces the old _retrieve_memories.

    Returns a MemoryContext whose .to_prompt_block() is injected into
    the system prompt instead of a raw memory dump.

    skip_reflection_call=True: skip the extra Gemini API call for the
    reflection (use fallback pattern synthesis instead). Useful when
    quota is tight — the evidence and patterns are still returned.
    """
    ctx = MemoryContext()

    # ── Layer 1: Intent Classification ──
    needs_memory, intent_categories = classify_intent(objective)
    ctx.needs_memory = needs_memory
    ctx.intent_categories = intent_categories

    if not needs_memory:
        ctx.reasoning_skipped = True
        ctx.skip_reason = "Question does not require personal memory retrieval."
        return ctx

    # ── Layer 2: Category-Aware Memory Retrieval ──
    # Two-stage retrieval:
    #   Stage A: Pull top-40 semantically closest memories using embedding
    #            index (one DB query, fast). Falls back to category scan
    #            if objective embedding fails.
    #   Stage B: Category-based scan to catch memories that might be
    #            semantically distant but categorically relevant (e.g. a
    #            memory tagged "project" that uses different vocabulary).
    # Dedup by key across both stages.
    all_memories = []
    seen_keys = set()
    try:
        from memory.vault import _embed_one, _cosine, _EMBED_DIMS

        # Stage A — semantic pre-filter
        objective_vec_l2 = await _embed_one(objective)
        if objective_vec_l2:
            # Pull all embedded memories for this user and score them.
            # Brute-force cosine — one user's vault never has enough docs
            # for this to be slow. Take top 40 by score.
            cursor = db["memory"].find(
                {"user_id": user_id, "embedding": {"$exists": True},
                 "category": {"$ne": "core_summary"}},
                {"key": 1, "content": 1, "category": 1, "updated_at": 1, "embedding": 1}
            )
            candidates = []
            async for doc in cursor:
                vec = doc.get("embedding")
                if vec and len(vec) == _EMBED_DIMS:
                    score = _cosine(objective_vec_l2, vec)
                    candidates.append((score, doc))
            candidates.sort(key=lambda x: x[0], reverse=True)
            for score, doc in candidates[:40]:
                key = doc.get("key", "")
                if key not in seen_keys:
                    seen_keys.add(key)
                    doc["_l2_score"] = score  # carry score into Layer 3
                    all_memories.append(doc)
            logger.debug(f"Layer 2 Stage A: {len(all_memories)} semantic candidates")
        else:
            objective_vec_l2 = None

        # Stage B — category scan to catch vocabulary-distant but relevant memories
        for category in (intent_categories or list(MEMORY_CATEGORIES.keys())[:6]):
            cursor = db["memory"].find(
                {"user_id": user_id, "category": category},
                {"key": 1, "content": 1, "category": 1, "updated_at": 1, "embedding": 1}
            ).sort("updated_at", -1).limit(20)
            async for doc in cursor:
                key = doc.get("key", "")
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_memories.append(doc)

        # Stage C — recency batch for uncategorised memories
        cursor = db["memory"].find(
            {"user_id": user_id},
            {"key": 1, "content": 1, "category": 1, "updated_at": 1, "embedding": 1}
        ).sort("updated_at", -1).limit(20)
        async for doc in cursor:
            key = doc.get("key", "")
            if key not in seen_keys:
                seen_keys.add(key)
                all_memories.append(doc)

    except Exception as e:
        logger.warning(f"Memory retrieval failed: {e}")

    ctx.raw_memories = all_memories
    ctx.sources_used["total"] = len(all_memories)

    # ── Layer 3: Semantic Scoring ──
    # Reuse _l2_score from Stage A when available (already computed above).
    # Only embed objective once for docs that came through Stage B/C only.
    evidence: list[Evidence] = []
    try:
        from memory.vault import _embed_one, _cosine, _EMBED_DIMS
        # Reuse the vector from Layer 2 Stage A if available
        objective_vec = locals().get("objective_vec_l2") or await _embed_one(objective)
    except Exception:
        objective_vec = None

    for doc in all_memories:
        content = str(doc.get("content", ""))
        key = str(doc.get("key", ""))
        # Prefer the pre-computed score from Stage A
        if "_l2_score" in doc:
            score = doc.pop("_l2_score")
        else:
            vec = doc.get("embedding")
            if objective_vec and vec and len(vec) == _EMBED_DIMS:
                score = _cosine(objective_vec, vec)
            else:
                score = _semantic_score(objective, content + " " + key)
        evidence.append(Evidence(
            source_type="memory",
            source_key=key,
            content=content[:300],
            category=doc.get("category", "facts"),
            relevance_score=score,
        ))

    # ── Layer 4: Session Evidence ──
    session_evidence = await _extract_session_evidence(db, user_id, objective)
    evidence.extend(session_evidence)
    ctx.sources_used["sessions"] = len(session_evidence)

    # Sort all evidence by relevance, take top 20
    evidence = sorted(evidence, key=lambda e: e.relevance_score, reverse=True)[:20]
    ctx.evidence = evidence

    # ── Layer 5: Pattern Detection ──
    patterns = detect_patterns(evidence)
    ctx.patterns = patterns

    # ── Layer 6: Confidence Scoring ──
    if not evidence:
        ctx.confidence = 0
    else:
        # Base confidence from evidence count
        mem_count = len([e for e in evidence if e.source_type == "memory"])
        session_count = len([e for e in evidence if e.source_type == "session"])
        base = min(70, mem_count * 5 + session_count * 3)
        # Boost for patterns found
        pattern_boost = min(25, len(patterns) * 8)
        # Boost for high-relevance evidence
        top_scores = sorted([e.relevance_score for e in evidence], reverse=True)[:5]
        score_boost = int(sum(top_scores) * 10)
        ctx.confidence = min(95, base + pattern_boost + score_boost)

    # ── Layer 7: Memory Reflection ──
    if not skip_reflection_call and api_key and (evidence or patterns):
        ctx.reflection = await _generate_reflection(objective, evidence, patterns, api_key)
    elif patterns:
        # Free fallback: synthesise from top pattern
        ctx.reflection = patterns[0].inference + "." if patterns else ""

    return ctx
