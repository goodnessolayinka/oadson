"""
api/voice.py — OADSON V3 voice endpoints for the mobile app.

Three things the Expo client needs that didn't exist as HTTP routes:

  POST /tts                    text -> MP3 (base64), Edge TTS
  POST /stt                    audio -> transcript, Gemini, speech-profile biased
  POST /speech-profile/correction   save a "that's wrong" correction

Design notes, stated because they're deliberate:

- /tts reuses the SAME preset -> Edge voice map as
  skills/voice_reply/skill.py (imported, not re-typed) so the two paths
  can't drift apart and start sounding different.

- /stt does NOT reimplement the audio path. It builds the same inline
  audio part core/attachment_processor.py already builds for voice
  notes, and hands it to core/provider_manager.py's generate() — the
  one place that owns API keys, model strings and the quota fallback
  chain. Nothing here touches a Gemini URL or an API key directly.

- /stt is honest about the fallback limit: OpenRouter's free models
  can't hear audio, so if every Gemini model is quota-exhausted the
  request returns 503 rather than a fabricated transcript. See
  generate()'s own "no_vision_fallback" handling for the same policy
  applied to images.

- Audio is never stored. It's decoded, sent inline, and dropped.
"""

import base64
import binascii
import logging
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from dependency import get_current_user, get_db

logger = logging.getLogger("oadson.api.voice")

router = APIRouter()

# ── LIMITS ──
MAX_TTS_CHARS = 600
# 60s of speech. Generous for real voice notes; a rough ceiling that
# catches a client accidentally posting a whole audio file. Base64
# inflates by ~4/3, so 60s of 16kHz mono WAV (~1.9MB raw) is ~2.6MB
# encoded — this cap is deliberately well above that so a legitimate
# 60s clip is never rejected for encoding overhead alone.
MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_AUDIO_SECONDS = 60

ACCEPTED_AUDIO_MIMES = {
    "audio/aac",
    "audio/mp4",
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp3",
    "audio/webm",
    "audio/ogg",
}


class TTSRequest(BaseModel):
    text: str
    voice_preset: Optional[str] = "nigerian_female"


class TTSResponse(BaseModel):
    audio_base64: str
    mime: str = "audio/mpeg"
    voice: Optional[str] = None


class STTRequest(BaseModel):
    audio_base64: str
    mime: Optional[str] = None


class STTResponse(BaseModel):
    transcript: str
    confidence_note: Optional[str] = None


class CorrectionRequest(BaseModel):
    heard: str
    meant: str


@router.post(
    "/tts",
    response_model=TTSResponse,
    summary="Text to speech (Edge TTS) — returns base64 MP3",
    description=(
        "Generates an MP3 in memory and returns it base64-encoded. Nothing "
        "is stored. No LLM call is made — this is a direct TTS call, so it "
        "stays fast. Unknown voice_preset falls back to nigerian_female."
    ),
)
async def tts(body: TTSRequest, user: dict = Depends(get_current_user)):
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Text cannot be empty.")
    if len(text) > MAX_TTS_CHARS:
        raise HTTPException(400, f"Text too long — {MAX_TTS_CHARS} characters maximum.")

    # Imported from the skill rather than duplicated — see this module's
    # docstring. A preset added there is instantly available here.
    from skills.voice_reply.skill import VOICE_PRESETS, DEFAULT_VOICE

    preset = (body.voice_preset or "nigerian_female").strip()
    voice = VOICE_PRESETS.get(preset, DEFAULT_VOICE)

    try:
        import edge_tts
    except ImportError:
        logger.error("edge-tts is not installed — cannot serve /tts")
        raise HTTPException(503, "Text-to-speech is unavailable (edge-tts not installed).")

    try:
        communicate = edge_tts.Communicate(text, voice)
        buf = BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        audio_bytes = buf.getvalue()
    except Exception as e:
        logger.error(f"/tts generation failed: {e}", exc_info=True)
        raise HTTPException(503, f"Text-to-speech failed: {e}")

    if not audio_bytes:
        raise HTTPException(503, "Text-to-speech produced no audio — check the voice name.")

    return TTSResponse(
        audio_base64=base64.b64encode(audio_bytes).decode(),
        mime="audio/mpeg",
        voice=voice,
    )


