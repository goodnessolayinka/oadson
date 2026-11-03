"""
core/speech_profile.py — "style_profile, but for how you SOUND."

style_profile.py learns how you WRITE. This learns how you SPEAK, so
speech-to-text stops mangling the words that actually matter to you:
your project names, the people you work with, Pidgin and Yoruba, and
the accent-specific mishearings a general ASR model makes on your voice.

Same merge-not-overwrite philosophy as style_profile.py, and the same
honest limit: this only stores what you actually confirm. Nothing here
invents an example, a term, or a correction. A wrong entry in this
profile is worse than an empty one, because /stt biases its prompt
with it — a hallucinated term would actively push the transcriber
toward the wrong word.

Storage: MongoDB collection "speech_profile", one document per user.
Fields: terms, corrections, phrases, examples (capped), updated_at.

Used by:
  - api/voice.py's /stt   (loads terms + corrections into the prompt,
                           then applies corrections as a post-pass)
  - skills/speech_profile (the view/add_term/add_correction/add_example/
                           reset actions)
"""

import re
import logging
from datetime import datetime

logger = logging.getLogger("oadson.speech_profile")

# Caps. examples is capped at 20 per the spec; the others are capped
# generously but still bounded, because every term/correction here is
# injected into the /stt prompt on EVERY transcription — an unbounded
# list would quietly grow the prompt (and the cost) forever.
MAX_EXAMPLES = 20
MAX_TERMS = 200
MAX_CORRECTIONS = 200
MAX_PHRASES = 100


def _empty_profile(user_id: str) -> dict:
    return {
        "user_id": user_id,
        "terms": [],
        "corrections": [],
        "phrases": [],
        "examples": [],
        "updated_at": None,
    }


async def get_profile(db, user_id: str) -> dict:
    """Returns the stored profile, or an empty one — never None."""
    doc = await db["speech_profile"].find_one({"user_id": user_id})
    if not doc:
        return {"status": "info", "message": "No speech profile yet.", "profile": _empty_profile(user_id)}
    doc.pop("_id", None)
    return {"status": "success", "profile": doc}


async def _touch(db, user_id: str, update: dict, *, push: dict = None) -> None:
    """
    One place that writes, so every mutation stamps user_id + updated_at
    the same way. `update` is the $set/$addToSet part; `push` is an
    optional $push (used for the capped lists).
    """
    ops = dict(update or {})
    ops.setdefault("$set", {})
    ops["$set"]["user_id"] = user_id
    ops["$set"]["updated_at"] = datetime.utcnow()
    if push:
        ops["$push"] = push
    await db["speech_profile"].update_one({"user_id": user_id}, ops, upsert=True)


async def add_term(db, user_id: str, term: str) -> dict:
    """
    A word the owner uses that ASR tends to get wrong — a project name,
    a person's name, a piece of jargon. $addToSet is the merge here:
    adding the same term twice is a no-op rather than a duplicate.
    """
    term = (term or "").strip()
    if not term:
        return {"status": "error", "reason": "No 'term' provided."}

    doc = await db["speech_profile"].find_one({"user_id": user_id}) or {}
    if len(doc.get("terms", [])) >= MAX_TERMS and term not in doc.get("terms", []):
        return {"status": "error", "reason": f"Term list is full ({MAX_TERMS}). Reset or remove some first."}

    await _touch(db, user_id, {"$addToSet": {"terms": term}})
    return {"status": "success", "message": f"Added term: {term}", "term": term}


async def add_correction(db, user_id: str, heard: str, meant: str) -> dict:
    """
    A specific mishearing -> the word that was actually said. This is
    what the app saves when the owner taps "That's wrong".

    Merge semantics: if a correction for the same `heard` already exists,
    the new `meant` REPLACES it rather than piling up a second entry —
    otherwise the same mishearing could accumulate three conflicting
    targets and the post-pass would apply them in arbitrary order.
    """
    heard = (heard or "").strip()
    meant = (meant or "").strip()
    if not heard or not meant:
        return {"status": "error", "reason": "Both 'heard' and 'meant' are required."}

    doc = await db["speech_profile"].find_one({"user_id": user_id}) or {}
    existing = [
        c for c in doc.get("corrections", [])
        if (c.get("heard") or "").strip().lower() != heard.lower()
    ]
    if len(existing) >= MAX_CORRECTIONS:
        return {"status": "error", "reason": f"Correction list is full ({MAX_CORRECTIONS}). Reset first."}

    existing.append({"heard": heard, "meant": meant})
    await _touch(db, user_id, {"$set": {"corrections": existing}})
    return {"status": "success", "message": f"Correction saved: {heard} -> {meant}", "corrections_count": len(existing)}


