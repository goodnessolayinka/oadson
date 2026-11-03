"""
skills/voice_reply/skill.py — OADSON V3 Voice Note Generation.

Uses Microsoft Edge's online neural TTS service via the `edge-tts`
Python library (rany2/edge-tts) — genuinely free, no API key, no card,
same "hobby-tier-friendly" bar as image_generation's Pollinations
choice. Runs entirely over a WebSocket to Microsoft's own service, no
local model, no GPU needed.

FORMAT NOTE, stated honestly rather than overclaimed: this ships MP3.
WhatsApp's native "voice bubble" (waveform, mic icon, hold-to-play)
technically wants OGG container + Opus codec specifically. The
JS/Node port of edge-tts documents an explicit output-format switch
for this; the canonical Python library's public Communicate API
doesn't clearly expose the same override without going around its
documented interface. Sending real, correctly-labeled MP3 will
definitely arrive as a normal playable WhatsApp audio message — just
possibly with a standard audio-player bubble instead of the native
voice-note waveform, depending on how WhatsApp classifies it. Worth
confirming which one shows up on first real send; if the native
bubble matters enough to chase, the fix is a raw WebSocket call
requesting `webm-24khz-16bit-mono-opus` directly instead of going
through Communicate's default path — flagged here rather than quietly
assumed.

Delivery: same pattern as image_generation.py — stores the audio in
file_sessions with a download_url, and lets
api/whatsapp.py's _deliver_skill_attachment() (added the same day as
this skill) pick it up and actually send it via GreenAPI, instead of
this skill trying to know about WhatsApp specifics itself. Skills stay
surface-agnostic; the platform adapter handles delivery.
"""

import os
import re
import logging
from datetime import datetime
from io import BytesIO

logger = logging.getLogger("oadson.skill.voice_reply")

BACKEND_URL = os.getenv(
    "BACKEND_URL",
    os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000")
).rstrip("/")

DEFAULT_VOICE = "en-NG-EzinneNeural"  # Nigerian English — small nice-to-have default
MAX_CHARS = 3000  # keeps voice notes to a reasonable length; not a hard TTS-service limit

# Added 2026-08-09 — confirmed live working, then asked for accent/gender
# choice. All IDs below verified against Edge TTS's real voice catalog
# (rany2/edge-tts docs + the service's own --list-voices output) before
# shipping — not guessed. "butler" mapped to en-GB-RyanNeural as the
# closest real match (formal deep British male) — voice CHARACTER is
# something I can't personally verify without audio playback, so this
# is the standard recommendation for that vibe, not a listened-and-
# confirmed pick. en-GB-ThomasNeural is the alternative if Ryan doesn't
# land right.
VOICE_PRESETS = {
    "nigerian_female": "en-NG-EzinneNeural",
    "nigerian_male": "en-NG-AbeoNeural",
    "american_female": "en-US-AriaNeural",
    "american_male": "en-US-GuyNeural",
    "british_female": "en-GB-SoniaNeural",
    "british_male_butler": "en-GB-RyanNeural",
}


def _serve_url(filename: str, session_id: str) -> str:
    return f"{BACKEND_URL}/api/v3/files/serve/{session_id}/{filename}"


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:30] or "voice-note")


async def _store_binary(db, user_id, session_id, filename, file_bytes, mime_type):
    import base64
    encoded = base64.b64encode(file_bytes).decode()
    await db["file_sessions"].update_one(
        {"user_id": user_id, "session_id": session_id, "filename": filename},
        {"$set": {
            "user_id": user_id, "session_id": session_id,
            "filename": filename, "content": encoded,
            "mime_type": mime_type, "encoding": "base64",
            "uploaded_at": datetime.utcnow(),
        }},
        upsert=True
    )


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    user_id = payload.get("user_id")
    session_id = payload.get("session_id", "default")

    text = (args.get("text") or "").strip()
    if not text:
        return {"status": "error", "reason": "No 'text' provided."}
    if not user_id:
        return {"status": "error", "reason": "User ID not available."}

    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS]

    voice = args.get("voice")  # advanced/exact override, e.g. a specific edge-tts ID
    if not voice:
        preset = args.get("voice_preset") or "nigerian_female"
        voice = VOICE_PRESETS.get(preset, DEFAULT_VOICE)

    try:
        import edge_tts
    except ImportError:
        return {"status": "error", "reason": "edge-tts not installed. Run: pip install edge-tts"}

    try:
        communicate = edge_tts.Communicate(text, voice)
        buf = BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        audio_bytes = buf.getvalue()
        if not audio_bytes:
            return {"status": "error", "reason": "TTS produced no audio — check the voice name is valid."}
    except Exception as e:
        logger.error(f"voice_reply TTS error: {e}", exc_info=True)
        return {"status": "error", "reason": f"TTS generation failed: {e}"}

    filename = f"{_slugify(text)}.mp3"

    try:
        from bootstrap import get
        db = get("db")
        await _store_binary(db, user_id, session_id, filename, audio_bytes, "audio/mpeg")
    except Exception as e:
        return {"status": "error", "reason": f"Voice note generated but failed to store: {e}"}

    download_url = _serve_url(filename, session_id)
    result = {
        "status": "success",
        "filename": filename,
        "voice": voice,
        "size_bytes": len(audio_bytes),
        "download_url": download_url,
        "message": f"✅ Voice note ready\n📥 {download_url}",
    }
    if truncated:
        result["note"] = f"Text was truncated to {MAX_CHARS} characters to keep the voice note a reasonable length."
    return result
