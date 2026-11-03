"""
core/document_grounding.py — OADSON Trustworthy Task State, piece 1 of 2.

WHY THIS EXISTS (real production evidence, not a hypothetical):

    User: "Now teach me module 1" -> correct.
    User: "Module 2?" -> correct.
    User: "Maybe use this file to explain" [asking for Module 3]
    OADSON: confidently describes "Module 3: Peoples of Northern Nigeria
             (Hausa, Kanuri, Jukun, Igala)" -- this is actually Module 2,
             UNIT 3. There is no standalone "Module 3" with this title
             anywhere in the source PDF.
    User: "Hallucination detected. Why did you hallucinate?"
    OADSON: apologizes, then immediately fabricates a SECOND wrong
             answer -- "Module 3 is actually 'The Evolution of the
             Nigerian State'" -- a title that does not appear in the
             document AT ALL. This wasn't in the source; it was
             invented to sound plausible.
    User: forces OADSON to literally paste the document's real table
             of contents before it gets the right answer, on the third
             try.

The failure here was never a data problem -- document_reader had
already correctly extracted the full text; the fix for silent PDF
extraction loss shipped earlier the same night. The document was
fully readable. The model chose to answer confidently from a
plausible-sounding guess TWICE, rather than checking the text it
already had access to.

That is the exact gap this module closes: for any claim of the shape
"the document says X" / "Module N covers Y" / "the file contains Z",
this checks whether X/Y/Z is ACTUALLY PRESENT in the source text at
the claimed location, mechanically, before the claim is allowed to
reach the user as a verified answer. A claim that fails this check
never gets to hide behind confident phrasing.

This is deliberately NOT a semantic/LLM-based checker for the primary
path. An LLM asking another LLM "does this look right" reintroduces
the exact failure mode being fixed -- a second guess checking a first
guess. The primary check here is closer to grep: does the claimed
module title, or a close paraphrase of it, actually occur in the
source text, and does it occur in the region the claim says it does.
"""

import re
import difflib
from dataclasses import dataclass, field


@dataclass
class GroundingResult:
    grounded: bool
    # UNKNOWN is a real, distinct outcome from grounded=False -- see
    # module docstring in task_state.py, Section-21-style rule: an
    # inability to check must never silently become a pass.
    status: str  # "grounded" | "not_grounded" | "unknown"
    claim: str
    evidence: str | None = None       # the actual matching text, if found
    evidence_location: str | None = None  # e.g. "chars 1200-1400" or "page 5"
    confidence: float = 0.0
    reason: str = ""


_STOPWORDS = {
    "the", "of", "a", "an", "and", "or", "in", "on", "at", "to", "for",
    "is", "are", "was", "were", "be", "been", "this", "that", "with",
    "as", "by", "from", "it", "its",
}


def _content_words_overlap(claim: str, matched_region: str) -> float:
    """
    How much of the claim's DISTINCTIVE vocabulary (stopwords removed)
    actually shows up in the matched source region. This is the check
    that catches "Evolution of the Nigerian State" vs the real
    "Evolution of Nigeria: From Slave Trade to Amalgamation" -- both
    share "evolution"/"of"/"nigeria(n)", but the claim's own distinct
    words ("state") aren't in the source, while the source's real
    distinct content ("slave", "trade", "amalgamation") isn't in the
    claim either. A genuine match should share most of its non-generic
    words; a shared-vocabulary coincidence typically won't.
    """
    claim_words = {w for w in claim.split() if w not in _STOPWORDS and len(w) > 2}
    if not claim_words:
        return 0.0
    region_words = set(matched_region.split())
    overlap = claim_words & region_words
    return len(overlap) / len(claim_words)


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation noise that
    would otherwise cause an exact-match check to miss a real hit
    (e.g. 'Module 3:' vs 'Module 3 -' vs 'MODULE THREE')."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}


def _normalize_numbers(text: str) -> str:
    """'module three' and 'module 3' must be treated as the same claim
    -- this was literally the exact confusion in the real transcript
    (the source PDF headers spell out 'MODULE THREE', the user and
    OADSON both said 'Module 3'). Without this, a mechanically correct
    grounding check would itself miss the real match."""
    words = text.split()
    return " ".join(_NUMBER_WORDS.get(w, w) for w in words)


