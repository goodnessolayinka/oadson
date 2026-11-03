from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import base64
import os
import json
import logging
from database import db
from dependency import get_current_user
from core.provider_manager import GEMINI_FALLBACK, GEMINI_MODELS, ProviderManager

router = APIRouter(prefix="/auth/biometric", tags=["biometrics"])
logger = logging.getLogger("oadson.biometrics")

class EnrollRequest(BaseModel):
    samples: List[str]

class VerifyRequest(BaseModel):
    sample: str

OWNER_EMAIL = os.getenv("OADSON_OWNER_EMAIL", "")

def _clean_b64(data: str, default_type: str = "image/jpeg") -> tuple[str, str]:
    """Accept raw base64 or a data-URL-prefixed string; return (raw_b64, mime_type)."""
    mime = default_type
    if not data:
        return "", mime
    if data.lower().startswith("data:"):
        try:
            header, b64_str = data.split(",", 1)
            mime = header.split(";")[0].replace("data:", "").strip()
            return b64_str.strip(), mime
        except Exception:
            pass
    
    b64_str = data.strip()
    try:
        raw = base64.b64decode(b64_str[:64])
        if raw.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif raw.startswith(b"\x89PNG"):
            mime = "image/png"
        elif raw.startswith(b"RIFF"):
            mime = "audio/wav"
        elif raw.startswith(b"OggS"):
            mime = "audio/ogg"
        elif raw.startswith(b"\x1a\x45\xdf\xa3"):
            mime = "audio/webm"
    except Exception:
        pass
    return b64_str, mime


async def _verify_with_gemini(biometric_type: str, enrolled_samples: list, candidate_sample: str) -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set for biometric verification — defaulting to strict mismatch.")
        return {"matched": False, "confidence": 0.0, "reasoning": "GEMINI_API_KEY not configured."}


    if biometric_type == "face":
        prompt = (
            "You are an expert biometric facial verification system. "
            "Your task is to compare the enrolled owner's face reference image(s) [Image 1] with the candidate verification image [Image 2].\n\n"
            "Determine if Image 2 is the EXACT SAME INDIVIDUAL as Image 1.\n"
            "CRITICAL SECURITY REQUIREMENTS:\n"
            "1. Pay extreme attention to detailed facial anatomy: eye shape, inter-pupillary distance, nose bridge geometry, nostril width, jawline contour, cheekbone structure, ear shape, lip shape, and skin markers.\n"
            "2. SIBLING / BROTHER DISCRIMINATION: Siblings and brothers often share facial resemblances. You MUST rigorously discriminate between brothers who look similar. Look for subtle differences in nose slope, jawline sharpness/roundness, eye spacing, and facial proportions.\n"
            "3. If the candidate is a brother or relative of the owner who looks similar but has distinct facial differences, you MUST set matched to false.\n\n"
            "Respond strictly in valid JSON format with no markdown wrappers:\n"
            "{\n"
            "  \"matched\": true or false,\n"
            "  \"confidence\": 0.0 to 1.0 float,\n"
            "  \"reasoning\": \"detailed explanation of facial feature matches or differences\"\n"
            "}"
        )
    else:
        prompt = (
            "You are an expert biometric voice verification system. "
            "Your task is to compare the enrolled owner's voice reference audio sample(s) [Audio 1] with the candidate verification audio sample [Audio 2].\n\n"
            "Determine if Audio 2 was spoken by the EXACT SAME INDIVIDUAL as Audio 1.\n"
            "CRITICAL SECURITY REQUIREMENTS:\n"
            "1. Pay extreme attention to vocal timbre, fundamental frequency (pitch), resonance, vocal tract characteristics, accent, speech cadence, and formant patterns.\n"
            "2. SIBLING / RELATIVE DISCRIMINATION: Siblings often sound somewhat similar. You MUST strictly distinguish between different speakers even if they share family accent or voice height.\n"
            "3. If the speaker in Audio 2 is a different person (e.g. brother, friend, stranger), you MUST set matched to false.\n\n"
            "Respond strictly in valid JSON format with no markdown wrappers:\n"
            "{\n"
            "  \"matched\": true or false,\n"
            "  \"confidence\": 0.0 to 1.0 float,\n"
            "  \"reasoning\": \"detailed explanation of acoustic feature matches or differences\"\n"
            "}"
        )

    parts = [{"text": prompt}]

    # Add enrolled reference sample(s)
    for sample in enrolled_samples[:2]:
        b64_data, mime = _clean_b64(sample, "image/jpeg" if biometric_type == "face" else "audio/webm")
        if b64_data:
            parts.append({
                "inline_data": {
                    "mime_type": mime,
                    "data": b64_data
                }
            })

    # Add candidate verification sample
    cand_b64, cand_mime = _clean_b64(candidate_sample, "image/jpeg" if biometric_type == "face" else "audio/webm")
    if cand_b64:
        parts.append({
            "inline_data": {
                "mime_type": cand_mime,
                "data": cand_b64
            }
        })

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1
        }
    }

    # NORMAL FALLBACK PATH (2026-11-02): verification no longer makes its
    # own direct Gemini HTTP call with a hardcoded model string (that is
    # what kept 404ing on retired models). It enters at the same model
    # every other "simple" task uses, GEMINI_MODELS["simple"]
    # (gemini-3.5-flash-lite), and on quota errors walks the same
    # GEMINI_FALLBACK chain generate() walks, reusing ProviderManager's
    # _call_gemini so key handling, quota detection and response parsing
    # stay in one place. The multimodal payload (enrolled samples plus
    # candidate, inline) is still built here because generate() only
    # supports a single image part. The walk stops at the "openrouter"
    # sentinel: OpenRouter free models cannot see images or hear audio,
    # so a biometric comparison there would be meaningless - we return an
    # honest failure instead of a fake verdict, same as the manager does
    # for image requests past that point.
    pm = ProviderManager()
    current = GEMINI_MODELS.get("simple", "gemini-3.5-flash-lite")
    while current and current != "openrouter":
        try:
            content_text = await pm._call_gemini(current, payload)
        except ValueError as e:
            if "quota_exceeded" in str(e):
                nxt = GEMINI_FALLBACK.get(current)
                logger.warning(f"Biometric verify quota hit on {current} - falling back to {nxt}")
                current = nxt
                continue
            logger.error(f"Biometric verify failed on {current}: {e}")
            return {"matched": False, "confidence": 0.0, "reasoning": f"AI service error: {str(e)}"}
        except Exception as e:
            logger.exception(f"Biometric verify exception on {current}: {e}")
            return {"matched": False, "confidence": 0.0, "reasoning": f"Verification exception: {str(e)}"}

        clean_text = content_text.strip()
        if clean_text.startswith("```json"):
            clean_text = clean_text[7:]
        if clean_text.endswith("```"):
            clean_text = clean_text[:-3]
        clean_text = clean_text.strip()

        try:
            result = json.loads(clean_text)
            return {
                "matched": bool(result.get("matched", False)),
                "confidence": float(result.get("confidence", 0.0)),
                "reasoning": str(result.get("reasoning", "")),
            }
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.error(f"Biometric verify: unreadable model response on {current}")
            return {"matched": False, "confidence": 0.0, "reasoning": "AI returned an unreadable verdict."}

    return {
        "matched": False,
        "confidence": 0.0,
        "reasoning": "All vision-capable providers are quota-exhausted right now. Try again shortly.",
    }