async def add_example(db, user_id: str, audio_ref: str, transcript: str) -> dict:
    """
    A confirmed transcript for a real clip. Only ever called with a
    transcript the owner has actually confirmed — see this module's
    docstring on why nothing here is auto-generated.
    """
    transcript = (transcript or "").strip()
    if not transcript:
        return {"status": "error", "reason": "No 'transcript' provided."}

    entry = {
        "audio_ref": (audio_ref or "").strip() or None,
        "transcript": transcript,
        "added_at": datetime.utcnow(),
    }
    # $slice: -MAX_EXAMPLES keeps the newest 20 and drops the oldest —
    # the cap is enforced by Mongo itself, so it can't drift out of sync
    # with the constant above.
    await _touch(db, user_id, {}, push={"examples": {"$each": [entry], "$slice": -MAX_EXAMPLES}})
    return {"status": "success", "message": "Example stored.", "example": entry}


async def add_phrase(db, user_id: str, phrase: str) -> dict:
    """A short phrase with its correct spelling — e.g. a greeting or a
    recurring instruction the owner says often."""
    phrase = (phrase or "").strip()
    if not phrase:
        return {"status": "error", "reason": "No 'phrase' provided."}

    doc = await db["speech_profile"].find_one({"user_id": user_id}) or {}
    if len(doc.get("phrases", [])) >= MAX_PHRASES and phrase not in doc.get("phrases", []):
        return {"status": "error", "reason": f"Phrase list is full ({MAX_PHRASES}). Reset first."}

    await _touch(db, user_id, {"$addToSet": {"phrases": phrase}})
    return {"status": "success", "message": f"Added phrase: {phrase}", "phrase": phrase}


async def reset_profile(db, user_id: str) -> dict:
    result = await db["speech_profile"].delete_one({"user_id": user_id})
    if result.deleted_count:
        return {"status": "success", "message": "Speech profile cleared."}
    return {"status": "info", "message": "No speech profile existed to clear."}


def apply_corrections(transcript: str, corrections: list) -> str:
    """
    Post-processing pass: swap each known mishearing for the real word.

    Deliberately a plain, case-insensitive literal replacement rather
    than anything fuzzy — a correction the owner confirmed should apply
    exactly where that string appears and nowhere else. Applied longest-
    `heard`-first so a longer mishearing ("oadson vee three") is fixed
    before a shorter one that's a substring of it ("oadson") can chew
    through it first.
    """
    if not transcript or not corrections:
        return transcript

    ordered = sorted(
        (c for c in corrections if c.get("heard") and c.get("meant")),
        key=lambda c: len(c["heard"]),
        reverse=True,
    )
    out = transcript
    for c in ordered:
        try:
            out = re.sub(re.escape(c["heard"]), c["meant"], out, flags=re.IGNORECASE)
        except re.error:
            # A malformed stored pattern must never break a transcription
            # that already succeeded — skip just that one entry.
            logger.warning(f"Skipping invalid correction pattern: {c.get('heard')!r}")
    return out


def build_stt_prompt(profile: dict) -> str:
    """
    The system prompt /stt sends alongside the audio. Kept here (not in
    api/voice.py) so the prompt and the profile schema live next to each
    other — a new profile field can't be added without this function
    being right there to update.
    """
    terms = [t for t in (profile.get("terms") or []) if t][:MAX_TERMS]
    corrections = [c for c in (profile.get("corrections") or []) if c.get("heard")][:MAX_CORRECTIONS]
    phrases = [p for p in (profile.get("phrases") or []) if p][:MAX_PHRASES]

    lines = [
        "You are transcribing a voice note from the owner of this system.",
        "Transcribe the audio EXACTLY as spoken. Return ONLY the transcript —",
        "no preamble, no commentary, no quotation marks around it, no markdown.",
        "If the audio is silent or unintelligible, return an empty string.",
    ]

    if terms:
        lines.append(
            "\nWords and names this speaker uses — prefer these spellings when "
            "the audio sounds like them:\n" + ", ".join(terms)
        )
    if corrections:
        lines.append(
            "\nKnown mishearings to avoid (heard -> what was actually said):\n"
            + "\n".join(f"  {c['heard']} -> {c['meant']}" for c in corrections)
        )
    if phrases:
        lines.append(
            "\nShort phrases this speaker uses, with correct spelling:\n"
            + "\n".join(f"  {p}" for p in phrases)
        )

    lines.append(
        "\nThe speaker uses Nigerian English, and mixes in Nigerian Pidgin and "
        "Yoruba. Transcribe those words as the speaker actually said them — do "
        "not translate them into standard English, and do not 'correct' them "
        "into a different word just because a standard-English word is more "
        "common. If a word is genuinely unclear, write your best literal "
        "attempt rather than substituting a plausible-sounding different word."
    )
    return "\n".join(lines)
