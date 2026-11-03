"""
core/provider_manager.py — OADSON V3 Provider Abstraction.

Gemini and OpenRouter are interchangeable providers.
The orchestrator calls generate(). It doesn't care which model answered.

generate() now accepts optional image_data (base64 str) + image_mime
so vision requests send REAL image bytes to Gemini instead of a URL
pasted into text (which caused hallucinated image descriptions —
Gemini was never actually looking at any image, just guessing from
a URL string it can't fetch).

One place manages all of this. Nothing else touches API keys or model strings.
"""

import os
import httpx
import logging
from typing import Literal, Optional

logger = logging.getLogger("oadson.provider_manager")


# ── MODEL TABLES ──

GEMINI_MODELS = {
    "simple":  "gemini-3.5-flash-lite",
    "complex": "gemini-3.5-flash-lite",
    "hard":    "gemini-3.5-flash-lite",
}

# Fallback chain: 3.5-flash-lite (primary) → 3.1-flash-lite → 3.8-flash → openrouter
# FIX 2026-11-02: gemini-2.5-flash is retired (Google's API 404s it for
# new users) — chain now lands on gemini-3.8-flash (GA 2026-09-02).
GEMINI_FALLBACK: dict[str, str | None] = {
    "gemini-3.5-flash-lite": "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite": "gemini-3.8-flash",
    "gemini-3.8-flash":      "openrouter",
}

# Free-tier OpenRouter models only. No paid models mixed in.
# NOTE: OpenRouter free models here do NOT support vision.
# If an image request falls back to OpenRouter, it will be told
# it cannot see images rather than hallucinating.
OPENROUTER_ENTRY = "meta-llama/llama-3.3-70b-instruct:free"

OPENROUTER_FALLBACK: dict[str, str | None] = {
    "meta-llama/llama-3.3-70b-instruct:free":  "qwen/qwen-2.5-72b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free":         "mistralai/mistral-small-3.1-24b-instruct:free",
    "mistralai/mistral-small-3.1-24b-instruct:free": None,
}

# Self-hosted local model — terminal surface only, tried before Gemini.
# See generate()'s surface handling and _try_local_qwen() below.
# Same ngrok interstitial issue documented in provider_registry.py's
# local_qwen_cli entry applies here too — hence the extra header.
LOCAL_QWEN_BASE_URL = "https://negotiate-starship-identity.ngrok-free.dev/v1/chat/completions"
LOCAL_QWEN_MODEL_ID = "oadson-cli-q4"
LOCAL_QWEN_TIMEOUT_SECONDS = 30


def _is_quota_error(status_code: int, text: str) -> bool:
    if status_code == 429:
        return True
    if status_code in (500, 503):
        low = text.lower()
        if "resource_exhausted" in low or "overloaded" in low:
            return True
    return False