@router.get("/status")
async def biometric_status(user: dict = Depends(get_current_user)):
    voice_rec = await db["biometrics"].find_one({"biometric_type": "voice"})
    face_rec = await db["biometrics"].find_one({"biometric_type": "face"})
    return {
        "voice_enrolled": bool(voice_rec and voice_rec.get("samples")),
        "face_enrolled": bool(face_rec and face_rec.get("samples")),
    }


@router.post("/{biometric_type}/enroll")
async def enroll_biometric(
    biometric_type: str,
    body: EnrollRequest,
    user: dict = Depends(get_current_user),
):
    if biometric_type not in ["voice", "face"]:
        raise HTTPException(status_code=400, detail="Invalid biometric type")

    if user.get("email") != OWNER_EMAIL:
        raise HTTPException(status_code=403, detail="Only the owner can enroll biometrics.")

    existing = await db["biometrics"].find_one({"biometric_type": biometric_type})
    if existing and existing.get("locked") and user.get("email") != OWNER_EMAIL:
        raise HTTPException(status_code=409, detail="Biometric already enrolled and locked.")

    valid_samples = []
    for sample in body.samples:
        b64_data, mime = _clean_b64(sample, "image/jpeg" if biometric_type == "face" else "audio/webm")
        if b64_data and len(b64_data) > 100:
            valid_samples.append(sample)

    if not valid_samples:
        raise HTTPException(status_code=400, detail="No valid biometric samples received")

    await db["biometrics"].update_one(
        {"biometric_type": biometric_type},
        {
            "$set": {
                "biometric_type": biometric_type,
                "samples": valid_samples,
                "locked": True,
            }
        },
        upsert=True,
    )

    return {
        "status": "success",
        "message": f"{biometric_type} successfully enrolled and locked with {len(valid_samples)} samples.",
        "samples_used": len(valid_samples),
    }


@router.post("/{biometric_type}/verify")
async def verify_biometric(
    biometric_type: str,
    body: VerifyRequest,
    user: dict = Depends(get_current_user),
):
    if biometric_type not in ["voice", "face"]:
        raise HTTPException(status_code=400, detail="Invalid biometric type")

    record = await db["biometrics"].find_one({"biometric_type": biometric_type})
    if not record or not record.get("samples"):
        raise HTTPException(status_code=404, detail="Biometric not enrolled")

    if not body.sample:
        raise HTTPException(status_code=400, detail="Empty verification sample")

    verification_result = await _verify_with_gemini(
        biometric_type=biometric_type,
        enrolled_samples=record["samples"],
        candidate_sample=body.sample
    )

    return {
        "matched": verification_result["matched"],
        "confidence": verification_result["confidence"],
        "reasoning": verification_result["reasoning"]
    }