@router.post(
    "/stt",
    response_model=STTResponse,
    summary="Speech to text — transcribes audio, biased by the owner's speech profile",
    description=(
        "Accepts base64 audio (aac/mp4/wav/mpeg/webm/ogg). Transcribes with "
        "Gemini using the same inline-audio path voice notes already use. The "
        "owner's stored speech profile (terms, corrections, phrases) is loaded "
        "into the prompt, and confirmed corrections are applied as a "
        "post-processing pass on the transcript. Max 60 seconds of audio."
    ),
)
async def stt(
    body: STTRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    raw_b64 = (body.audio_base64 or "").strip()
    if not raw_b64:
        raise HTTPException(400, "audio_base64 cannot be empty.")

    mime = (body.mime or "").strip().lower()
    # A client that doesn't send a mime gets a sane default rather than a
    # 400 — the recorder format is the client's business, not a reason to
    # refuse the request. An explicit, unsupported mime IS rejected.
    if mime and mime not in ACCEPTED_AUDIO_MIMES:
        raise HTTPException(
            400,
            f"Unsupported audio mime '{mime}'. Accepted: {', '.join(sorted(ACCEPTED_AUDIO_MIMES))}.",
        )
    if not mime:
        mime = "audio/webm"

    # Decode first — the real byte length is what the size cap should be
    # measured against, not the base64 string length.
    try:
        audio_bytes = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "audio_base64 is not valid base64.")

    if not audio_bytes:
        raise HTTPException(400, "Decoded audio is empty.")

    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(
            413,
            f"Audio too large — {len(audio_bytes)} bytes, {MAX_AUDIO_BYTES} maximum "
            f"(~{MAX_AUDIO_SECONDS} seconds).",
        )

    user_id = str(user["_id"])

    from core import speech_profile as sp
    from bootstrap import get as _bootstrap_get

    profile_result = await sp.get_profile(db, user_id)
    profile = profile_result.get("profile") or {}

    prompt = sp.build_stt_prompt(profile)

    # Same inline-audio mechanism core/attachment_processor.py uses for
    # voice notes — generate() attaches this to the last user message as
    # an inline_data part. The mime is what tells the model it's audio.
    provider = _bootstrap_get("provider_manager")

    try:
        reply, model_used = await provider.generate(
            messages=[{"role": "user", "content": prompt}],
            task_type="simple",
            image_data=raw_b64,
            image_mime=mime,
            surface="app",
        )
    except Exception as e:
        logger.error(f"/stt transcription failed: {e}", exc_info=True)
        raise HTTPException(503, f"Transcription failed: {e}")

    # generate()'s own honest no-vision fallback message means every
    # audio-capable model was exhausted. That is NOT a transcript — pass
    # it through as a 503 rather than returning it as if it were speech.
    if model_used == "no_vision_fallback":
        raise HTTPException(
            503,
            "Transcription unavailable right now — all audio-capable models are "
            "exhausted and the backup text models can't hear audio. Try again shortly.",
        )

    transcript = (reply or "").strip()

    # Strip the accidental wrapping a model sometimes adds despite being
    # told not to — a quoted transcript is still correct content.
    if len(transcript) >= 2 and transcript[0] == transcript[-1] and transcript[0] in "\"'":
        transcript = transcript[1:-1].strip()

    # Post-processing pass — see core/speech_profile.apply_corrections.
    corrected = sp.apply_corrections(transcript, profile.get("corrections") or [])

    confidence_note = None
    if not corrected:
        confidence_note = "No speech detected in the audio."
    elif corrected != transcript:
        confidence_note = "Speech-profile corrections applied."
    elif not profile.get("terms") and not profile.get("corrections"):
        confidence_note = (
            "No speech profile yet — accuracy on names and Pidgin/Yoruba may be lower. "
            "Add terms via @speech_profile to improve it."
        )

    return STTResponse(transcript=corrected, confidence_note=confidence_note)


@router.post(
    "/speech-profile/correction",
    summary="Save a speech-profile correction (the app's \"That's wrong\" button)",
    description=(
        "Stores a heard->meant mishearing pair on the owner's speech profile. "
        "Used by the mobile app when the owner corrects a transcript. Replaces "
        "any existing correction for the same 'heard' value rather than "
        "duplicating it."
    ),
)
async def save_correction(
    body: CorrectionRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    from core import speech_profile as sp

    result = await sp.add_correction(db, str(user["_id"]), body.heard, body.meant)
    if result.get("status") == "error":
        raise HTTPException(400, result.get("reason") or "Could not save correction.")
    return result