class ProviderManager:
    """
    Single interface to all AI providers.
    Call generate() — it handles routing, fallback, and errors.
    """

    def __init__(self) -> None:
        self._gemini_key = os.getenv("GEMINI_API_KEY", "")
        self._openrouter_key = os.getenv("OPENROUTER_API_KEY", "")

        if not self._gemini_key:
            logger.warning("GEMINI_API_KEY not set.")
        if not self._openrouter_key:
            logger.warning("OPENROUTER_API_KEY not set — OpenRouter fallback disabled.")

    async def generate(
        self,
        messages: list[dict],
        task_type: Literal["simple", "complex", "hard"] = "simple",
        system: str = "",
        image_data: Optional[str] = None,
        image_mime: Optional[str] = None,
        surface: str = "web",
    ) -> tuple[str, str]:
        """
        Generate a response. Returns (reply_text, model_used).

        image_data: base64-encoded image bytes (NOT a URL). If provided,
                    sent as real inline image data to Gemini.
        image_mime: e.g. "image/jpeg", "image/png"

        surface: ADDED 2026-09-07 — when "terminal" and no image is
        attached (the local model has no vision support), try the
        self-hosted local_qwen_cli model FIRST, before Gemini. This is
        the user's own fine-tuned model, own hardware — free to call,
        no quota to protect, so it's worth trying before burning a
        Gemini quota slot on the terminal surface specifically. Falls
        straight through to the existing Gemini->OpenRouter chain,
        completely untouched, on ANY failure (tunnel down, server not
        running, timeout, bad response) - same fail-open philosophy as
        every other fallback in this codebase. Every other surface
        (web/app/whatsapp/telegram) is entirely unaffected — this only
        ever fires when surface == "terminal".
        """
        # local_qwen_cli removed 2026-10-21.

        start_model = GEMINI_MODELS.get(task_type, GEMINI_MODELS["simple"])
        gemini_payload = self._build_gemini_payload(messages, system, image_data, image_mime)

        # Walk Gemini chain
        current = start_model
        while current and current != "openrouter":
            try:
                reply = await self._call_gemini(current, gemini_payload)
                logger.info(f"Provider: {current}")
                return reply, current
            except ValueError as e:
                if "quota_exceeded" in str(e):
                    current = GEMINI_FALLBACK.get(current)
                    logger.warning(f"Quota hit — falling back to: {current}")
                    continue
                raise

        # Walk OpenRouter chain — NO VISION SUPPORT on free models.
        # If there was an image, be honest instead of hallucinating.
        if image_data:
            return (
                "I can't process images right now — my vision provider is "
                "temporarily unavailable and my backup text models can't see images. "
                "Try again shortly.",
                "no_vision_fallback",
            )

        if not self._openrouter_key:
            raise RuntimeError(
                "Gemini quota exhausted and OPENROUTER_API_KEY is not set."
            )

        or_messages = self._build_openrouter_messages(messages, system)
        current_or = OPENROUTER_ENTRY
        while current_or:
            try:
                reply = await self._call_openrouter(current_or, or_messages)
                logger.info(f"Provider: openrouter/{current_or}")
                return reply, f"openrouter/{current_or}"
            except ValueError as e:
                if "quota_exceeded" in str(e):
                    current_or = OPENROUTER_FALLBACK.get(current_or)
                    logger.warning(f"OpenRouter quota hit — falling back to: {current_or}")
                    continue
                raise

        raise RuntimeError("All providers exhausted. No response possible.")

    # ── LOCAL (terminal surface only) ──

    async def _try_local_qwen(self, messages: list[dict], system: str) -> Optional[str]:
        """
        Best-effort call to the self-hosted local_qwen_cli model.
        Returns the reply text on success, or None on ANY failure
        (connection refused, timeout, non-200, malformed body) — never
        raises. Callers treat None as "fall through to Gemini", so this
        function's whole job is to fail quietly and let generate()
        decide what to do next, exactly like every other fallback step
        in this file.

        No auth (own unauthenticated llama-server), no vision, no tool
        calls — matches the local_qwen_cli entry in provider_registry.py.
        """
        payload_messages = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(messages)

        try:
            async with httpx.AsyncClient(timeout=LOCAL_QWEN_TIMEOUT_SECONDS) as client:
                res = await client.post(
                    LOCAL_QWEN_BASE_URL,
                    headers={
                        "Content-Type": "application/json",
                        "ngrok-skip-browser-warning": "true",
                    },
                    json={
                        "model": LOCAL_QWEN_MODEL_ID,
                        "messages": payload_messages,
                        "max_tokens": 2048,
                        "temperature": 0.7,
                    },
                )
            if res.status_code != 200:
                logger.warning(f"local_qwen_cli returned {res.status_code}: {res.text[:200]}")
                return None
            data = res.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.warning(f"local_qwen_cli unreachable: {e}")
            return None

    # ── GEMINI ──

    def _build_gemini_payload(
        self,
        messages: list[dict],
        system: str,
        image_data: Optional[str] = None,
        image_mime: Optional[str] = None,
    ) -> dict:
        contents = []
        for i, m in enumerate(messages):
            role = "user" if m["role"] == "user" else "model"
            parts = [{"text": m["content"]}]

            # Attach real image bytes to the LAST user message only
            is_last_user_msg = (role == "user" and i == len(messages) - 1)
            if is_last_user_msg and image_data and image_mime:
                parts.append({
                    "inline_data": {
                        "mime_type": image_mime,
                        "data": image_data,
                    }
                })

            contents.append({"role": role, "parts": parts})

        payload: dict = {"contents": contents}
        if system:
            payload["system_instruction"] = {"parts": [{"text": system}]}
        return payload

    async def _call_gemini(self, model: str, payload: dict) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        # SECURITY FIX 2026-08-14: key moved from URL to x-goog-api-key
        # header — was leaking into Render's httpx request logs.
        async with httpx.AsyncClient(timeout=90) as client:
            res = await client.post(url, json=payload, headers={"x-goog-api-key": self._gemini_key})

        if _is_quota_error(res.status_code, res.text):
            raise ValueError(f"quota_exceeded:{model}")
        if res.status_code != 200:
            raise RuntimeError(f"Gemini error {res.status_code}: {res.text[:300]}")

        data = res.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected Gemini response shape: {data}") from e

    # ── OPENROUTER ──

    def _build_openrouter_messages(self, messages: list[dict], system: str) -> list[dict]:
        result = []
        if system:
            result.append({"role": "system", "content": system})
        result.extend(messages)
        return result

    async def _call_openrouter(self, model: str, messages: list[dict]) -> str:
        async with httpx.AsyncClient(timeout=90) as client:
            res = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._openrouter_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "http://localhost:3000",
                    "X-Title": "OADSON V3",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": 2048,
                    "temperature": 0.7,
                },
            )

        if _is_quota_error(res.status_code, res.text):
            raise ValueError(f"quota_exceeded:{model}")
        if res.status_code != 200:
            raise RuntimeError(f"OpenRouter error {res.status_code}: {res.text[:300]}")

        data = res.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected OpenRouter response shape: {data}") from e