def extract_document_structure(full_text: str) -> list[dict]:
    """
    Parse a document's actual heading structure (module/unit/chapter
    numbers and titles) directly from its text, independent of any
    model's interpretation of it.

    This is the ground truth the grounding check compares claims
    against. It intentionally does NOT use a model call -- structure
    extraction via regex is slower to adapt to unusual documents but
    cannot hallucinate a heading that isn't there, which is the one
    property that actually matters here.

    Returns a list of {"level": "module"|"unit", "number": "3",
    "title": "...", "start_char": int, "raw_heading": "..."} in
    document order. A caller can still fall back to a model-assisted
    parse for documents whose headings don't match common patterns --
    but that fallback path MUST be labeled with a lower confidence,
    never silently treated as equally reliable as a direct text match.
    """
    headings = []
    # Common patterns actually seen across real course-material PDFs
    # (including the exact source document from the transcript):
    # "MODULE ONE", "MODULE 1", "Module 1:", "UNIT 3:", "Unit 3 -"
    patterns = [
        (r"\bMODULE\s+(ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN|\d+)\b[:\-\s]*([A-Z][^\n]{0,120})", "module"),
        (r"\bUnit\s+(\d+)\s*[:\-]\s*([^\n]{0,150})", "unit"),
    ]
    for pattern, level in patterns:
        for m in re.finditer(pattern, full_text, re.IGNORECASE):
            raw_num = m.group(1)
            number = _NUMBER_WORDS.get(raw_num.lower(), raw_num)
            title = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else ""
            headings.append({
                "level": level,
                "number": number,
                "title": title,
                "start_char": m.start(),
                "raw_heading": m.group(0).strip(),
            })
    headings.sort(key=lambda h: h["start_char"])
    return headings


