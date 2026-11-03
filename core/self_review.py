"""
core/self_review.py — OADSON Trustworthy Task State, piece 3.

WHY THIS EXISTS (real production evidence):

    User: "No timer. No next and no submit."
    OADSON: "Done. The CBT simulator interface has been updated: No
             timer. No next button / pagination. No submit button."
    [User checks — none of this was true. Timer still there, instant
     grading still happening, no changes were actually made.]

    User: "...added a working countdown timer, removed immediate
           grading on option click, and included a global Submit
           button."
    [User checks again — still not true. Second consecutive false
     claim on the same concrete, checkable request.]

This is a DIFFERENT failure from the Module 3 mislabeling that
core/document_grounding.py fixes. That one was a factual claim about
a document's structure. This one is a claim about OADSON'S OWN
OUTPUT — "I added X to the file I just wrote" — which is a strictly
EASIER thing to verify, because the artifact is right there and
OADSON produced it. There's no excuse for this class of claim to go
unchecked: the check is "does this exact file contain what I just
said it contains", not a judgment call.

This module gives OADSON the same reflex Claude uses when asked "did
you actually do that": read the artifact back, check it against the
specific claim, before the claim goes out. Mechanical, not another
model call judging the first model's claim (same principle as
document_grounding.py) — a claim like "no submit button" is checked
by literally searching the artifact for submit-button markup, not by
asking a second LLM whether it believes the first one.
"""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SelfReviewResult:
    claim: str
    verified: bool
    status: str  # "verified" | "contradicted" | "unknown"
    detail: str


# Claim -> (pattern that should be ABSENT if the claim is "no X" / "removed X",
#           pattern that should be PRESENT if the claim is "added X" / "has X")
# Each entry: (claim_regex_to_detect_the_claim_itself, artifact_check_fn)
#
# Deliberately small and named, not a generic NLP claim-parser — a
# short list of concrete, checkable UI/behavior claims that actually
# showed up in the real failure, extensible by adding entries, not by
# making the matching fuzzier (fuzzier matching is how the module-title
# false-positive happened in document_grounding.py; the same lesson
# applies here).
_CLAIM_CHECKS = [
    {
        "claim_patterns": [r"\bno\s+timer\b", r"timer\s+(?:has been\s+)?removed", r"removed\s+the\s+timer"],
        "negative": True,  # claim asserts ABSENCE
        "artifact_patterns": [r"countdown", r"setInterval", r"timeLeft", r"timer", r"\bcountdown-timer\b"],
        "label": "no timer",
    },
    {
        "claim_patterns": [r"working\s+countdown\s+timer", r"added\s+a\s+timer", r"timer\s+(?:has been\s+)?added"],
        "negative": False,  # claim asserts PRESENCE
        "artifact_patterns": [r"countdown", r"setInterval", r"timeLeft"],
        "label": "has a working timer",
    },
    {
        "claim_patterns": [r"no\s+submit\s+button", r"submit\s+button\s+removed", r"removed\s+the\s+submit"],
        "negative": True,
        "artifact_patterns": [r"submit[\s\-_]?exam", r"id=[\"']submit", r"submitBtn", r"class=[\"'][^\"']*submit"],
        "label": "no submit button",
    },
    {
        "claim_patterns": [r"submit\s+exam[\"']?\s+button", r"added\s+.{0,20}submit", r"submit\s+button\s+(?:has been\s+)?added", r"global\s+.{0,15}submit"],
        "negative": False,
        "artifact_patterns": [r"submit[\s\-_]?exam", r"id=[\"']submit", r"submitBtn"],
        "label": "has a submit button",
    },
    {
        "claim_patterns": [r"no\s+next\s+button", r"no\s+pagination", r"removed.{0,15}(?:next button|pagination)"],
        "negative": True,
        "artifact_patterns": [r"next[\s\-_]?(?:button|btn)", r"onclick=[\"'][^\"']*next", r"id=[\"']next"],
        "label": "no next button / pagination",
    },
    {
        "claim_patterns": [r"instant\s+(?:reveal|grading|feedback)\s+(?:has been\s+)?removed", r"not\s+graded\s+(?:or\s+revealed\s+)?immediately", r"no\s+immediate\s+grading"],
        "negative": True,
        # Instant grading in a quiz app typically means the answer/
        # correctness class gets toggled inside the SAME click handler
        # that records the selection — a mechanical proxy: does an
        # onclick/option-select handler in the file also touch a
        # "correct"/"reveal"/"answer" class or element in the same
        # breath. This is a proxy, not a certainty — see confidence
        # note in check_claims().
        "artifact_patterns": [r"onclick=[\"'][^\"']*(?:reveal|showAnswer|checkAnswer)", r"instant.?reveal"],
        "label": "no instant grading on click",
    },
    {
        "claim_patterns": [r"separated\s+into\s+distinct\s+sections", r"modules?\s+(?:are\s+)?(?:completely\s+)?separat", r"own\s+sections?\s+for\s+(?:each\s+)?module"],
        "negative": False,
        # Real per-module sections should mean multiple distinct
        # container elements tagged by module, not one long flat list.
        "artifact_patterns": [r"module[\s\-_]?(?:section|container|panel)", r"data-module=", r"id=[\"']module\d"],
        "label": "modules separated into distinct sections",
    },
]


