from enum import Enum
from typing import Literal
from fastapi import HTTPException


# ── GEMINI MODELS (confirmed live as of June 2026) ──
# gemini-2.0-flash and gemini-2.0-flash-lite: SHUT DOWN June 1, 2026.
# gemini-1.5-*: SHUT DOWN, all return 404.
# gemini-3.1-flash-lite: GA, stable — best free-tier cheap/fast model.
# gemini-3.1-flash-lite:      GA, stable (May 2026) — frontier Flash, replaces
#                        all 2.x Flash for complex tasks.
# geminin-house:        GA, most capable stable Pro (as of April 2026).
class GeminiModel(str, Enum):
    # FREE TIER ONLY models (as of July 2026):
    # gemini-3.1-flash-lite: GA, free tier retained — cheapest, fastest
    # gemini-3.1-flash:      GA, free tier retained — stronger, still free
    # gemini-2.5-flash:      PAID ONLY since April 1 2026 — DO NOT USE on free key
    # gemini-3.1-pro:        PAID ONLY — DO NOT USE on free key
    FLASH_LITE = "gemini-3.1-flash-lite"   # simple: cheap, high-volume, free
    FLASH      = "gemini-3.1-flash"        # complex: stronger, still free
    # No PRO — it's paid-only. OpenRouter handles hard tasks instead.


# ── OPENROUTER FALLBACK MODELS (free tier, :free suffix required) ──
# OpenRouter uses the OpenAI-compatible chat completions shape.
# All :free models: 20 req/min, 50 req/day (1000/day after $10 spend).
# These are confirmed free as of June 2026 — roster rotates, so if one
# stops working swap it for another :free model from openrouter.ai/models.
class OpenRouterModel(str, Enum):
    # Strong general-purpose, confirmed free June 2026
    LLAMA_70B     = "meta-llama/llama-3.3-70b-instruct:free"
    # Best free coding/agentic model as of June 2026 (262K context)
    QWEN3_CODER   = "qwen/qwen3-coder-480b-a35b-instruct:free"
    # Reasoning fallback — use :free slug from openrouter.ai/models if R1 comes back
    MISTRAL_SMALL = "mistralai/mistral-small-3.2-24b-instruct:free"


# ── GEMINI FALLBACK CHAIN ──
# Tried in order when a Gemini model returns 429 / 500 RESOURCE_EXHAUSTED
# / 503 overloaded. "openrouter" is the sentinel that signals chat_router
# to switch providers entirely.
# Chain: flash-lite → flash → openrouter (no Pro — paid only on free key)
GEMINI_FALLBACK_CHAIN: dict[str, str | None] = {
    GeminiModel.FLASH_LITE.value: GeminiModel.FLASH.value,
    GeminiModel.FLASH.value:      "openrouter",  # Gemini exhausted → OpenRouter
}

# ── OPENROUTER FALLBACK CHAIN ──
# Tried in order when an OpenRouter model returns 429 / error.
OPENROUTER_FALLBACK_CHAIN: dict[str, str | None] = {
    OpenRouterModel.LLAMA_70B.value:     OpenRouterModel.QWEN3_CODER.value,
    OpenRouterModel.QWEN3_CODER.value:   OpenRouterModel.MISTRAL_SMALL.value,
    OpenRouterModel.MISTRAL_SMALL.value: None,  # fully exhausted
}

# First OpenRouter model to try when Gemini chain is exhausted
OPENROUTER_ENTRY = OpenRouterModel.LLAMA_70B.value


def get_gemini_fallback(current_model: str) -> str | None:
    """Next Gemini model in chain, or 'openrouter' sentinel, or None."""
    return GEMINI_FALLBACK_CHAIN.get(current_model)


def get_openrouter_fallback(current_model: str) -> str | None:
    """Next OpenRouter model in chain, or None if fully exhausted."""
    return OPENROUTER_FALLBACK_CHAIN.get(current_model)


def is_quota_error(status_code: int, response_text: str) -> bool:
    """
    Detect quota / rate-limit / overload errors that should trigger a
    fallback rather than a hard failure.

    Surfaces checked:
      - HTTP 429  — standard rate-limit
      - HTTP 500  — Gemini RESOURCE_EXHAUSTED (free-tier overload)
      - HTTP 503  — Gemini "overloaded" / provider capacity unavailable
    All three mean "try the next model", not "something is broken".
    """
    if status_code == 429:
        return True
    if status_code in (500, 503):
        lowered = response_text.lower()
        if "resource_exhausted" in lowered or "overloaded" in lowered:
            return True
    return False


def route_model(
    task_type: Literal["simple", "complex", "hard"],
    tokens_used_today: int,
    daily_limit: int = 1_000_000,
) -> str:
    """
    Returns the starting Gemini model for a request.
    Fallback across the chain is handled by call_with_fallback()
    in chat_router — this only picks the entry point.
    """
    if tokens_used_today >= daily_limit:
        raise HTTPException(
            status_code=429,
            detail="Daily token limit reached. Try again tomorrow."
        )

    remaining = daily_limit - tokens_used_today

    if task_type == "simple":
        return GeminiModel.FLASH_LITE.value

    elif task_type == "complex":
        return GeminiModel.FLASH.value

    elif task_type == "hard":
        # No paid Pro model — use Flash and let OpenRouter handle overflow
        return GeminiModel.FLASH.value

    return GeminiModel.FLASH_LITE.value