def check_claim(claim: str, source_text: str, claimed_location: str | None = None) -> GroundingResult:
    """
    The core check: does `claim` actually appear in `source_text`?

    Three-tier check, strongest evidence first:
      1. Exact/near-exact substring match (normalized) -> high confidence grounded.
      2. Fuzzy match above a real similarity threshold -> grounded, lower confidence.
      3. No match found anywhere in the text -> NOT grounded. This is
         the tier that catches exactly what happened in the transcript:
         a fabricated title ("Evolution of the Nigerian State") that
         does not exist in the source at all would score ~0 similarity
         against every chunk of real text and correctly fail here.

    If source_text is empty or unavailable, returns status="unknown" --
    never "not_grounded" and never "grounded". An empty source is a
    missing-evidence case, not a false claim; conflating the two would
    make the checker itself unreliable in exactly the way it exists to
    prevent.
    """
    if not source_text or not source_text.strip():
        return GroundingResult(
            grounded=False, status="unknown", claim=claim,
            reason="No source text available to check against.",
        )

    norm_claim = _normalize_numbers(_normalize(claim))
    norm_source = _normalize_numbers(_normalize(source_text))

    if not norm_claim:
        return GroundingResult(
            grounded=False, status="unknown", claim=claim,
            reason="Claim was empty after normalization.",
        )

    # Tier 1: exact substring
    if norm_claim in norm_source:
        idx = norm_source.find(norm_claim)
        return GroundingResult(
            grounded=True, status="grounded", claim=claim,
            evidence=claim, evidence_location=f"char ~{idx}",
            confidence=1.0, reason="Exact match found in source text.",
        )

    # Tier 2: fuzzy match against sliding windows sized to the claim
    window = max(len(norm_claim), 20)
    best_ratio = 0.0
    best_pos = -1
    step = max(window // 4, 5)
    for i in range(0, max(len(norm_source) - window, 1), step):
        chunk = norm_source[i:i + window]
        ratio = difflib.SequenceMatcher(None, norm_claim, chunk).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_pos = i

    FUZZY_THRESHOLD = 0.72  # deliberately strict -- a near-miss on a
    # module NUMBER (Module 2 vs Module 3) must NOT pass as "close
    # enough", since that exact confusion is the real bug being fixed.
    #
    # BUG FOUND DURING TESTING 2026-08-16: character-sequence
    # similarity alone is not enough. Tested directly against a second
    # real fabrication from the same production transcript --
    # "Evolution of the Nigerian State" (invented, not in the document)
    # scored 0.74 similarity against the REAL title "Evolution of
    # Nigeria: From Slave Trade to Amalgamation", purely because both
    # strings share several common words ("evolution", "of",
    # "nigeria(n)"). A fabricated claim passed as grounded on the first
    # test run. difflib's SequenceMatcher measures character overlap,
    # not whether the claim's actual DISTINCT content words are
    # present -- so a plausible-sounding paraphrase that shares
    # vocabulary with the real title can slip through a pure
    # similarity-ratio check. Fixed by requiring the claim's non-stopword
    # content words to substantially overlap with the source region
    # too, not just character sequence -- both conditions must hold.
    if best_ratio >= FUZZY_THRESHOLD:
        if _content_words_overlap(norm_claim, norm_source[max(best_pos, 0):best_pos + window]) >= 0.6:
            return GroundingResult(
                grounded=True, status="grounded", claim=claim,
                evidence=source_text[best_pos:best_pos + window],
                evidence_location=f"char ~{best_pos}",
                confidence=round(best_ratio, 2),
                reason=f"Fuzzy match (similarity {best_ratio:.2f}) found in source text.",
            )
        # High character-sequence similarity but the claim's actual
        # distinct content words aren't well-represented in the match
        # region -- this is the shared-vocabulary-different-meaning
        # trap. Fall through to tier 3 (not grounded) rather than
        # trusting the ratio alone.

    # Tier 3: genuinely not found
    return GroundingResult(
        grounded=False, status="not_grounded", claim=claim,
        confidence=round(best_ratio, 2),
        reason=(
            f"No match found in source text (best similarity {best_ratio:.2f}, "
            f"below the {FUZZY_THRESHOLD} threshold). This claim does not appear "
            f"to be supported by the document."
        ),
    )


def check_module_claim(claimed_number: str, claimed_title: str, source_text: str) -> GroundingResult:
    """
    Purpose-built check for the exact transcript scenario: "Module N
    is titled X". Verifies BOTH that a heading numbered N exists in
    the document, AND that its actual title matches the claimed title
    -- not just that the title text appears somewhere unrelated to
    that number, which is what let "Module 2 Unit 3" get mislabeled as
    "Module 3" in the real incident (the unit's title text IS real and
    IS in the document -- just not under the claimed module number).
    """
    structure = extract_document_structure(source_text)
    modules = [h for h in structure if h["level"] == "module"]

    matching = [h for h in modules if h["number"] == str(claimed_number)]
    if not matching:
        available = [f"Module {h['number']}: {h['title'][:60]}" for h in modules]
        return GroundingResult(
            grounded=False, status="not_grounded",
            claim=f"Module {claimed_number}: {claimed_title}",
            reason=(
                f"No 'Module {claimed_number}' heading found in the document at all. "
                f"Modules actually present: {available if available else 'none detected'}."
            ),
        )

    heading = matching[0]
    title_check = check_claim(claimed_title, heading["title"] or heading["raw_heading"])
    if title_check.grounded:
        return GroundingResult(
            grounded=True, status="grounded",
            claim=f"Module {claimed_number}: {claimed_title}",
            evidence=heading["raw_heading"], evidence_location=f"char ~{heading['start_char']}",
            confidence=title_check.confidence,
            reason=f"Module {claimed_number} heading found with matching title.",
        )
    return GroundingResult(
        grounded=False, status="not_grounded",
        claim=f"Module {claimed_number}: {claimed_title}",
        reason=(
            f"Module {claimed_number} exists in the document, but its actual title "
            f"is \"{heading['title'] or heading['raw_heading']}\" -- not \"{claimed_title}\" "
            f"as claimed. This is very likely a unit/section being mislabeled as the "
            f"module itself (the exact real-world failure this checker exists to catch)."
        ),
    )