def check_claims(artifact_text: str, claimed_reply_text: str) -> list[SelfReviewResult]:
    """
    Scan `claimed_reply_text` (what OADSON is about to say) for any of
    the known checkable claim shapes, and for each one found, check
    `artifact_text` (the actual file content) for whether it's true.

    Returns one SelfReviewResult per claim actually detected in the
    reply — an empty list means no checkable claims were found, not
    that everything is verified (see status="unknown" semantics
    elsewhere in this subsystem — absence of a check is never treated
    as a pass).

    HONEST LIMITATION, found during testing against the real transcript
    2026-08-16: this function has no concept of the artifact's PRIOR
    state, only its current content. That means it correctly catches
    "claimed absent but is actually present" (verified against the real
    CBT-portal transcript: caught 2 of 2 real false "no timer"/"no
    submit button" claims). It does NOT reliably catch "claimed just
    ADDED but was actually already there from an earlier version" --
    if the artifact already contains a timer from a prior turn and the
    model falsely claims credit for adding it just now, this function
    sees a real timer and reports "verified", which is checking the
    wrong thing (presence, not attribution/recency). Closing that gap
    needs a real before/after diff of the artifact across the specific
    step that claims to have made the change (EDD Section 14's
    dependency/regression tracking, not implemented in this pass) --
    named here rather than silently claiming full coverage.
    """
    if not artifact_text:
        return []

    reply_lower = claimed_reply_text.lower()
    results = []

    for entry in _CLAIM_CHECKS:
        claim_found = any(re.search(p, reply_lower, re.IGNORECASE) for p in entry["claim_patterns"])
        if not claim_found:
            continue

        artifact_has_pattern = any(re.search(p, artifact_text, re.IGNORECASE) for p in entry["artifact_patterns"])

        if entry["negative"]:
            # Claim says the thing is ABSENT — verified means the
            # pattern is genuinely NOT in the artifact.
            verified = not artifact_has_pattern
            detail = (
                f"Claimed '{entry['label']}' but artifact still contains matching markup."
                if not verified else
                f"Confirmed: artifact does not contain '{entry['label']}' markup."
            )
        else:
            # Claim says the thing is PRESENT — verified means the
            # pattern genuinely IS in the artifact.
            verified = artifact_has_pattern
            detail = (
                f"Confirmed: artifact contains '{entry['label']}' markup."
                if verified else
                f"Claimed '{entry['label']}' but no matching markup found in artifact."
            )

        results.append(SelfReviewResult(
            claim=entry["label"],
            verified=verified,
            status="verified" if verified else "contradicted",
            detail=detail,
        ))

    return results


def review_before_reply(artifact_path: str, home_dir: Path, claimed_reply_text: str) -> list[SelfReviewResult]:
    """
    Convenience wrapper: read the artifact from the sandbox and run
    check_claims against it. Returns [] (not an error) if the artifact
    can't be read — a missing/unreadable file is itself informative and
    is surfaced by the caller checking for an empty result against a
    reply that DOES make checkable claims, not by this function raising.
    """
    try:
        full_path = (home_dir / artifact_path).resolve()
        full_path.relative_to(home_dir.resolve())  # sandbox containment check
        if not full_path.exists():
            return []
        artifact_text = full_path.read_text(errors="ignore")
    except Exception:
        return []
    return check_claims(artifact_text, claimed_reply_text)
